# OSI SCA Image Scanner - Technical Explanation

This project builds a Docker image that runs software composition analysis scans using Trivy, enriches the generated JSON output with application metadata, and uploads the scan results and SBOM files to Amazon S3.

The container supports two main execution modes:

- Image scan mode: scans a container image, usually from AWS ECR.
- Folder scan mode: scans a local source-code or filesystem folder mounted into the scanner container.

The mode is selected automatically by `entrypoint.sh`. If `FOLDER_PATH` is set, the container runs folder scan mode. If `FOLDER_PATH` is not set, it runs image scan mode.

## Project Files

This project currently contains:

- `Dockerfile`: builds the scanner image.
- `entrypoint.sh`: runtime script that performs scans, adds metadata, and uploads results to S3.

## Dockerfile Explanation

The Dockerfile uses a multi-stage build.

### Builder Stage

```dockerfile
FROM debian:bookworm-slim AS builder

ARG TRIVY_VERSION=0.67.2
```

The builder stage starts from a slim Debian image and defines the Trivy version to install.

```dockerfile
RUN set -e && \
    apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates gnupg tar curl && \
    rm -rf /var/lib/apt/lists/* && \
    wget -O /tmp/trivy.tar.gz "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" && \
    wget -O /tmp/trivy_checksums.txt "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_checksums.txt" && \
    cd /tmp && \
    grep "trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" trivy_checksums.txt | awk '{print $1}' > expected_checksum.txt && \
    sha256sum trivy.tar.gz | awk '{print $1}' > actual_checksum.txt && \
    cmp -s expected_checksum.txt actual_checksum.txt && \
    mkdir -p /trivy && \
    tar -xzf trivy.tar.gz -C /trivy trivy && \
    rm -rf /var/lib/apt/lists/* /tmp/*
```

This stage:

- Installs temporary tools needed to download and unpack Trivy.
- Downloads the Trivy release archive.
- Downloads the official checksum file.
- Verifies the downloaded archive with SHA256.
- Extracts only the `trivy` binary into `/trivy`.

The checksum validation is important because it helps ensure the binary was not corrupted or tampered with during download.

### Final Stage

```dockerfile
FROM debian:bookworm-slim
```

The final runtime image also uses Debian slim.

```dockerfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl unzip ca-certificates jq docker.io && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip" && \
    unzip /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /var/lib/apt/lists/* /tmp/*
```

The runtime image installs:

- `curl`: used to download AWS CLI.
- `unzip`: used to extract the AWS CLI installer.
- `ca-certificates`: needed for HTTPS connections.
- `jq`: used by the entrypoint to inject metadata into JSON output.
- `docker.io`: provides Docker CLI tooling.
- AWS CLI v2: used to upload scan results to S3.

```dockerfile
RUN useradd -r -G docker appsecuser
```

Creates a non-root system user named `appsecuser` and adds it to the `docker` group.

```dockerfile
COPY --from=builder /trivy/trivy /usr/local/bin/trivy
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
```

Copies the verified Trivy binary from the builder stage and copies the project entrypoint script into the final image.

```dockerfile
RUN chmod +x /usr/local/bin/trivy /usr/local/bin/entrypoint.sh && \
    chown appsecuser /usr/local/bin/trivy /usr/local/bin/entrypoint.sh
```

Marks both files as executable and gives ownership to `appsecuser`.

```dockerfile
USER appsecuser
WORKDIR /home/appsecuser
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
```

The scanner runs as `appsecuser`, starts in `/home/appsecuser`, and automatically executes `entrypoint.sh` when the container starts.

## Entrypoint Script Explanation

The entrypoint is a Bash script.

```bash
#!/bin/bash
set -e
```

`set -e` means the script exits immediately when a command fails, unless that command is explicitly allowed to fail with `|| true`.

## Main Functions

### `inject_metadata`

```bash
inject_metadata() {
    local input_file="$1"
    local output_file="$2"
    local metadata_type="$3"
    ...
}
```

This function reads a Trivy JSON file, adds a top-level `metadata` object, and writes the enriched JSON to a new file.

The metadata values come from environment variables:

- `SCANNER_AGENT_ID`
- `SCAN_JOB_ID`
- `APP_SERVICE_ID`
- `BASE_URL`
- `AUTH_TOKEN`
- `SERVICE_ENVIRONMENT_ID`

For SBOM files, the function first removes any existing `.metadata` field:

```jq
if metadata_type == "sbom" then
  del(.metadata)
else
  .
end
```

Then it adds project/platform metadata at the top level.

Final shape:

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
  "...": "original Trivy data"
}
```

### `generate_sbom`

```bash
generate_sbom() {
    local target="$1"
    local sbom_path="$2"
    local timestamp="$3"
    ...
}
```

This function:

1. Generates a CycloneDX SBOM using Trivy.
2. Adds metadata to the SBOM JSON.
3. Uploads the final SBOM file to S3.
4. Deletes temporary files.

Current command:

```bash
trivy image --format cyclonedx --output "$sbom_tmp" "$target" 2>/dev/null || true
```

In image scan mode, this makes sense because the target is a container image URI.

Important note: in folder scan mode, this same function is called with `FOLDER_PATH`. Since the function uses `trivy image`, Trivy will treat the folder path as an image name, not a filesystem path. For true folder SBOM generation, this command should usually be `trivy fs --format cyclonedx --output "$sbom_tmp" "$target"`.

### `upload_scan_results`

```bash
upload_scan_results() {
    local scan_tmp="$1"
    local scan_path="$2"
    local timestamp="$3"
    ...
}
```

This function:

1. Adds metadata to the Trivy scan result.
2. Uploads the final scan JSON to S3.
3. Deletes temporary files.

Upload command:

```bash
aws s3 cp "$scan_file" "s3://$S3_BUCKET/$scan_path"
```

### `setup_aws_credentials`

```bash
setup_aws_credentials() {
    if [[ -n "$AWS_ACCESS_KEY_ID" && -n "$AWS_SECRET_ACCESS_KEY" ]]; then
        export AWS_ACCESS_KEY_ID
        export AWS_SECRET_ACCESS_KEY
        ...
    fi
}
```

This function exports AWS credentials so both AWS CLI and Trivy can use them.

If `IMAGE_URI` is set, the region is parsed from the ECR image URI:

```bash
ECR_REGION=$(echo "$IMAGE_URI" | cut -d'.' -f4)
export AWS_DEFAULT_REGION=$ECR_REGION
```

For an ECR image like:

```text
123456789012.dkr.ecr.us-east-1.amazonaws.com/my-app:v1
```

The parsed region is:

```text
us-east-1
```

The script also sets:

```bash
export AWS_SDK_LOAD_CONFIG=1
export AWS_EC2_METADATA_DISABLED=true
```

These help Trivy's AWS SDK behavior, especially when running inside a container where EC2 instance metadata should not be queried.

### `cleanup_aws_credentials`

```bash
cleanup_aws_credentials() {
    unset AWS_ACCESS_KEY_ID
    unset AWS_SECRET_ACCESS_KEY
    unset AWS_SESSION_TOKEN
}
```

This removes AWS credentials from the process environment after the image scan and SBOM upload are complete.

## Folder Scan Mode

Folder scan mode runs when `FOLDER_PATH` is set.

```bash
if [[ -n "$FOLDER_PATH" ]]; then
```

Required variables checked by the script:

- `APP_NAME`
- `SERVICE_NAME`

Practically required for successful upload:

- `S3_BUCKET`
- AWS credentials, unless the container has another valid AWS credential source.

The script validates that the folder exists:

```bash
if [[ ! -d "$FOLDER_PATH" ]]; then
    echo "[ERROR] Folder does not exist: $FOLDER_PATH"
    exit 1
fi
```

The version is hard-coded:

```bash
VERSION="manual-folder-scan"
```

### Folder Scan Output Path

The vulnerability, secret, and misconfiguration scan result is uploaded to:

```text
s3://$S3_BUCKET/$APP_NAME/$SERVICE_NAME/hosted-repo/manual-folder-scan/osi-sca-image-scanner/$TIMESTAMP.json
```

The SBOM is uploaded to:

```text
s3://$S3_BUCKET/$APP_NAME/$SERVICE_NAME/hosted-repo/manual-folder-scan/sbom/$TIMESTAMP.json
```

### Folder Scan Command

```bash
trivy fs --scanners vuln,secret,misconfig "$FOLDER_PATH" \
    -f json --timeout 15m > "$SCAN_TMP" || true
```

This scans the mounted folder for:

- Vulnerabilities in dependencies.
- Secrets.
- Misconfigurations.

The command ends with `|| true`, so Trivy findings or scan errors do not stop the script immediately. This helps ensure the script still tries to upload whatever output was generated.

## Image Scan Mode

Image scan mode runs when `FOLDER_PATH` is not set.

```bash
echo "[INFO] Running IMAGE SCAN MODE"
```

Required variables:

- `APP_NAME`
- `SERVICE_NAME`
- `BRANCH`
- `VERSION`
- `IMAGE_URI`
- `S3_BUCKET`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

The script then exports AWS credentials and prepares a timestamp:

```bash
TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
```

Branch names are sanitized for S3 paths:

```bash
SAFE_BRANCH=$(echo "$BRANCH" | tr '/' '-')
```

Example:

```text
feature/login-api
```

becomes:

```text
feature-login-api
```

### Image Scan Output Path

Scan result path:

```text
s3://$S3_BUCKET/$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/osi-sca-image-scanner/$TIMESTAMP.json
```

SBOM path:

```text
s3://$S3_BUCKET/$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/sbom/$TIMESTAMP.json
```

### Image Scan Command

```bash
trivy image --format json "$IMAGE_URI" > "$SCAN_TMP" || true
```

This scans the container image and writes the result as JSON.

### ECR Authentication

For ECR images, the script parses these values:

```bash
ECR_REGION=$(echo "$IMAGE_URI" | cut -d'.' -f4)
ECR_REGISTRY=$(echo "$IMAGE_URI" | cut -d'/' -f1)
```

Then it exports:

```bash
export AWS_REGION="$ECR_REGION"
export AWS_SDK_LOAD_CONFIG=1
```

Trivy can authenticate to ECR automatically using the AWS credentials in the environment.

## End-to-End Flow

### Image Scan Flow

1. Linux server starts the scanner container.
2. Environment variables are passed into the container.
3. `entrypoint.sh` starts automatically.
4. Since `FOLDER_PATH` is not set, image scan mode is selected.
5. Script validates required variables.
6. Script exports AWS credentials.
7. Script creates a timestamp.
8. Script sanitizes the branch name.
9. Trivy scans the container image.
10. Script injects metadata into the scan JSON.
11. Script uploads the scan JSON to S3.
12. Script configures ECR-related AWS environment values.
13. Trivy generates a CycloneDX SBOM for the image.
14. Script injects metadata into the SBOM JSON.
15. Script uploads the SBOM JSON to S3.
16. Script unsets AWS credentials.
17. Container exits successfully.

### Folder Scan Flow

1. Linux server starts the scanner container with a host folder mounted into it.
2. `FOLDER_PATH` points to the mounted folder inside the container.
3. `entrypoint.sh` starts automatically.
4. Since `FOLDER_PATH` is set, folder scan mode is selected.
5. Script validates `APP_NAME` and `SERVICE_NAME`.
6. Script validates that the folder exists.
7. Script creates a timestamp.
8. Trivy scans the folder using filesystem scan mode.
9. Script injects metadata into the scan JSON.
10. Script uploads the scan JSON to S3.
11. Script attempts to generate an SBOM.
12. Script injects metadata into the SBOM JSON.
13. Script uploads the SBOM JSON to S3.
14. Container exits successfully.

## Build the Scanner Image on a Linux Server

From the project directory:

```bash
docker build -t osi-sca-image-scanner:latest .
```

Build with a different Trivy version:

```bash
docker build \
  --build-arg TRIVY_VERSION=0.67.2 \
  -t osi-sca-image-scanner:0.67.2 .
```

Verify the image exists:

```bash
docker images | grep osi-sca-image-scanner
```

## Example Scenario 1: Scan an AWS ECR Image

Example image:

```text
123456789012.dkr.ecr.us-east-1.amazonaws.com/payment-api:1.4.2
```

Run command:

```bash
docker run --rm \
  -e APP_NAME="banking-platform" \
  -e SERVICE_NAME="payment-api" \
  -e BRANCH="release/1.4" \
  -e VERSION="1.4.2" \
  -e IMAGE_URI="123456789012.dkr.ecr.us-east-1.amazonaws.com/payment-api:1.4.2" \
  -e S3_BUCKET="my-security-scan-results" \
  -e AWS_ACCESS_KEY_ID="AKIA..." \
  -e AWS_SECRET_ACCESS_KEY="..." \
  -e SCANNER_AGENT_ID="scanner-agent-001" \
  -e SCAN_JOB_ID="scan-job-789" \
  -e APP_SERVICE_ID="svc-payment-api" \
  -e BASE_URL="https://security.example.com" \
  -e AUTH_TOKEN="token-value" \
  -e SERVICE_ENVIRONMENT_ID="prod" \
  osi-sca-image-scanner:latest
```

Expected behavior:

- The scanner runs in image scan mode.
- Trivy scans the ECR image.
- Scan results are uploaded to:

```text
s3://my-security-scan-results/banking-platform/payment-api/release-1.4/1.4.2/osi-sca-image-scanner/<timestamp>.json
```

- SBOM is uploaded to:

```text
s3://my-security-scan-results/banking-platform/payment-api/release-1.4/1.4.2/sbom/<timestamp>.json
```

## Example Scenario 2: Scan a Local Folder on a Linux Server

Assume the source code is on the Linux server at:

```text
/opt/apps/payment-api
```

Run command:

```bash
docker run --rm \
  -v /opt/apps/payment-api:/scan-target:ro \
  -e FOLDER_PATH="/scan-target" \
  -e APP_NAME="banking-platform" \
  -e SERVICE_NAME="payment-api" \
  -e S3_BUCKET="my-security-scan-results" \
  -e AWS_ACCESS_KEY_ID="AKIA..." \
  -e AWS_SECRET_ACCESS_KEY="..." \
  -e AWS_DEFAULT_REGION="us-east-1" \
  -e SCANNER_AGENT_ID="scanner-agent-001" \
  -e SCAN_JOB_ID="manual-folder-scan-123" \
  -e APP_SERVICE_ID="svc-payment-api" \
  -e BASE_URL="https://security.example.com" \
  -e AUTH_TOKEN="token-value" \
  -e SERVICE_ENVIRONMENT_ID="dev" \
  osi-sca-image-scanner:latest
```

Expected behavior:

- The scanner runs in folder scan mode.
- Trivy scans `/scan-target`.
- Scan results are uploaded to:

```text
s3://my-security-scan-results/banking-platform/payment-api/hosted-repo/manual-folder-scan/osi-sca-image-scanner/<timestamp>.json
```

- SBOM is uploaded to:

```text
s3://my-security-scan-results/banking-platform/payment-api/hosted-repo/manual-folder-scan/sbom/<timestamp>.json
```

## Example Scenario 3: Scan a Public Docker Hub Image

This script's image scan mode requires AWS credentials because it always uploads results to S3.

Example:

```bash
docker run --rm \
  -e APP_NAME="demo" \
  -e SERVICE_NAME="nginx" \
  -e BRANCH="main" \
  -e VERSION="latest" \
  -e IMAGE_URI="nginx:latest" \
  -e S3_BUCKET="my-security-scan-results" \
  -e AWS_ACCESS_KEY_ID="AKIA..." \
  -e AWS_SECRET_ACCESS_KEY="..." \
  -e AWS_DEFAULT_REGION="us-east-1" \
  osi-sca-image-scanner:latest
```

Expected behavior:

- Trivy pulls and scans `nginx:latest`.
- Results and SBOM are uploaded to S3.

Important note: the script parses ECR region from `IMAGE_URI`. For non-ECR image names like `nginx:latest`, that parsing does not produce a valid region. The upload can still work if `AWS_DEFAULT_REGION` is set, but the ECR-specific region logic is designed for ECR image URIs.

## Example Scenario 4: Run with Temporary AWS Session Credentials

If the server uses temporary STS credentials, pass the session token too:

```bash
docker run --rm \
  -e APP_NAME="banking-platform" \
  -e SERVICE_NAME="payment-api" \
  -e BRANCH="main" \
  -e VERSION="2026.05.06" \
  -e IMAGE_URI="123456789012.dkr.ecr.us-east-1.amazonaws.com/payment-api:2026.05.06" \
  -e S3_BUCKET="my-security-scan-results" \
  -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  -e AWS_SESSION_TOKEN="$AWS_SESSION_TOKEN" \
  osi-sca-image-scanner:latest
```

Current script note: `setup_aws_credentials` exports `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, but does not explicitly export `AWS_SESSION_TOKEN`. Environment variables passed through `docker run -e AWS_SESSION_TOKEN=...` are already available inside the container, so this usually works. The cleanup function does unset `AWS_SESSION_TOKEN`.

## Example Scenario 5: Use the Host Docker Socket

The final image installs Docker CLI. If you want the scanner container to access the host Docker daemon, mount the Docker socket:

```bash
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e APP_NAME="demo" \
  -e SERVICE_NAME="local-image" \
  -e BRANCH="main" \
  -e VERSION="local" \
  -e IMAGE_URI="my-local-image:latest" \
  -e S3_BUCKET="my-security-scan-results" \
  -e AWS_ACCESS_KEY_ID="AKIA..." \
  -e AWS_SECRET_ACCESS_KEY="..." \
  -e AWS_DEFAULT_REGION="us-east-1" \
  osi-sca-image-scanner:latest
```

This can be useful if the image exists locally on the Linux server and is not available in a remote registry.

Security note: mounting `/var/run/docker.sock` gives the container powerful access to the host Docker daemon. Use this only in trusted environments.

## Required IAM Permissions

For ECR image scans, the AWS identity should be able to authenticate to ECR and read the image.

Typical ECR permissions:

```json
{
  "Effect": "Allow",
  "Action": [
    "ecr:GetAuthorizationToken",
    "ecr:BatchCheckLayerAvailability",
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchGetImage"
  ],
  "Resource": "*"
}
```

For S3 uploads:

```json
{
  "Effect": "Allow",
  "Action": [
    "s3:PutObject"
  ],
  "Resource": "arn:aws:s3:::my-security-scan-results/*"
}
```

If the bucket requires multipart upload or encryption-specific permissions, additional S3 or KMS permissions may be required.

## Expected S3 Structure

For image scans:

```text
bucket/
  APP_NAME/
    SERVICE_NAME/
      BRANCH/
        VERSION/
          osi-sca-image-scanner/
            TIMESTAMP.json
          sbom/
            TIMESTAMP.json
```

For folder scans:

```text
bucket/
  APP_NAME/
    SERVICE_NAME/
      hosted-repo/
        manual-folder-scan/
          osi-sca-image-scanner/
            TIMESTAMP.json
          sbom/
            TIMESTAMP.json
```

## Example Log Flow

Image scan mode:

```text
[INFO] Running IMAGE SCAN MODE
[INFO] Running Trivy IMAGE scan...
[INFO] Adding metadata to scan results
[INFO] Uploading scan results -> s3://my-security-scan-results/...
[INFO] Configuring ECR authentication for Trivy...
[INFO] ECR authentication configured for Trivy (using AWS credentials)
[INFO] Generating SBOM using Trivy for: 123456789012.dkr.ecr.us-east-1.amazonaws.com/payment-api:1.4.2
[INFO] Adding metadata to SBOM
[INFO] Uploading SBOM -> s3://my-security-scan-results/...
[INFO] Image scan + SBOM generation completed successfully.
```

Folder scan mode:

```text
[INFO] Running LOCAL FOLDER SCAN at: /scan-target
[INFO] Running Trivy filesystem scan...
[INFO] Adding metadata to scan results
[INFO] Uploading scan results -> s3://my-security-scan-results/...
[INFO] Generating SBOM using Trivy for: /scan-target
[INFO] Adding metadata to SBOM
[INFO] Uploading SBOM -> s3://my-security-scan-results/...
[INFO] Folder scan completed successfully.
```

## Operational Notes

### Trivy Exit Behavior

The scan commands use `|| true`:

```bash
trivy image --format json "$IMAGE_URI" > "$SCAN_TMP" || true
trivy fs --scanners vuln,secret,misconfig "$FOLDER_PATH" -f json --timeout 15m > "$SCAN_TMP" || true
```

This means Trivy failures do not immediately fail the script. The script may continue and try to upload output even if Trivy failed.

This is useful when findings should not break the pipeline, but it can hide real scanner failures unless logs are monitored carefully.

### Folder SBOM Command Concern

In folder scan mode, the script currently calls:

```bash
generate_sbom "$FOLDER_PATH" "$SBOM_PATH" "$TIMESTAMP"
```

But `generate_sbom` internally uses:

```bash
trivy image --format cyclonedx --output "$sbom_tmp" "$target"
```

For a folder path, this should generally be:

```bash
trivy fs --format cyclonedx --output "$sbom_tmp" "$target"
```

Otherwise, Trivy may try to interpret the folder path as an image reference.

### Folder Mode S3 Bucket Validation

Folder mode validates only:

- `APP_NAME`
- `SERVICE_NAME`

But upload still requires:

- `S3_BUCKET`
- Valid AWS credentials or another AWS credential provider.

For clearer failures, folder mode should also validate `S3_BUCKET`.

### Metadata Secrets

The script adds `AUTH_TOKEN` into the uploaded JSON:

```json
"api_auth_token": "..."
```

This means the token will be stored in S3 scan result files. Confirm this is intended. If not, remove it from the metadata or replace it with a non-sensitive reference.

### Character Encoding

Some log messages in `entrypoint.sh` display as:

```text
â†’
```

This appears to be a mis-encoded arrow character. It should be replaced with plain ASCII:

```text
->
```

## Recommended Improvements

Recommended changes for production hardening:

- Validate `S3_BUCKET` in folder scan mode.
- Use `trivy fs --format cyclonedx` for folder SBOM generation.
- Avoid uploading `AUTH_TOKEN` unless it is explicitly required.
- Log Trivy failures clearly instead of hiding them completely with `|| true`.
- Support `AWS_SESSION_TOKEN` explicitly in `setup_aws_credentials`.
- Consider using IAM roles instead of static AWS access keys where possible.
- Consider adding a `TRIVY_TIMEOUT` environment variable instead of hard-coding `15m`.
- Consider adding a `TRIVY_CACHE_DIR` volume for faster repeated scans.

## Quick Reference

Build:

```bash
docker build -t osi-sca-image-scanner:latest .
```

Run image scan:

```bash
docker run --rm \
  -e APP_NAME="my-app" \
  -e SERVICE_NAME="my-service" \
  -e BRANCH="main" \
  -e VERSION="1.0.0" \
  -e IMAGE_URI="123456789012.dkr.ecr.us-east-1.amazonaws.com/my-service:1.0.0" \
  -e S3_BUCKET="my-security-scan-results" \
  -e AWS_ACCESS_KEY_ID="AKIA..." \
  -e AWS_SECRET_ACCESS_KEY="..." \
  osi-sca-image-scanner:latest
```

Run folder scan:

```bash
docker run --rm \
  -v /path/on/server/source:/scan-target:ro \
  -e FOLDER_PATH="/scan-target" \
  -e APP_NAME="my-app" \
  -e SERVICE_NAME="my-service" \
  -e S3_BUCKET="my-security-scan-results" \
  -e AWS_ACCESS_KEY_ID="AKIA..." \
  -e AWS_SECRET_ACCESS_KEY="..." \
  -e AWS_DEFAULT_REGION="us-east-1" \
  osi-sca-image-scanner:latest
```

