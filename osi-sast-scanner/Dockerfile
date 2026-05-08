# ===========================
# Builder Stage: Gitleaks
# ===========================
FROM debian:bookworm-slim AS gitleaks-builder

ARG GITLEAKS_VERSION=8.26.0

RUN set -e && \
    apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates tar && \
    rm -rf /var/lib/apt/lists/* && \
    wget -O /tmp/gitleaks.tar.gz "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" && \
    mkdir -p /gitleaks && \
    tar -xzf /tmp/gitleaks.tar.gz -C /gitleaks gitleaks && \
    rm -rf /tmp/*


# ===========================
# Builder Stage: TruffleHog
# ===========================
FROM debian:bookworm-slim AS trufflehog-builder

ARG TRUFFLEHOG_VERSION=3.90.5

RUN set -e && \
    apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates tar && \
    rm -rf /var/lib/apt/lists/* && \
    wget -O /tmp/trufflehog.tar.gz "https://github.com/trufflesecurity/trufflehog/releases/download/v${TRUFFLEHOG_VERSION}/trufflehog_${TRUFFLEHOG_VERSION}_linux_amd64.tar.gz" && \
    mkdir -p /trufflehog && \
    tar -xzf /tmp/trufflehog.tar.gz -C /trufflehog trufflehog && \
    rm -rf /tmp/*


# ===========================
# Builder Stage: Semgrep
# ===========================
FROM debian:bookworm-slim AS semgrep-builder

ARG SEMGREP_VERSION=1.131.0

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir "semgrep==${SEMGREP_VERSION}"


# ===========================
# Builder Stage: Trivy (for SBOM generation)
# ===========================
FROM debian:bookworm-slim AS trivy-builder

ARG TRIVY_VERSION=0.70.0

RUN set -e && \
    apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates gnupg tar && \
    rm -rf /var/lib/apt/lists/* && \
    wget -O /tmp/trivy.tar.gz "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" && \
    wget -O /tmp/trivy_checksums.txt "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_checksums.txt" && \
    cd /tmp && \
    grep "trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" trivy_checksums.txt | awk '{print $1}' > expected_checksum.txt && \
    sha256sum trivy.tar.gz | awk '{print $1}' > actual_checksum.txt && \
    cmp -s expected_checksum.txt actual_checksum.txt && \
    mkdir -p /trivy && \
    tar -xzf trivy.tar.gz -C /trivy trivy && \
    rm -rf /tmp/*


# ===========================
# Final Stage
# ===========================
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl unzip ca-certificates jq python3 python3-pip && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip" && \
    unzip /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    pip3 install --no-cache-dir azure-storage-blob --break-system-packages && \
    rm -rf /var/lib/apt/lists/* /tmp/*

RUN useradd -r -m appsecuser

COPY --from=gitleaks-builder /gitleaks/gitleaks /usr/local/bin/gitleaks
COPY --from=trufflehog-builder /trufflehog/trufflehog /usr/local/bin/trufflehog
COPY --from=semgrep-builder /opt/venv /opt/venv
COPY --from=trivy-builder /trivy/trivy /usr/local/bin/trivy

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY blob_storage.py /app/blob_storage.py
COPY upload_to_blob.py /app/upload_to_blob.py

# Fix line endings in case file was edited on Windows
RUN sed -i 's/\r//' /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/gitleaks /usr/local/bin/trufflehog /usr/local/bin/trivy && \
    chown -R appsecuser:appsecuser /opt/venv

# PATH and Python venv
ENV PATH="/opt/venv/bin:$PATH"
ENV VIRTUAL_ENV=/opt/venv

# Disable TruffleHog auto-update COMPLETELY
ENV TRUFFLEHOG_DISABLE_UPDATE=true
ENV TRUFFLEHOG_NO_UPDATE=true
ENV TRUFFLEHOG_UPDATER_ENABLED=false
ENV TRUFFLEHOG_SKIP_UPDATE=true

USER appsecuser
WORKDIR /home/appsecuser

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
