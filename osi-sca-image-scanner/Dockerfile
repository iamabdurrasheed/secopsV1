# ----------- Builder Stage -----------
FROM debian:bookworm-slim AS builder

ARG TRIVY_VERSION=0.70.0

RUN set -e && \
    apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates gnupg tar curl && \
    rm -rf /var/lib/apt/lists/* && \
    \
    # Install Trivy
    wget -O /tmp/trivy.tar.gz "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" && \
    wget -O /tmp/trivy_checksums.txt "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_checksums.txt" && \
    cd /tmp && \
    grep "trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" trivy_checksums.txt | awk '{print $1}' > expected_checksum.txt && \
    sha256sum trivy.tar.gz | awk '{print $1}' > actual_checksum.txt && \
    cmp -s expected_checksum.txt actual_checksum.txt && \
    mkdir -p /trivy && \
    tar -xzf trivy.tar.gz -C /trivy trivy && \
    rm -rf /var/lib/apt/lists/* /tmp/*

# ----------- Final Stage -----------
FROM debian:bookworm-slim

# Install runtime dependencies and Azure Blob SDK
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl unzip ca-certificates jq docker.io python3 python3-pip && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip" && \
    unzip /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    pip3 install --no-cache-dir azure-storage-blob --break-system-packages && \
    rm -rf /var/lib/apt/lists/* /tmp/*

RUN useradd -r -G docker appsecuser

# Copy Trivy binary from builder
COPY --from=builder /trivy/trivy /usr/local/bin/trivy

# Copy entrypoint and blob upload scripts
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY blob_storage.py /app/blob_storage.py
COPY upload_to_blob.py /app/upload_to_blob.py

# Fix line endings in case file was edited on Windows
RUN sed -i 's/\r//' /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/trivy /usr/local/bin/entrypoint.sh && \
    chown appsecuser /usr/local/bin/trivy /usr/local/bin/entrypoint.sh

USER appsecuser
WORKDIR /home/appsecuser

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
