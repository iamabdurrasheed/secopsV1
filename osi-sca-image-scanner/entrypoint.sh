#!/bin/bash
set -e

# ==========================
# FUNCTIONS
# ==========================

# Inject metadata into JSON
inject_metadata() {
    local input_file="$1"
    local output_file="$2"
    local metadata_type="$3"

    jq --arg scanner_agent_id "$SCANNER_AGENT_ID" \
       --arg scan_job_id "$SCAN_JOB_ID" \
       --arg app_service_id "$APP_SERVICE_ID" \
       --arg base_url "$BASE_URL" \
       --arg api_auth_token "$AUTH_TOKEN" \
       --arg service_environment_id "$SERVICE_ENVIRONMENT_ID" \
       '
         (if "'"$metadata_type"'" == "sbom" then
            del(.metadata)
          else
            .
          end)
          |
          { metadata: {
                scanner_agent_id: $scanner_agent_id,
                scan_job_id: $scan_job_id,
                app_service_id: $app_service_id,
                base_url: $base_url,
                api_auth_token: $api_auth_token,
                service_environment_id: $service_environment_id
            }} + .
       ' "$input_file" > "$output_file"
}

# Generate SBOM using Trivy (REGISTRY AUTH REQUIRED for ECR)
generate_sbom() {
    local target="$1"
    local sbom_path="$2"
    local timestamp="$3"

    local sbom_tmp="/tmp/sbom_tmp_$timestamp.json"
    local sbom_file="/tmp/sbom_$timestamp.json"

    echo "[INFO] Generating SBOM using Trivy for: $target"
    trivy image --format cyclonedx --output "$sbom_tmp" "$target" 2>/dev/null || true

    echo "[INFO] Adding metadata to SBOM"
    inject_metadata "$sbom_tmp" "$sbom_file" "sbom"

    echo "[INFO] Uploading SBOM to Azure Blob: $sbom_path"
    python3 /app/upload_to_blob.py "$sbom_file" "$sbom_path"

    rm -f "$sbom_tmp" "$sbom_file"
}

# Upload scan results
upload_scan_results() {
    local scan_tmp="$1"
    local scan_path="$2"
    local timestamp="$3"

    local scan_file="/tmp/scan_$timestamp.json"

    echo "[INFO] Adding metadata to scan results"
    inject_metadata "$scan_tmp" "$scan_file" "scan"

    echo "[INFO] Uploading scan results to Azure Blob: $scan_path"
    python3 /app/upload_to_blob.py "$scan_file" "$scan_path"

    rm -f "$scan_tmp" "$scan_file"
}

# Setup AWS creds (for IMAGE MODE / ECR access)
setup_aws_credentials() {
    if [[ -n "$AWS_ACCESS_KEY_ID" && -n "$AWS_SECRET_ACCESS_KEY" ]]; then
        export AWS_ACCESS_KEY_ID
        export AWS_SECRET_ACCESS_KEY

        if [[ -n "$IMAGE_URI" ]]; then
            ECR_REGION=$(echo "$IMAGE_URI" | cut -d'.' -f4)
            export AWS_DEFAULT_REGION=$ECR_REGION
        fi

        # REQUIRED for AWS Go SDK (Trivy uses this)
        export AWS_SDK_LOAD_CONFIG=1
        export AWS_EC2_METADATA_DISABLED=true
    fi
}


cleanup_aws_credentials() {
    unset AWS_ACCESS_KEY_ID
    unset AWS_SECRET_ACCESS_KEY
    unset AWS_SESSION_TOKEN
}

# ==========================
# FOLDER SCAN MODE
# ==========================

if [[ -n "$FOLDER_PATH" ]]; then
    echo "[INFO] Running LOCAL FOLDER SCAN at: $FOLDER_PATH"

    for var in APP_NAME SERVICE_NAME; do
        if [[ -z "${!var}" ]]; then
            echo "[ERROR] $var is not set"
            exit 1
        fi
    done

    if [[ ! -d "$FOLDER_PATH" ]]; then
        echo "[ERROR] Folder does not exist: $FOLDER_PATH"
        exit 1
    fi

    TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
    VERSION="manual-folder-scan"

    # === TRIVY FILESYSTEM SCAN ===
    SCAN_PATH="$APP_NAME/$SERVICE_NAME/hosted-repo/$VERSION/osi-sca-image-scanner/$TIMESTAMP.json"
    SCAN_TMP="/tmp/scan_tmp_$TIMESTAMP.json"

    echo "[INFO] Running Trivy filesystem scan..."
    trivy fs --scanners vuln,secret,misconfig "$FOLDER_PATH" \
        -f json --timeout 15m > "$SCAN_TMP" || true

    upload_scan_results "$SCAN_TMP" "$SCAN_PATH" "$TIMESTAMP"

    # === SBOM GENERATION ===
    SBOM_PATH="$APP_NAME/$SERVICE_NAME/hosted-repo/$VERSION/sbom/$TIMESTAMP.json"

    generate_sbom "$FOLDER_PATH" "$SBOM_PATH" "$TIMESTAMP"

    echo "[INFO] Folder scan completed successfully."
    exit 0
fi

# ==========================
# IMAGE SCAN MODE
# ==========================

echo "[INFO] Running IMAGE SCAN MODE"

for var in APP_NAME SERVICE_NAME BRANCH VERSION IMAGE_URI; do
  if [[ -z "${!var}" ]]; then
    echo "[ERROR] $var is not set"
    exit 1
  fi
done

if [[ "$IMAGE_URI" == *".dkr.ecr."* ]]; then
  for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
    if [[ -z "${!var}" ]]; then
      echo "[ERROR] $var is required for ECR images"
      exit 1
    fi
  done
fi

setup_aws_credentials

TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
# Sanitize branch name: replace / with - for S3 path compatibility
SAFE_BRANCH=$(echo "$BRANCH" | tr '/' '-')

# === TRIVY IMAGE SCAN ===
SCAN_PATH="$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/osi-sca-image-scanner/$TIMESTAMP.json"
SCAN_TMP="/tmp/scan_tmp_$TIMESTAMP.json"

echo "[INFO] Running Trivy IMAGE scan..."
trivy image --format json "$IMAGE_URI" > "$SCAN_TMP" || true

# Upload scan results (AWS creds MUST remain available)
upload_scan_results "$SCAN_TMP" "$SCAN_PATH" "$TIMESTAMP"

# === SBOM (Trivy uses AWS SDK for ECR authentication) ===
SBOM_PATH="$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/sbom/$TIMESTAMP.json"

# ECR configuration for Trivy
echo "[INFO] Configuring ECR authentication for Trivy..."
ECR_REGION=$(echo "$IMAGE_URI" | cut -d'.' -f4)
ECR_REGISTRY=$(echo "$IMAGE_URI" | cut -d'/' -f1)

# Trivy uses AWS SDK for ECR authentication automatically when AWS creds are set
export AWS_REGION="$ECR_REGION"
export AWS_SDK_LOAD_CONFIG=1

echo "[INFO] ECR authentication configured for Trivy (using AWS credentials)"

generate_sbom "$IMAGE_URI" "$SBOM_PATH" "$TIMESTAMP"

# Save to structured /tmp path: /tmp/<app>/<service>/<branch>/<version>/osi-sca-image-scanner/<timestamp>.json
STRUCTURED_DIR="/tmp/$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/osi-sca-image-scanner"
mkdir -p "$STRUCTURED_DIR"
[ -f "$SCAN_TMP" ] && cp "$SCAN_TMP" "$STRUCTURED_DIR/$TIMESTAMP.json" && echo "[INFO] Structured result saved to: $STRUCTURED_DIR/$TIMESTAMP.json"

echo "[INFO] Image scan + SBOM generation completed successfully."
