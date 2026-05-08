# OSI SAST Scanner Technical Guide

## Project Overview

This project builds a containerized SAST and secret-scanning agent. The scanner runs inside Docker, scans either a Git repository or a mounted local folder, creates JSON reports, generates a CycloneDX SBOM, and uploads both outputs to Amazon S3.

The project currently contains:

- `Dockerfile`: builds the scanner image and installs all required scanner tools.
- `entrypoint.sh`: runtime workflow that prepares source code, runs scanners, builds final JSON reports, generates SBOM output, and uploads artifacts to S3.
- `empty.txt`: placeholder file with no runtime role.

The scanner includes these tools:

- Gitleaks: detects hardcoded secrets in files and Git content.
- TruffleHog: detects verified and unverified secrets in the filesystem.
- Semgrep: runs static analysis rules using `--config auto`.
- Trivy: generates a CycloneDX SBOM for the scanned filesystem.
- AWS CLI v2: uploads final reports to S3 and supports AWS CodeCommit credential helper integration.

## Dockerfile Flow

The `Dockerfile` uses a multi-stage build so scanner binaries are downloaded and prepared in separate builder stages, then copied into a smaller final runtime image.

### Gitleaks Builder Stage

```dockerfile
FROM debian:bookworm-slim AS gitleaks-builder
ARG GITLEAKS_VERSION=8.26.0
```

This stage:

1. Installs minimal download tools.
2. Downloads the configured Gitleaks Linux x64 release from GitHub.
3. Extracts the `gitleaks` binary into `/gitleaks`.
4. Removes temporary download files.

The final image later copies this binary to `/usr/local/bin/gitleaks`.

### TruffleHog Builder Stage

```dockerfile
FROM debian:bookworm-slim AS trufflehog-builder
ARG TRUFFLEHOG_VERSION=3.90.5
```

This stage:

1. Installs minimal download tools.
2. Downloads the configured TruffleHog Linux AMD64 release from GitHub.
3. Extracts the `trufflehog` binary into `/trufflehog`.
4. Removes temporary files.

The final image later copies this binary to `/usr/local/bin/trufflehog`.

### Semgrep Builder Stage

```dockerfile
FROM debian:bookworm-slim AS semgrep-builder
ARG SEMGREP_VERSION=1.131.0
```

This stage:

1. Installs Python, `venv`, `pip`, and certificates.
2. Creates a Python virtual environment at `/opt/venv`.
3. Installs the pinned Semgrep version into that virtual environment.

The final image later copies `/opt/venv` and adds it to `PATH`.

### Trivy Builder Stage

```dockerfile
FROM debian:bookworm-slim AS trivy-builder
ARG TRIVY_VERSION=0.62.1
```

This stage:

1. Downloads the configured Trivy Linux 64-bit archive.
2. Downloads the release checksums file.
3. Verifies the downloaded archive checksum.
4. Extracts the `trivy` binary into `/trivy`.

The checksum validation is important because Trivy is used to generate SBOM output.

### Final Runtime Stage

The final image is based on `debian:bookworm-slim`.

It installs:

- `git`: clone Git repositories.
- `curl` and `unzip`: install AWS CLI v2.
- `ca-certificates`: TLS support.
- `jq`: manipulate JSON output and SBOM metadata.
- `python3`: runtime support for Semgrep.
- AWS CLI v2: upload reports to S3 and support CodeCommit authentication.

It also:

1. Creates a non-root user named `appsecuser`.
2. Copies scanner binaries and the Semgrep virtual environment from builder stages.
3. Copies `entrypoint.sh` to `/usr/local/bin/entrypoint.sh`.
4. Makes binaries executable.
5. Sets Semgrep virtual environment variables.
6. Disables TruffleHog auto-update behavior through environment variables.
7. Runs the container as `appsecuser`.
8. Sets `/home/appsecuser` as the working directory.
9. Uses `entrypoint.sh` as the container entrypoint.

## Entrypoint Flow

The `entrypoint.sh` script is the scanner orchestrator. It exits on unhandled errors with `set -e`, but individual scanner commands are allowed to continue even if findings or scanner errors return non-zero exit codes.

### High-Level Runtime Sequence

1. Export TruffleHog auto-update disable variables.
2. Prepare source code using one of two modes:
   - Local folder mode with `FOLDER_PATH`.
   - Git clone mode with `REPO_URL` and `BRANCH`.
3. Run Gitleaks.
4. Run TruffleHog.
5. Run Semgrep.
6. Build a merged SAST JSON report.
7. Upload the SAST report to S3.
8. Generate a CycloneDX SBOM using Trivy.
9. Add scanner metadata to the SBOM.
10. Upload the SBOM to S3.

## Source Preparation Modes

### Local Folder Mode

Local folder mode is used when `FOLDER_PATH` is set.

Required variables:

- `FOLDER_PATH`
- `APP_NAME`
- `SERVICE_NAME`
- `S3_BUCKET`

The script validates that the folder exists, changes into that directory, and scans it directly.

This mode is useful when source code is already present on the Linux server and mounted into the container.

### Git Clone Mode

Git clone mode is used when `FOLDER_PATH` is not set.

Required variables:

- `APP_NAME`
- `SERVICE_NAME`
- `REPO_URL`
- `BRANCH`
- `IS_HOSTED_ON_PREM`
- `S3_BUCKET`

The script creates `/home/appsecuser/repo`, clones the configured branch, resolves the commit SHA, and checks out that exact commit.

If `IS_HOSTED_ON_PREM=True`, Git SSL verification is disabled:

```bash
git config --global http.sslVerify false
```

Use this only for trusted internal Git servers where certificate handling is known and accepted.

### AWS CodeCommit Mode

If `REPO_URL` contains `git-codecommit`, the script configures AWS CodeCommit Git authentication:

```bash
git config --global credential.helper '!aws codecommit credential-helper $@'
git config --global credential.UseHttpPath true
```

Required CodeCommit variables:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

If `AWS_DEFAULT_REGION` is not set, the script tries to infer it from the CodeCommit URL. If region inference fails, it defaults to `us-east-1`.

## Scanner Execution Details

### Gitleaks

The script runs:

```bash
gitleaks dir --report-format json --report-path "$TMP_GITLEAKS"
```

If the output file is empty, the result is normalized to an empty JSON array:

```json
[]
```

### TruffleHog

The script runs:

```bash
trufflehog filesystem . --json --no-update
```

TruffleHog emits newline-delimited JSON. The script converts it into a JSON array with:

```bash
jq -s '.'
```

If there is no output, the result is normalized to:

```json
[]
```

### Semgrep

The script runs:

```bash
semgrep scan --config auto --json
```

If there is no output, the result is normalized to:

```json
{}
```

### Trivy SBOM

The script runs:

```bash
trivy fs --format cyclonedx --output "$SBOM_TMP" .
```

Then it uses `jq` to replace or add top-level SBOM metadata with scanner metadata.

## Final SAST JSON Format

The final SAST report is written to `/tmp/osi-sast-<timestamp>.json`.

Top-level structure:

```json
{
  "metadata": {
    "scanner_agent_id": "...",
    "scan_job_id": "...",
    "app_service_id": "...",
    "base_url": "...",
    "api_auth_token": "...",
    "service_environment_id": "..."
  },
  "tools": {
    "gitleaks": {
      "version": "...",
      "results": []
    },
    "trufflehog": {
      "version": "...",
      "results": []
    },
    "semgrep": {
      "version": "...",
      "results": {}
    }
  }
}
```

Metadata variables are optional in the script, but they are included in the output if provided:

- `SCANNER_AGENT_ID`
- `SCAN_JOB_ID`
- `APP_SERVICE_ID`
- `BASE_URL`
- `AUTH_TOKEN`
- `SERVICE_ENVIRONMENT_ID`

## S3 Output Paths

The scanner uploads two files:

- SAST scanner JSON report.
- CycloneDX SBOM JSON report.

### Git Clone Mode S3 Paths

Branch names are sanitized by replacing `/` with `-`.

```text
s3://<S3_BUCKET>/<APP_NAME>/<SERVICE_NAME>/<SAFE_BRANCH>/<COMMIT_SHA>/osi-sast-scanner/<TIMESTAMP>.json
s3://<S3_BUCKET>/<APP_NAME>/<SERVICE_NAME>/<SAFE_BRANCH>/<COMMIT_SHA>/sbom/<TIMESTAMP>.json
```

Example:

```text
s3://my-security-reports/payments/checkout-service/main/abc123.../osi-sast-scanner/20260506-120102.json
s3://my-security-reports/payments/checkout-service/main/abc123.../sbom/20260506-120102.json
```

### Local Folder Mode S3 Paths

```text
s3://<S3_BUCKET>/<APP_NAME>/<SERVICE_NAME>/local/manual-folder-scan/osi-sast-scanner/<TIMESTAMP>.json
s3://<S3_BUCKET>/<APP_NAME>/<SERVICE_NAME>/local/manual-folder-scan/sbom/<TIMESTAMP>.json
```

## Build the Docker Image on a Linux Server

From the directory containing `Dockerfile` and `entrypoint.sh`:

```bash
docker build -t osi-sast-scanner:latest .
```

Verify the image exists:

```bash
docker images osi-sast-scanner
```

## Linux Server Running Scenarios

### Scenario 1: Scan a Public Git Repository

Use this when the repository can be cloned without credentials.

```bash
docker run --rm \
  -e APP_NAME="demo-app" \
  -e SERVICE_NAME="demo-service" \
  -e REPO_URL="https://github.com/example-org/example-repo.git" \
  -e BRANCH="main" \
  -e IS_HOSTED_ON_PREM="False" \
  -e S3_BUCKET="my-security-reports" \
  -e SCANNER_AGENT_ID="scanner-001" \
  -e SCAN_JOB_ID="job-001" \
  -e APP_SERVICE_ID="service-001" \
  -e BASE_URL="https://security.example.com" \
  -e AUTH_TOKEN="token-value" \
  -e SERVICE_ENVIRONMENT_ID="dev" \
  -v "$HOME/.aws:/home/appsecuser/.aws:ro" \
  osi-sast-scanner:latest
```

Expected flow:

1. Container starts as `appsecuser`.
2. Script validates Git mode variables.
3. Repository is cloned into `/home/appsecuser/repo`.
4. Scanner checks out the exact commit for `main`.
5. Gitleaks, TruffleHog, and Semgrep scan the repo.
6. Final SAST JSON is uploaded to S3.
7. Trivy generates a CycloneDX SBOM.
8. SBOM is uploaded to S3.

### Scenario 2: Scan an AWS CodeCommit Repository

Use this when the source repository is hosted in AWS CodeCommit.

```bash
docker run --rm \
  -e APP_NAME="platform" \
  -e SERVICE_NAME="billing-api" \
  -e REPO_URL="https://git-codecommit.us-east-1.amazonaws.com/v1/repos/billing-api" \
  -e BRANCH="main" \
  -e IS_HOSTED_ON_PREM="False" \
  -e S3_BUCKET="my-security-reports" \
  -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  -e AWS_DEFAULT_REGION="us-east-1" \
  -e SCANNER_AGENT_ID="scanner-002" \
  -e SCAN_JOB_ID="job-002" \
  -e APP_SERVICE_ID="billing-api" \
  -e BASE_URL="https://security.example.com" \
  -e AUTH_TOKEN="token-value" \
  -e SERVICE_ENVIRONMENT_ID="prod" \
  osi-sast-scanner:latest
```

Expected flow:

1. Script detects `git-codecommit` in `REPO_URL`.
2. AWS credential variables are validated.
3. AWS CodeCommit credential helper is configured.
4. Repository is cloned from CodeCommit.
5. Scanners run against the checked-out source.
6. SAST and SBOM reports are uploaded to S3.

### Scenario 3: Scan an Internal On-Prem Git Repository

Use this when the repository is on an internal Git server that requires SSL verification to be disabled.

```bash
docker run --rm \
  -e APP_NAME="internal-app" \
  -e SERVICE_NAME="legacy-service" \
  -e REPO_URL="https://git.internal.example.com/scm/team/legacy-service.git" \
  -e BRANCH="release/2026.05" \
  -e IS_HOSTED_ON_PREM="True" \
  -e S3_BUCKET="my-security-reports" \
  -v "$HOME/.aws:/home/appsecuser/.aws:ro" \
  osi-sast-scanner:latest
```

Expected flow:

1. Script validates Git mode variables.
2. SSL verification is disabled for Git.
3. Repository is cloned.
4. Branch name `release/2026.05` is sanitized to `release-2026.05` for the S3 path.
5. Scanners run.
6. Reports are uploaded to S3.

### Scenario 4: Scan a Local Folder Already Present on the Linux Server

Use this when source code is already checked out on the server.

Example source folder:

```text
/opt/source/checkout-service
```

Run:

```bash
docker run --rm \
  -e APP_NAME="payments" \
  -e SERVICE_NAME="checkout-service" \
  -e FOLDER_PATH="/scan-target" \
  -e S3_BUCKET="my-security-reports" \
  -e SCANNER_AGENT_ID="scanner-004" \
  -e SCAN_JOB_ID="job-004" \
  -e APP_SERVICE_ID="checkout-service" \
  -e BASE_URL="https://security.example.com" \
  -e AUTH_TOKEN="token-value" \
  -e SERVICE_ENVIRONMENT_ID="qa" \
  -v "/opt/source/checkout-service:/scan-target:ro" \
  -v "$HOME/.aws:/home/appsecuser/.aws:ro" \
  osi-sast-scanner:latest
```

Expected flow:

1. Script sees `FOLDER_PATH=/scan-target`.
2. Git clone is skipped.
3. Script changes into `/scan-target`.
4. Scanners run against the mounted folder.
5. S3 paths use `local/manual-folder-scan`.
6. SAST and SBOM reports are uploaded to S3.

### Scenario 5: Run on an EC2 Instance with an IAM Role

If the Linux server is an EC2 instance with an instance profile that allows S3 uploads, you do not need to mount `~/.aws`.

The IAM role needs permission similar to:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::my-security-reports/*"
    }
  ]
}
```

Run:

```bash
docker run --rm \
  -e APP_NAME="payments" \
  -e SERVICE_NAME="checkout-service" \
  -e REPO_URL="https://github.com/example-org/checkout-service.git" \
  -e BRANCH="main" \
  -e IS_HOSTED_ON_PREM="False" \
  -e S3_BUCKET="my-security-reports" \
  osi-sast-scanner:latest
```

Expected flow:

1. Source is cloned.
2. Scanner outputs are generated.
3. AWS CLI uses the EC2 instance role credentials.
4. Reports are uploaded to S3.

## Required Environment Variables

### Required for Local Folder Mode

| Variable | Description |
| --- | --- |
| `FOLDER_PATH` | Path inside the container to scan. Usually a mounted volume. |
| `APP_NAME` | Application name used in S3 object paths. |
| `SERVICE_NAME` | Service name used in S3 object paths. |
| `S3_BUCKET` | Destination S3 bucket name. |

### Required for Git Clone Mode

| Variable | Description |
| --- | --- |
| `APP_NAME` | Application name used in S3 object paths. |
| `SERVICE_NAME` | Service name used in S3 object paths. |
| `REPO_URL` | Git repository URL. |
| `BRANCH` | Branch to clone and scan. |
| `IS_HOSTED_ON_PREM` | Use `True` to disable Git SSL verification for on-prem Git. Otherwise use `False`. |
| `S3_BUCKET` | Destination S3 bucket name. |

### Required for AWS CodeCommit

| Variable | Description |
| --- | --- |
| `AWS_ACCESS_KEY_ID` | AWS access key used by CodeCommit credential helper. |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key used by CodeCommit credential helper. |
| `AWS_DEFAULT_REGION` | AWS region. Optional if region can be inferred from the CodeCommit URL. |

### Optional Metadata Variables

| Variable | Description |
| --- | --- |
| `SCANNER_AGENT_ID` | Scanner agent identifier included in reports. |
| `SCAN_JOB_ID` | Scan job identifier included in reports. |
| `APP_SERVICE_ID` | Application service identifier included in reports. |
| `BASE_URL` | Related platform or API base URL included in reports. |
| `AUTH_TOKEN` | Token value included in report metadata. Avoid storing sensitive production tokens here unless required. |
| `SERVICE_ENVIRONMENT_ID` | Environment identifier such as `dev`, `qa`, `stage`, or `prod`. |

## Full End-to-End Linux Server Flow

This is a complete example for a normal Git repository scan on a Linux server.

### 1. Install Docker

For Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y docker.io
sudo systemctl enable --now docker
```

Optional: allow the current user to run Docker without `sudo`.

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

### 2. Configure AWS Access

Option A: use an EC2 instance role with `s3:PutObject`.

Option B: configure local AWS credentials:

```bash
aws configure
```

Then mount `~/.aws` into the scanner container.

### 3. Build the Scanner Image

```bash
cd /opt/osi-sast-scanner
docker build -t osi-sast-scanner:latest .
```

### 4. Run the Scanner

```bash
docker run --rm \
  -e APP_NAME="payments" \
  -e SERVICE_NAME="checkout-service" \
  -e REPO_URL="https://github.com/example-org/checkout-service.git" \
  -e BRANCH="main" \
  -e IS_HOSTED_ON_PREM="False" \
  -e S3_BUCKET="my-security-reports" \
  -e SCANNER_AGENT_ID="scanner-linux-001" \
  -e SCAN_JOB_ID="scan-20260506-001" \
  -e APP_SERVICE_ID="checkout-service" \
  -e BASE_URL="https://security.example.com" \
  -e AUTH_TOKEN="token-value" \
  -e SERVICE_ENVIRONMENT_ID="prod" \
  -v "$HOME/.aws:/home/appsecuser/.aws:ro" \
  osi-sast-scanner:latest
```

### 5. Confirm Reports in S3

```bash
aws s3 ls "s3://my-security-reports/payments/checkout-service/main/" --recursive
```

You should see objects under:

```text
payments/checkout-service/main/<commit-sha>/osi-sast-scanner/<timestamp>.json
payments/checkout-service/main/<commit-sha>/sbom/<timestamp>.json
```

### 6. Download and Inspect a Report

```bash
aws s3 cp "s3://my-security-reports/payments/checkout-service/main/<commit-sha>/osi-sast-scanner/<timestamp>.json" ./sast-report.json
jq '.metadata, .tools.gitleaks.version, .tools.trufflehog.version, .tools.semgrep.version' sast-report.json
```

## Operational Notes

- The container runs as non-root `appsecuser`.
- Scanner failures are tolerated with `|| true`, so the pipeline can still produce and upload reports.
- S3 upload failures are not ignored. If `aws s3 cp` fails, the container exits with an error.
- In non-CodeCommit mode, the script unsets `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` before uploading so AWS CLI can use an IAM role. If you rely on explicit environment credentials for S3 in non-CodeCommit mode, mount `~/.aws` or use an instance role instead.
- `AUTH_TOKEN` is written into report metadata as `api_auth_token`. Treat generated reports as sensitive if this variable is populated.
- Trivy errors during SBOM generation are ignored, but the later `jq` step expects the temporary SBOM file to exist and contain valid JSON.
- `semgrep --config auto` may require network access depending on Semgrep behavior and ruleset resolution.

## Troubleshooting

### Git Clone Fails

Check:

- `REPO_URL` is reachable from the Linux server.
- `BRANCH` exists.
- Credentials are available for private repositories.
- For CodeCommit, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and region are correct.
- For on-prem Git, confirm whether `IS_HOSTED_ON_PREM=True` is required.

### S3 Upload Fails

Check:

- `S3_BUCKET` exists.
- AWS identity has `s3:PutObject` permission.
- The container can access AWS credentials.
- EC2 instance metadata access is available if relying on an IAM role.

### SBOM Upload Fails

Check whether Trivy produced valid JSON:

```bash
trivy fs --format cyclonedx --output /tmp/sbom.json .
jq '.' /tmp/sbom.json
```

### No Findings Found

No findings does not mean scanners failed. Empty outputs are normalized:

- Gitleaks: `[]`
- TruffleHog: `[]`
- Semgrep: `{}`

Review container logs for these messages:

```text
[INFO] Running Gitleaks
[INFO] Running TruffleHog
[INFO] Running Semgrep
[INFO] Uploading SAST results
[INFO] Generating SBOM using Trivy
[INFO] Uploading SBOM
[INFO] Completed. SAST results and SBOM uploaded.
```
