#!/bin/bash
set -e

# ===========================
# Global config
# ===========================
export TRUFFLEHOG_DISABLE_UPDATE=true
export TRUFFLEHOG_NO_UPDATE=true
export TRUFFLEHOG_UPDATER_ENABLED=false
export TRUFFLEHOG_SKIP_UPDATE=true

# ===========================
# Functions
# ===========================

setup_vcs_auth() {
    # Only do special handling for AWS CodeCommit
    if [[ "$REPO_URL" == *"git-codecommit"* ]]; then
        echo "[INFO] Detected AWS CodeCommit repository, configuring credential helper"

        # Validate AWS credentials presence
        for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
            if [[ -z "${!var}" ]]; then
                echo "[ERROR] $var is not set but required for CodeCommit"
                exit 1
            fi
        done

        # Try to infer region from REPO_URL if AWS_DEFAULT_REGION is not set
        if [[ -z "$AWS_DEFAULT_REGION" ]]; then
            if [[ "$REPO_URL" =~ codecommit\.([a-z0-9-]+)\.amazonaws\.com ]]; then
                AWS_DEFAULT_REGION="${BASH_REMATCH[1]}"
            else
                AWS_DEFAULT_REGION="us-east-1"
            fi
        fi
        export AWS_DEFAULT_REGION

        git config --global credential.helper '!aws codecommit credential-helper $@'
        git config --global credential.UseHttpPath true
    else
        echo "[INFO] Non-CodeCommit repository detected, using IAM role for AWS operations"
    fi
}

run_gitleaks() {
    TMP_GITLEAKS=$(mktemp)
    echo "[INFO] Running Gitleaks"

    GITLEAKS_VERSION=$(gitleaks version 2>/dev/null || echo "unknown")
    gitleaks dir --report-format json --report-path "$TMP_GITLEAKS" || true

    [[ ! -s "$TMP_GITLEAKS" ]] && echo "[]" > "$TMP_GITLEAKS"
    GITLEAKS_RESULTS=$(cat "$TMP_GITLEAKS")
}

run_trufflehog() {
    TMP_TRUFFLE=$(mktemp)
    echo "[INFO] Running TruffleHog"

    TRUFFLEHOG_VERSION=$(trufflehog --version 2>/dev/null || echo "unknown")

    # IMPORTANT: Disable auto update
    trufflehog filesystem . --json --no-update > "$TMP_TRUFFLE" || true

    [[ ! -s "$TMP_TRUFFLE" ]] && echo "[]" > "$TMP_TRUFFLE"
    TRUFFLEHOG_RESULTS=$(jq -s '.' "$TMP_TRUFFLE")
}

run_semgrep() {
    TMP_SEMGREP=$(mktemp)
    echo "[INFO] Running Semgrep"

    SEMGREP_VERSION=$(semgrep --version 2>/dev/null || echo "unknown")

    semgrep scan --config auto --json > "$TMP_SEMGREP" || true

    [[ ! -s "$TMP_SEMGREP" ]] && echo '{}' > "$TMP_SEMGREP"
    SEMGREP_RESULTS=$(cat "$TMP_SEMGREP")
}

# ===========================
# Metadata
# ===========================

build_metadata_json() {
cat <<EOF
{
  "scanner_agent_id": "$SCANNER_AGENT_ID",
  "scan_job_id": "$SCAN_JOB_ID",
  "app_service_id": "$APP_SERVICE_ID",
  "base_url": "$BASE_URL",
  "api_auth_token": "$AUTH_TOKEN",
  "service_environment_id": "$SERVICE_ENVIRONMENT_ID"
}
EOF
}

# ===========================
# Final JSON Build
# ===========================

build_final_json() {
cat <<EOF
{
  "metadata": $(build_metadata_json),

  "tools": {
    "gitleaks": {
      "version": "$GITLEAKS_VERSION",
      "results": $GITLEAKS_RESULTS
    },
    "trufflehog": {
      "version": "$TRUFFLEHOG_VERSION",
      "results": $TRUFFLEHOG_RESULTS
    },
    "semgrep": {
      "version": "$SEMGREP_VERSION",
      "results": $SEMGREP_RESULTS
    }
  }
}
EOF
}

# ===========================
# Repo / folder preparation
# ===========================

prepare_repo_if_needed() {
    # --- Local folder mode (no git clone) ---
    if [[ -n "$FOLDER_PATH" ]]; then
        echo "[INFO] Using local folder: $FOLDER_PATH"

        # Validate required env for local scans
        for var in APP_NAME SERVICE_NAME; do
            if [[ -z "${!var}" ]]; then
                echo "[ERROR] $var is not set"
                exit 1
            fi
        done

        if [[ ! -d "$FOLDER_PATH" ]]; then
            echo "[ERROR] Folder path $FOLDER_PATH does not exist"
            exit 1
        fi

        cd "$FOLDER_PATH"
        return
    fi

    # --- Git clone mode ---
    echo "[INFO] No FOLDER_PATH provided, proceeding with Git repository clone mode"

    # Validate required vars for git mode
    for var in APP_NAME SERVICE_NAME REPO_URL BRANCH IS_HOSTED_ON_PREM; do
      if [[ -z "${!var}" ]]; then
        echo "[ERROR] $var is not set"
        exit 1
      fi
    done

    # Setup auth only if needed (CodeCommit)
    setup_vcs_auth

    WORKDIR="/home/appsecuser/repo"
    mkdir -p "$WORKDIR"
    cd "$WORKDIR"

    if [[ "$IS_HOSTED_ON_PREM" == "True" ]]; then
        echo "[WARN] Disabling SSL verification for on-prem git"
        git config --global http.sslVerify false
    fi

    MAX_RETRIES=3
    COUNT=0

    echo "[INFO] Cloning git repo..."
    until git clone --branch "$BRANCH" "$REPO_URL" .; do
        COUNT=$((COUNT + 1))
        if [[ "$COUNT" -ge "$MAX_RETRIES" ]]; then
            echo "[ERROR] git clone failed after $MAX_RETRIES attempts"
            exit 1
        fi
        echo "[WARN] Retry git clone... ($COUNT/$MAX_RETRIES)"
        sleep 5
    done

    # Checkout to specific commit
    VERSION=$(git rev-parse "$BRANCH")
    echo "[INFO] Checked out version: $VERSION"
    git checkout "$VERSION"
}

# ===========================
# Main execution
# ===========================

echo "[INFO] Preparing environment"
prepare_repo_if_needed

echo "[INFO] Running all scanners"
run_gitleaks
run_trufflehog
run_semgrep

echo "[INFO] Building merged JSON"

TIMESTAMP=$(date +"%Y%m%d-%H%M%S")

# Decide VERSION + S3 paths based on local vs git mode
if [[ -n "$FOLDER_PATH" ]]; then
    VERSION="manual-folder-scan"
    SAST_S3_PATH="$APP_NAME/$SERVICE_NAME/local/$VERSION/osi-sast-scanner/$TIMESTAMP.json"
    SBOM_S3_PATH="$APP_NAME/$SERVICE_NAME/local/$VERSION/sbom/$TIMESTAMP.json"
else
    VERSION=$(git rev-parse HEAD)
    # Sanitize branch name: replace / with - for S3 path compatibility
    SAFE_BRANCH=$(echo "$BRANCH" | tr '/' '-')
    SAST_S3_PATH="$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/osi-sast-scanner/$TIMESTAMP.json"
    SBOM_S3_PATH="$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/sbom/$TIMESTAMP.json"
fi

FINAL_FILE="/tmp/osi-sast-$TIMESTAMP.json"
SBOM_FILE="/tmp/sbom-$TIMESTAMP.json"
SBOM_TMP="/tmp/sbom-tmp-$TIMESTAMP.json"

build_final_json > "$FINAL_FILE"

echo "[INFO] Uploading SAST results to Azure Blob: $SAST_S3_PATH"
python3 /app/upload_to_blob.py "$FINAL_FILE" "$SAST_S3_PATH"

echo "[INFO] Generating SBOM using Trivy"
trivy fs --format cyclonedx --output "$SBOM_TMP" . 2>/dev/null || true

echo "[INFO] Adding metadata to SBOM"
jq --arg scanner_agent_id "$SCANNER_AGENT_ID" \
   --arg scan_job_id "$SCAN_JOB_ID" \
   --arg app_service_id "$APP_SERVICE_ID" \
   --arg base_url "$BASE_URL" \
   --arg api_auth_token "$AUTH_TOKEN" \
   --arg service_environment_id "$SERVICE_ENVIRONMENT_ID" \
   '
     del(.metadata)
     | {
         metadata: {
             scanner_agent_id: $scanner_agent_id,
             scan_job_id: $scan_job_id,
             app_service_id: $app_service_id,
             base_url: $base_url,
             api_auth_token: $api_auth_token,
             service_environment_id: $service_environment_id
         }
       } + .
   ' "$SBOM_TMP" > "$SBOM_FILE"

echo "[INFO] Uploading SBOM to Azure Blob: $SBOM_S3_PATH"
python3 /app/upload_to_blob.py "$SBOM_FILE" "$SBOM_S3_PATH"

# Save to structured /tmp path: /tmp/<app>/<service>/<branch>/<commit>/osi-sast-scanner/<timestamp>.json
STRUCTURED_DIR="/tmp/$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/osi-sast-scanner"
mkdir -p "$STRUCTURED_DIR"
cp "$FINAL_FILE" "$STRUCTURED_DIR/$TIMESTAMP.json"
echo "[INFO] Structured result saved to: $STRUCTURED_DIR/$TIMESTAMP.json"

# Save SBOM to structured path
SBOM_STRUCTURED_DIR="/tmp/$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/sbom"
mkdir -p "$SBOM_STRUCTURED_DIR"
cp "$SBOM_FILE" "$SBOM_STRUCTURED_DIR/$TIMESTAMP.json"
echo "[INFO] Structured SBOM saved to: $SBOM_STRUCTURED_DIR/$TIMESTAMP.json"

echo "[INFO] Completed. SAST results and SBOM uploaded."
