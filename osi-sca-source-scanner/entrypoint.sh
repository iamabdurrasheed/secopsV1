#!/bin/bash
set -e

# Function to get scanner version
get_scanner_version() {
    local scanner=$1
    case $scanner in
        "trivy")
            trivy --version | grep -oP 'Version: \K[0-9.]+';;
        "grype")
            grype version | grep -oP 'Version:\s+\K[0-9.]+';;
        "osv-scanner")
            osv-scanner --version 2>&1 | grep -oP 'osv-scanner version: \K[0-9.]+';;
        "dependency-check")
            /usr/share/dependency-check/bin/dependency-check.sh --version 2>&1 | grep -oP 'version \K[0-9.]+';;
    esac
}

# === Local folder scan mode ===
if [[ -n "$FOLDER_PATH" ]]; then
    echo "[INFO] Running local combined scan on: $FOLDER_PATH"

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

    WORKDIR="/home/appsecuser"
    TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
    VERSION="manual-folder-scan"
    S3_PATH="$APP_NAME/$SERVICE_NAME/local/$VERSION/osi-sca-source-scanner/$TIMESTAMP.json"
    SCAN_FILE="/tmp/$(basename "$S3_PATH")"

    TRIVY_TMP="/tmp/trivy_result.json"
    GRYPE_TMP="/tmp/grype_result.json"
    OSV_TMP="/tmp/osv_result.json"
    DEPCHECK_TMP="/tmp/depcheck_result.json"

    echo "[INFO] Detecting scanner versions..."
    TRIVY_VER=$(get_scanner_version "trivy")
    GRYPE_VER=$(get_scanner_version "grype")
    OSV_VER=$(get_scanner_version "osv-scanner")
    DEPCHECK_VER=$(get_scanner_version "dependency-check")

    echo "[INFO] Scanner versions - Trivy: $TRIVY_VER, Grype: $GRYPE_VER, OSV: $OSV_VER, Dependency-Check: $DEPCHECK_VER"

    echo "[INFO] Starting all scanners in parallel..."

    (
        echo "[INFO] Running Trivy scanner..."
        trivy fs --scanners vuln,secret,misconfig "$FOLDER_PATH" -f json --timeout 15m > "$TRIVY_TMP" 2>/dev/null || echo '{}' > "$TRIVY_TMP"
        echo "[INFO] Trivy scanner completed"
    ) &
    TRIVY_PID=$!

    (
        echo "[INFO] Running Grype scanner..."
        grype dir:"$FOLDER_PATH" -o json > "$GRYPE_TMP" 2>/dev/null || echo '{}' > "$GRYPE_TMP"
        echo "[INFO] Grype scanner completed"
    ) &
    GRYPE_PID=$!

    (
        echo "[INFO] Running OSV scanner..."
        osv-scanner --no-ignore --recursive "$FOLDER_PATH" -f json > "$OSV_TMP" 2>/dev/null || echo '{}' > "$OSV_TMP"
        echo "[INFO] OSV scanner completed"
    ) &
    OSV_PID=$!

    (
        echo "[INFO] Running Dependency-Check scanner..."
        chmod +w /usr/share/dependency-check/data/ 2>/dev/null || true
        /usr/share/dependency-check/bin/dependency-check.sh --format JSON --scan "$FOLDER_PATH" --out "$WORKDIR" --data /usr/share/dependency-check/data > /dev/null 2>&1 || true
        if [[ -f "$WORKDIR/dependency-check-report.json" ]]; then
            cp "$WORKDIR/dependency-check-report.json" "$DEPCHECK_TMP"
        else
            echo '{}' > "$DEPCHECK_TMP"
        fi
        echo "[INFO] Dependency-Check scanner completed"
    ) &
    DEPCHECK_PID=$!

    echo "[INFO] Waiting for all scanners to complete..."
    wait $TRIVY_PID $GRYPE_PID $OSV_PID $DEPCHECK_PID
    echo "[INFO] All scanners completed"

    echo "[INFO] Merging scan results..."
    jq -n \
        --arg scanner_agent_id "$SCANNER_AGENT_ID" \
        --arg scan_job_id "$SCAN_JOB_ID" \
        --arg app_service_id "$APP_SERVICE_ID" \
        --arg base_url "$BASE_URL" \
        --arg api_auth_token "$AUTH_TOKEN" \
        --arg service_environment_id "$SERVICE_ENVIRONMENT_ID" \
        --arg trivy_ver "$TRIVY_VER" \
        --arg grype_ver "$GRYPE_VER" \
        --arg osv_ver "$OSV_VER" \
        --arg depcheck_ver "$DEPCHECK_VER" \
        --slurpfile trivy "$TRIVY_TMP" \
        --slurpfile grype "$GRYPE_TMP" \
        --slurpfile osv "$OSV_TMP" \
        --slurpfile depcheck "$DEPCHECK_TMP" \
        '{
            metadata: {
                scanner_agent_id: $scanner_agent_id,
                scan_job_id: $scan_job_id,
                app_service_id: $app_service_id,
                base_url: $base_url,
                api_auth_token: $api_auth_token,
                service_environment_id: $service_environment_id
            },
            tools: {
                "dependency-check-source-scanner": {
                    version: $depcheck_ver,
                    results: $depcheck[0]
                },
                "trivy-source-scanner": {
                    version: $trivy_ver,
                    results: $trivy[0]
                },
                "grype-source-scanner": {
                    version: $grype_ver,
                    results: $grype[0]
                },
                "osv-source-scanner": {
                    version: $osv_ver,
                    results: $osv[0]
                }
            }
        }' > "$SCAN_FILE"

    SBOM_S3_PATH="$APP_NAME/$SERVICE_NAME/local/$VERSION/sbom/$TIMESTAMP.json"
    SBOM_FILE="/tmp/sbom_$(basename "$SBOM_S3_PATH")"
    SBOM_TMP="/tmp/sbom_tmp_$TIMESTAMP.json"

    echo "[INFO] Generating SBOM using Trivy"
    trivy fs --format cyclonedx --output "$SBOM_TMP" "$FOLDER_PATH" 2>/dev/null || true

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
           }
           + .
       ' "$SBOM_TMP" > "$SBOM_FILE"

    echo "[INFO] Uploading to Azure Blob..."
    python3 /app/upload_to_blob.py "$SCAN_FILE" "$S3_PATH"
    python3 /app/upload_to_blob.py "$SBOM_FILE" "$SBOM_S3_PATH"
    echo "[INFO] Azure upload completed"

    OUTPUT_DIR="${OUTPUT_DIR:-/home/appsecuser/scan-results}"
    mkdir -p "$OUTPUT_DIR"
    OUTPUT_FILE="$OUTPUT_DIR/osi-sca-scan-result-$TIMESTAMP.json"

    cp "$SCAN_FILE" "$OUTPUT_FILE"
    echo "[INFO] Scan result saved to: $OUTPUT_FILE"

    cp "$SCAN_FILE" "/tmp/osi-sca-scan-result.json"
    echo "[INFO] Also saved to: /tmp/osi-sca-scan-result.json"

    rm -f "$TRIVY_TMP" "$GRYPE_TMP" "$OSV_TMP" "$DEPCHECK_TMP" "$WORKDIR/dependency-check-report.json" "$SCAN_FILE"

    echo "[INFO] Local combined scan complete"
    exit 0
fi

# =======================
# GIT CLONE MODE
# =======================

echo "[INFO] No FOLDER_PATH provided, proceeding with Git repository clone mode"

for var in APP_NAME SERVICE_NAME REPO_URL BRANCH IS_HOSTED_ON_PREM; do
    if [[ -z "${!var}" ]]; then
        echo "[ERROR] $var is not set"
        exit 1
    fi
done

if [[ "$IS_HOSTED_ON_PREM" == "False" ]]; then
    for var in VERSION; do
        if [[ -z "${!var}" ]]; then
            echo "[ERROR] $var is not set"
            exit 1
        fi
    done
    echo "[INFO] Using provided VERSION: $VERSION"
fi

if [[ "$REPO_URL" == *"git-codecommit"* ]]; then
    for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
        if [[ -z "${!var}" ]]; then
            echo "[ERROR] $var is not set."
            exit 1
        fi
    done
fi

export AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY"

WORKDIR="/home/appsecuser"
SUB_DIR="repo"
mkdir -p "$WORKDIR/$SUB_DIR"
cd "$WORKDIR/$SUB_DIR"

MAX_RETRIES=3
RETRY_DELAY=5
COUNT=0

if [[ "$REPO_URL" == *"git-codecommit"* ]]; then
    git config --global credential.helper '!aws codecommit credential-helper $@'
    git config --global credential.UseHttpPath true
fi

echo "[INFO] Cloning repository..."

if [[ "$IS_HOSTED_ON_PREM" == "True" ]]; then
    git config --global http.sslVerify false
fi

until git clone --branch "$BRANCH" "$REPO_URL" .; do
    COUNT=$((COUNT + 1))
    if [ "$COUNT" -ge "$MAX_RETRIES" ]; then
        echo "[ERROR] git clone failed after $MAX_RETRIES"
        exit 1
    fi
    echo "[WARN] retrying clone..."
    sleep "$RETRY_DELAY"
done

VERSION=$(git rev-parse "$BRANCH")
echo "[INFO] Checked out version: $VERSION"
git checkout "$VERSION"

TRIVY_TMP="/tmp/trivy_result.json"
GRYPE_TMP="/tmp/grype_result.json"
OSV_TMP="/tmp/osv_result.json"
DEPCHECK_TMP="/tmp/depcheck_result.json"

echo "[INFO] Detecting scanner versions..."
TRIVY_VER=$(get_scanner_version "trivy")
GRYPE_VER=$(get_scanner_version "grype")
OSV_VER=$(get_scanner_version "osv-scanner")
DEPCHECK_VER=$(get_scanner_version "dependency-check")

echo "[INFO] Versions: Trivy=$TRIVY_VER Grype=$GRYPE_VER OSV=$OSV_VER DependencyCheck=$DEPCHECK_VER"

echo "[INFO] Starting all scanners in parallel..."

(
    echo "[INFO] Running Trivy..."
    trivy fs --scanners vuln,secret,misconfig ./ -f json --timeout 15m > "$TRIVY_TMP" 2>/dev/null || echo '{}' > "$TRIVY_TMP"
    echo "[INFO] Trivy completed"
) &
TRIVY_PID=$!

(
    echo "[INFO] Running Grype..."
    grype dir:. -o json > "$GRYPE_TMP" 2>/dev/null || echo '{}' > "$GRYPE_TMP"
    echo "[INFO] Grype completed"
) &
GRYPE_PID=$!

(
    echo "[INFO] Running OSV..."
    osv-scanner --no-ignore --recursive . -f json > "$OSV_TMP" 2>/dev/null || echo '{}' > "$OSV_TMP"
    echo "[INFO] OSV completed"
) &
OSV_PID=$!

(
    echo "[INFO] Running Dependency-Check..."
    chmod +w /usr/share/dependency-check/data/ 2>/dev/null || true
    /usr/share/dependency-check/bin/dependency-check.sh --format JSON --scan ./ --out "$WORKDIR" --data /usr/share/dependency-check/data > /dev/null 2>&1 || true
    if [[ -f "$WORKDIR/dependency-check-report.json" ]]; then
        cp "$WORKDIR/dependency-check-report.json" "$DEPCHECK_TMP"
    else
        echo '{}' > "$DEPCHECK_TMP"
    fi
    echo "[INFO] Dependency-Check completed"
) &
DEPCHECK_PID=$!

echo "[INFO] Waiting for all scanners to complete..."
wait $TRIVY_PID $GRYPE_PID $OSV_PID $DEPCHECK_PID
echo "[INFO] All scanners completed"

TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
SAFE_BRANCH=$(echo "$BRANCH" | tr '/' '-')
S3_PATH="$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/osi-sca-source-scanner/$TIMESTAMP.json"
SCAN_FILE="/tmp/$(basename "$S3_PATH")"

echo "[INFO] Merging scan results..."
jq -n \
    --arg scanner_agent_id "$SCANNER_AGENT_ID" \
    --arg scan_job_id "$SCAN_JOB_ID" \
    --arg app_service_id "$APP_SERVICE_ID" \
    --arg base_url "$BASE_URL" \
    --arg api_auth_token "$AUTH_TOKEN" \
    --arg service_environment_id "$SERVICE_ENVIRONMENT_ID" \
    --arg trivy_ver "$TRIVY_VER" \
    --arg grype_ver "$GRYPE_VER" \
    --arg osv_ver "$OSV_VER" \
    --arg depcheck_ver "$DEPCHECK_VER" \
    --slurpfile trivy "$TRIVY_TMP" \
    --slurpfile grype "$GRYPE_TMP" \
    --slurpfile osv "$OSV_TMP" \
    --slurpfile depcheck "$DEPCHECK_TMP" \
    '{
        metadata: {
            scanner_agent_id: $scanner_agent_id,
            scan_job_id: $scan_job_id,
            app_service_id: $app_service_id,
            base_url: $base_url,
            api_auth_token: $api_auth_token,
            service_environment_id: $service_environment_id
        },
        tools: {
            "dependency-check-source-scanner": {
                version: $depcheck_ver,
                results: $depcheck[0]
            },
            "trivy-source-scanner": {
                version: $trivy_ver,
                results: $trivy[0]
            },
            "grype-source-scanner": {
                version: $grype_ver,
                results: $grype[0]
            },
            "osv-source-scanner": {
                version: $osv_ver,
                results: $osv[0]
            }
        }
    }' > "$SCAN_FILE"

SBOM_S3_PATH="$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/sbom/$TIMESTAMP.json"
SBOM_FILE="/tmp/sbom_$(basename "$SBOM_S3_PATH")"
SBOM_TMP="/tmp/sbom_tmp_$TIMESTAMP.json"

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
       }
       + .
   ' "$SBOM_TMP" > "$SBOM_FILE"

echo "[INFO] Uploading to Azure Blob..."
python3 /app/upload_to_blob.py "$SCAN_FILE" "$S3_PATH"
python3 /app/upload_to_blob.py "$SBOM_FILE" "$SBOM_S3_PATH"
echo "[INFO] Azure upload completed"

OUTPUT_DIR="${OUTPUT_DIR:-/home/appsecuser/scan-results}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_FILE="$OUTPUT_DIR/osi-sca-scan-result-$TIMESTAMP.json"

cp "$SCAN_FILE" "$OUTPUT_FILE"
echo "[INFO] Scan result saved to: $OUTPUT_FILE"

# Save to structured /tmp path: /tmp/<app>/<service>/<branch>/<commit>/osi-sca-source-scanner/<timestamp>.json
STRUCTURED_DIR="/tmp/$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/osi-sca-source-scanner"
mkdir -p "$STRUCTURED_DIR"
cp "$SCAN_FILE" "$STRUCTURED_DIR/$TIMESTAMP.json"
echo "[INFO] Structured result saved to: $STRUCTURED_DIR/$TIMESTAMP.json"

# Save SBOM to structured path
SBOM_STRUCTURED_DIR="/tmp/$APP_NAME/$SERVICE_NAME/$SAFE_BRANCH/$VERSION/sbom"
mkdir -p "$SBOM_STRUCTURED_DIR"
cp "$SBOM_FILE" "$SBOM_STRUCTURED_DIR/$TIMESTAMP.json"
echo "[INFO] Structured SBOM saved to: $SBOM_STRUCTURED_DIR/$TIMESTAMP.json"

rm -f "$TRIVY_TMP" "$GRYPE_TMP" "$OSV_TMP" "$DEPCHECK_TMP" "$WORKDIR/dependency-check-report.json" "$SCAN_FILE"

echo "[INFO] Git-based combined scan complete"
