# ----------- Builder Stage for Trivy -----------
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

# ----------- Builder Stage for Grype -----------
FROM debian:bookworm-slim AS grype-builder

ARG GRYPE_VERSION=0.112.0

RUN set -e && \
    apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates gnupg tar && \
    rm -rf /var/lib/apt/lists/* && \
    wget -O /tmp/grype.tar.gz "https://github.com/anchore/grype/releases/download/v${GRYPE_VERSION}/grype_${GRYPE_VERSION}_linux_amd64.tar.gz" && \
    wget -O /tmp/grype_checksums.txt "https://github.com/anchore/grype/releases/download/v${GRYPE_VERSION}/grype_${GRYPE_VERSION}_checksums.txt" && \
    cd /tmp && \
    grep "grype_${GRYPE_VERSION}_linux_amd64.tar.gz" grype_checksums.txt | awk '{print $1}' > expected_checksum.txt && \
    sha256sum grype.tar.gz | awk '{print $1}' > actual_checksum.txt && \
    cmp -s expected_checksum.txt actual_checksum.txt && \
    mkdir -p /grype && \
    tar -xzf grype.tar.gz -C /grype grype && \
    rm -rf /tmp/*

# ----------- Builder Stage for OSV -----------
FROM debian:bookworm-slim AS osv-builder

ARG OSV_VERSION=2.3.6

RUN set -e && \
    apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates && \
    wget -O /osv-scanner "https://github.com/google/osv-scanner/releases/download/v${OSV_VERSION}/osv-scanner_linux_amd64" && \
    chmod +x /osv-scanner && \
    mkdir -p /osv && \
    mv /osv-scanner /osv/ && \
    rm -rf /var/lib/apt/lists/*

# ----------- Dependency-Check Base -----------
FROM owasp/dependency-check-action:latest AS depcheck-base

# ----------- Final Combined Stage -----------
FROM debian:bookworm-slim

# Install required packages including Python for Azure Blob upload
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    curl \
    unzip \
    ca-certificates \
    jq \
    procps \
    openjdk-17-jre-headless \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip" && \
    unzip /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/*

# Install Azure Blob Storage SDK
RUN pip3 install --no-cache-dir azure-storage-blob --break-system-packages

# Copy all scanner binaries
COPY --from=trivy-builder /trivy/trivy /usr/local/bin/trivy
COPY --from=grype-builder /grype/grype /usr/local/bin/grype
COPY --from=osv-builder /osv/osv-scanner /usr/local/bin/osv-scanner
COPY --from=depcheck-base /usr/share/dependency-check /usr/share/dependency-check

# Create user
RUN useradd -r -m appsecuser && \
    chown -R appsecuser:appsecuser /usr/share/dependency-check/data/

# Copy entrypoint script and Azure Blob upload scripts
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY blob_storage.py /app/blob_storage.py
COPY upload_to_blob.py /app/upload_to_blob.py

# Fix line endings in case file was edited on Windows
RUN sed -i 's/\r//' /usr/local/bin/entrypoint.sh

# Set permissions
RUN chmod +x /usr/local/bin/trivy \
    /usr/local/bin/grype \
    /usr/local/bin/osv-scanner \
    /usr/local/bin/entrypoint.sh && \
    chown appsecuser:appsecuser \
    /usr/local/bin/trivy \
    /usr/local/bin/grype \
    /usr/local/bin/osv-scanner \
    /usr/local/bin/entrypoint.sh

USER appsecuser
WORKDIR /home/appsecuser

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
