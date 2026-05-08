# Technical Guide

This document explains the two files in this folder in simple words:

- [Dockerfile](Dockerfile)
- [entrypoint.sh](entrypoint.sh)

The goal of the whole project is to build one container that can run several source-code security scanners, combine their results, and save or upload the final report.

## Big Picture

Think of the Docker image as a prebuilt toolbox.

- The `Dockerfile` builds the toolbox.
- The `entrypoint.sh` script decides what to do when the container starts.

Inside the container, the tools scan a codebase using different scanners:

- Trivy
- Grype
- OSV Scanner
- OWASP Dependency-Check

The script can work in two modes:

- scan a local folder that is already mounted into the container
- clone a Git repository and scan that clone

After scanning, it merges the results into one JSON file and, if configured, uploads the output to S3.

## File 1: Dockerfile

### What it does

The `Dockerfile` builds the final Docker image that contains:

- Trivy
- Grype
- OSV Scanner
- OWASP Dependency-Check
- AWS CLI
- Git, curl, unzip, jq, Java runtime, and other helper tools
- the `entrypoint.sh` script that starts the scan

### Why it exists

Each scanner normally comes from a different source and may need different setup steps. The Dockerfile packages them together so the user only needs one container image instead of installing each tool by hand.

This is useful because:

- the environment stays consistent every time
- the same versions can be reused across runs
- the scan can be run in CI/CD, locally, or in a cloud task
- the user does not need to install the scanners on their machine

### How it works

The Dockerfile is split into several stages.

#### 1. Trivy builder stage

This stage starts from `debian:bookworm-slim` and downloads a specific Trivy release.

It does the following:

- installs basic download tools like `wget`, `ca-certificates`, `gnupg`, and `tar`
- downloads the Trivy tarball
- downloads the Trivy checksum file
- compares the downloaded file checksum against the expected checksum
- extracts the `trivy` binary into a temporary folder

The checksum check is important because it helps confirm the download was not corrupted or tampered with.

#### 2. Grype builder stage

This stage is similar to the Trivy stage.

It:

- downloads the Grype release archive
- downloads the checksum file
- verifies the checksum
- extracts the `grype` binary

#### 3. OSV builder stage

This stage downloads the OSV Scanner binary directly.

It:

- installs `wget` and `ca-certificates`
- downloads the `osv-scanner` executable
- makes it runnable with `chmod +x`
- moves it into a folder for later copying

#### 4. Dependency-Check base stage

This stage starts from the official `owasp/dependency-check-action:latest` image.

Instead of downloading Dependency-Check manually, it reuses the official image as a source for the tool and its files.

#### 5. Final image stage

This is the image the user actually runs.

It installs system packages needed at runtime:

- `git` for cloning repositories
- `curl` and `unzip` for AWS CLI install
- `ca-certificates` for secure downloads
- `jq` for JSON merging
- `procps` for process utilities
- `openjdk-17-jre-headless` because Dependency-Check needs Java

Then it installs AWS CLI.

After that it copies the scanner binaries from the earlier stages into `/usr/local/bin`:

- `trivy`
- `grype`
- `osv-scanner`
- Dependency-Check files

Then it creates a non-root user named `appsecuser`, copies in `entrypoint.sh`, gives the scripts executable permission, and switches to the non-root user for safer runtime execution.

### Important design idea

The Dockerfile uses multi-stage builds.

That means each tool is downloaded in its own temporary stage, then only the final needed binaries are copied into the final image. This keeps the final image smaller and cleaner than installing everything in one layer.

## File 2: entrypoint.sh

### What it does

This script is the container’s main startup logic.

When the container starts, Docker runs this script automatically. The script checks environment variables, decides which scan mode to use, runs the scanners, merges the results, and handles output.

### Why it exists

The Docker image needs one clear starting point.

This script makes the image flexible because the same container can:

- scan a mounted folder
- clone a Git repo and scan it
- upload to S3 when configured
- save a local JSON report every time

It also reduces the need for separate images or separate scripts for each scanner.

### How it works, step by step

#### 1. Script setup

The script starts with:

- `#!/bin/bash` so it runs in Bash
- `set -e` so it stops if a command fails unexpectedly

It defines a helper function called `get_scanner_version`.

That function runs each scanner’s version command and extracts the version number.

Why this matters:

- the final report includes which scanner version produced the results
- version tracking helps with debugging and audit trails

#### 2. Local folder scan mode

If `FOLDER_PATH` is set, the script enters local scan mode.

This means the user already has code inside a folder and wants the container to scan that folder directly.

The script then:

- checks that `APP_NAME` and `SERVICE_NAME` are set
- checks that the folder exists
- builds output paths using the current timestamp
- creates temporary files for each scanner’s JSON output
- detects scanner versions
- runs all scanners in parallel
- waits for all scanners to finish
- merges all scanner results into one JSON file using `jq`
- generates an SBOM using Trivy
- adds metadata to the SBOM
- uploads both files to S3 if `S3_BUCKET` is set
- always saves a local copy of the merged result
- cleans up temporary files

##### Why run scanners in parallel

Running the scanners at the same time saves time. Each scanner works independently, so there is no reason to wait for one to finish before starting the next.

##### Why use temporary files

Each scanner writes to its own temporary JSON file so their outputs do not overwrite each other. After all scans finish, `jq` combines them into one final report.

##### Why use `jq`

`jq` is used because the scanner outputs are JSON. It makes it easy to build a single final JSON document with shared metadata plus one section per scanner.

##### Why generate an SBOM

An SBOM, or Software Bill of Materials, is a structured list of software components in the scanned project. This helps teams understand what packages are present and where risk may exist.

#### 3. Git clone mode

If `FOLDER_PATH` is not set, the script switches to Git mode.

This mode is for scanning a repository directly from a Git URL.

The script then:

- checks that required variables are present, such as `APP_NAME`, `SERVICE_NAME`, `REPO_URL`, `BRANCH`, `IS_HOSTED_ON_PREM`, and `S3_BUCKET`
- checks `VERSION` when the repo is not hosted on-premises
- checks AWS keys when the repository is a CodeCommit repo
- sets up Git credentials if needed for CodeCommit
- disables SSL verification for on-prem repositories when required
- clones the repository with retries
- checks out the selected commit or version
- runs the same scanners in parallel
- merges the results into JSON
- creates an SBOM
- uploads results to S3 if configured
- saves the final JSON locally

### The main variables and what they mean

Here is the plain-English meaning of the important environment variables:

- `FOLDER_PATH`: scan this local folder instead of cloning Git
- `APP_NAME`: application name used in output paths
- `SERVICE_NAME`: service name used in output paths
- `REPO_URL`: Git repository URL to clone
- `BRANCH`: branch name to scan
- `VERSION`: version label used in report paths
- `IS_HOSTED_ON_PREM`: tells the script whether the repo is internal/on-prem or not
- `S3_BUCKET`: bucket name for uploads
- `SCANNER_AGENT_ID`, `SCAN_JOB_ID`, `APP_SERVICE_ID`, `BASE_URL`, `AUTH_TOKEN`, `SERVICE_ENVIRONMENT_ID`: metadata copied into the final JSON
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`: AWS credentials when needed
- `OUTPUT_DIR`: optional local output directory

### What each scanner contributes

- Trivy: scans for vulnerabilities, secrets, and misconfigurations
- Grype: scans the directory for vulnerabilities
- OSV Scanner: checks package data against OSV advisories
- Dependency-Check: inspects dependencies for known issues

Each scanner has different strengths, so combining them gives broader coverage.

### Why the script merges results instead of keeping separate files

One final file is easier to store, upload, and process later.

It also makes downstream systems simpler because they only need to read one JSON document instead of four separate scanner outputs.

### Why the script creates local output even when uploading to S3

Local output is useful for:

- debugging
- quick access inside the container
- fallback storage if upload fails later
- review by other tools in the pipeline

### Why there are two scan paths

The project supports two workflows because teams work in two common ways:

- they already have source code mounted in a folder
- they only have a Git repository URL and want the container to clone it

Supporting both makes the container more flexible without needing separate images.

## End-to-End Flow

Here is the full flow in simple terms:

1. Docker builds the image and packs in all scanner tools.
2. The container starts and runs `entrypoint.sh`.
3. The script checks whether it should scan a local folder or a Git repo.
4. It runs Trivy, Grype, OSV Scanner, and Dependency-Check in parallel.
5. It waits for all scanners to finish.
6. It merges the results into one JSON report.
7. It generates an SBOM.
8. It adds metadata to the output.
9. It uploads the files to S3 if configured.
10. It saves a local copy for convenience.

## In Simple Words

If you want the shortest possible explanation:

- the `Dockerfile` builds a ready-to-use security scanning container
- the `entrypoint.sh` script tells that container what to do when it starts
- the container scans code with multiple tools
- the results are combined into one report
- the report can be saved locally and uploaded to S3

## Notes

- The current design assumes all required environment variables are provided by the caller.
- The script is written to keep going even if one scanner fails, because each scanner writes a fallback empty JSON object when needed.
- The dependency-check data directory permissions are adjusted at runtime so the non-root user can write to it.

## File Summary

### Dockerfile

Builds the container image and installs all required tools.

### entrypoint.sh

Runs the scans, combines the results, generates the SBOM, and handles output.

## Example Scenarios: Testing on a Linux Server

These examples show common ways to build and run the container on a Linux machine. Replace placeholder values (like `my-app`, `/path/to/code`, `s3-bucket-name`, or `https://git.example/repo.git`) with real values for your environment.

Prerequisites:

- Docker installed on the host (or Podman with Docker CLI compatibility)
- Enough disk space and network access for downloading scanner binaries
- If uploading to S3 or using CodeCommit, valid AWS credentials or an appropriate IAM role

Build the image (run in the folder containing the `Dockerfile`):

```bash
docker build -t osi-sca-source-scanner:latest .
```

1) Local folder scan (folder mounted into container)

This runs the container and scans a local directory mounted to `/scan` inside the container. The script will detect `FOLDER_PATH` and run local scan mode.

```bash
docker run --rm \
	-e APP_NAME=my-app \
	-e SERVICE_NAME=my-service \
	-e FOLDER_PATH=/scan \
	-e SCANNER_AGENT_ID=agent-1 \
	-e SCAN_JOB_ID=job-1 \
	-v /path/to/code:/scan:ro \
	osi-sca-source-scanner:latest
```

Notes:
- Mount the code path read-only to avoid accidental changes (`:ro`).
- If scanners need write access (e.g., dependency-check data), remove `:ro` or mount a writable data directory and ensure permissions match `appsecuser`.

2) Git clone mode with S3 upload (provide AWS creds)

Use this when you want the container to clone a remote repo, run the scans, and upload results to S3. Provide AWS environment variables so the container can upload.

```bash
docker run --rm \
	-e APP_NAME=my-app \
	-e SERVICE_NAME=my-service \
	-e REPO_URL=https://github.com/example/repo.git \
	-e BRANCH=main \
	-e IS_HOSTED_ON_PREM=False \
	-e S3_BUCKET=s3-bucket-name \
	-e AWS_ACCESS_KEY_ID=AKIA... \
	-e AWS_SECRET_ACCESS_KEY=SECRET... \
	-e AWS_REGION=us-east-1 \
	osi-sca-source-scanner:latest
```

Notes:
- When `IS_HOSTED_ON_PREM=False`, the script will unset AWS creds before uploading (designed for ECS task roles). On a plain Docker host, keep `IS_HOSTED_ON_PREM=True` if you need to use the provided AWS env vars for uploads.

3) CodeCommit repo clone example (HTTPS, needs AWS creds and CodeCommit helper)

Set AWS credentials; the script will configure Git to use the AWS CodeCommit credential helper when the URL contains `git-codecommit`.

```bash
docker run --rm \
	-e APP_NAME=my-app \
	-e SERVICE_NAME=my-service \
	-e REPO_URL=https://git-codecommit.us-east-1.amazonaws.com/v1/repos/my-repo \
	-e BRANCH=main \
	-e IS_HOSTED_ON_PREM=True \
	-e S3_BUCKET=s3-bucket-name \
	-e AWS_ACCESS_KEY_ID=AKIA... \
	-e AWS_SECRET_ACCESS_KEY=SECRET... \
	osi-sca-source-scanner:latest
```

4) Docker Compose quick example

Save the following as `docker-compose.yml` next to your `Dockerfile` for local testing purposes:

```yaml
version: '3.7'
services:
	scanner:
		image: osi-sca-source-scanner:latest
		build: .
		environment:
			- APP_NAME=my-app
			- SERVICE_NAME=my-service
			- FOLDER_PATH=/scan
		volumes:
			- /path/to/code:/scan:ro
```

Run with:

```bash
docker compose up --build --remove-orphans
```

5) Troubleshooting tips on Linux

- Permission denied when mounting volumes: ensure the mounted path is readable by the container user. You can change owner or add a writable mount for dependency-check data.

	```bash
	sudo chown -R 1000:1000 /path/to/code
	```

- Inspect logs from the container to debug scanner behavior:

	```bash
	docker logs <container-id>
	```

- If an individual scanner fails, the script writes a fallback empty JSON object and continues; check the per-scanner temporary files in `/tmp` inside the container for more details.

6) Quick smoke test (run without uploads)

Run a repo scan but do not set `S3_BUCKET`. This will create local output in `/home/appsecuser/scan-results` inside the image filesystem; bind-mount that path to inspect results on the host:

```bash
docker run --rm \
	-e APP_NAME=my-app \
	-e SERVICE_NAME=my-service \
	-e REPO_URL=https://github.com/example/repo.git \
	-e BRANCH=main \
	-e IS_HOSTED_ON_PREM=True \
	-v $(pwd)/scan-results:/home/appsecuser/scan-results \
	osi-sca-source-scanner:latest
```

After the run, check `./scan-results` on the host for `osi-sca-scan-result-<timestamp>.json`.

---

If you want, I can also add a small `README.md` with these steps and an example `docker-compose.yml` file saved into the repository. Which format do you prefer next? (short README, or full step-by-step runbook?)