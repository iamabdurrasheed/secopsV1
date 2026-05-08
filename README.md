# OSI Scanner

OSI Scanner is a Docker-based security scanning orchestration system. It receives scan requests, launches scanner containers in the background, executes security analysis tools (dependency scanning, image scanning, and SAST), generates vulnerability reports, and stores results locally and optionally in Azure Blob Storage.

---

## High-Level Flow

```
┌──────────────────────┐
│  Scan Request Client │
│  (HTTP API or CLI)   │
└──────────┬───────────┘
           │
           │ HTTP POST /trigger-scan  OR  ZeroMQ REQ
           │
           ▼
┌──────────────────────────────────────┐
│ secops-polling-migration             │
│ FastAPI Service (Port 9001)          │
│ - Receives HTTP scan requests        │
│ - Validates payload                  │
└──────────┬───────────────────────────┘
           │
           │ ZeroMQ REQ (JSON payload)
           │
           ▼
┌──────────────────────────────────────┐
│ zmq_worker.py                        │
│ ZeroMQ REP Socket (Port 9002)        │
│ - Receives and validates scan job    │
│ - Sends immediate ACK/rejection      │
│ - Starts scanning in background      │
└──────────┬───────────────────────────┘
           │
           │ [Background Thread]
           │
           ├─ docker build ─┐
           │                │
           │                ▼
           │         ┌──────────────────────────┐
           │         │ Scanner Container        │
           │         │ (osi-sca-source-scanner) │
           │         │ (osi-sca-image-scanner)  │
           │         │ (osi-sast-scanner)       │
           │         │                          │
           │         │ • Git clone or local run │
           │         │ • Execute scanners       │
           │         │ • Merge JSON results     │
           │         │ • Generate SBOM          │
           │         │ • Upload to Azure        │
           │         └──────────┬───────────────┘
           │                    │
           │                    ▼
           │         ┌──────────────────────────┐
           │         │ /tmp Tree Output         │
           │         │ (/tmp/<app>/<service>/..)│
           │         └──────────────────────────┘
           │
           │ [Completion Metadata]
           │
           └─ ZeroMQ PUSH ─→ Port 9000 (stub)
```

---

## Repository Structure

```
Osi_Scanner/
│
├── .env
│   Shared environment configuration.
│   Contains: ZMQ ports, Azure credentials, timeout settings.
│
├── README.md
│   This file. High-level project overview.
│
├── TECHNICAL.md
│   Complete engineering reference. Full architecture, functions,
│   APIs, operational procedures, and debugging guides.
│
├── requirements.txt
│   Python dependencies (pyzmq, python-dotenv, Azure SDK).
│
├── setup_and_run.py
│   Local smoke-test script. Starts the worker, sends a scan request,
│   monitors logs, and reports results.
│
├── zmq_worker.py
│   Core orchestrator. Listens for scan requests on ZeroMQ REP socket,
│   validates payloads, and spawns Docker containers asynchronously.
│
├── secops-polling-migration/
│   Production HTTP API service.
│   ├── src/main.py
│   │   FastAPI entry point. Defines base app and mounts routes.
│   │
│   ├── src/routes/polling_routes.py
│   │   HTTP routes. Defines POST /trigger-scan and GET /health.
│   │
│   ├── src/schemas/polling_schemas.py
│   │   Request/response Pydantic models (ScanTriggerPayload, etc.).
│   │
│   ├── src/controllers/polling_controller.py
│   │   Request handlers. Bridges HTTP routes to service layer.
│   │
│   └── src/services/polling_service.py
│       Business logic. Orchestrates scan dispatch to ZMQ worker.
│
├── osi-sca-source-scanner/
│   Docker image for source code dependency scanning.
│   ├── Dockerfile
│   │   Multi-stage build. Installs Trivy, Grype, OSV Scanner,
│   │   Dependency-Check, jq, and Python Azure SDK.
│   │
│   ├── entrypoint.sh
│   │   Container startup script. Clones repository, runs scanners
│   │   in parallel, merges results, generates SBOM, uploads to Azure.
│   │
│   ├── blob_storage.py
│   │   Azure Blob Storage client. Handles authentication and upload.
│   │
│   └── upload_to_blob.py
│       CLI wrapper for uploading scan results and SBOM to Azure.
│
├── osi-sca-image-scanner/
│   Docker image for container image scanning.
│   ├── Dockerfile
│   │   Base image, Trivy install, Docker CLI, AWS CLI, Python SDK.
│   │
│   ├── entrypoint.sh
│   │   AWS credential setup, Trivy image scan, SBOM generation, upload.
│   │
│   ├── blob_storage.py
│   │   (identical to source-scanner version)
│   │
│   └── upload_to_blob.py
│       (identical to source-scanner version)
│
├── osi-sast-scanner/
│   Docker image for static application security testing (SAST).
│   ├── Dockerfile
│   │   Installs Gitleaks, TruffleHog, Semgrep, Trivy.
│   │
│   ├── entrypoint.sh
│   │   Git clone, run SAST tools, merge results, SBOM, upload.
│   │
│   ├── blob_storage.py
│   │   (identical to other scanners)
│   │
│   └── upload_to_blob.py
│       (identical to other scanners)
│
└── fastapi-integration.md
    (Legacy file; not actively used in current architecture)
```

---

## Main Components

### 1. secops-polling-migration (HTTP API Service)

**Purpose**: Accept HTTP scan requests from external clients.

**Port**: 9001

**What it receives**:
- POST `/trigger-scan` with JSON payload containing:
  - `app_name`, `service_name` (identifiers)
  - `scanner_name` (which scanner to use)
  - `repo_url`, `repo_branch` (for source/SAST scanning)
  - `image_uri` (for image scanning)
  - AWS credentials, Azure settings (optional)

**What it does**:
- Validates the payload
- Forwards the request to the ZeroMQ worker (TCP localhost:9002)
- Returns HTTP 200 if accepted
- Returns HTTP 503 if worker is unreachable

**What port it listens on**: 9001

### 2. zmq_worker.py (Background Orchestrator)

**Purpose**: Receive scan jobs and launch scanner containers.

**Port**: 9002

**What it processes**:
- ZeroMQ REQ messages from FastAPI service
- JSON payload with scan parameters
- Validates scanner name, required fields

**How it launches jobs**:
- Acknowledges request immediately (synchronous handshake)
- Starts Docker build + run in background thread (asynchronous processing)

**How it communicates**:
- ZeroMQ REP socket (receive requests)
- ZeroMQ PUSH socket (send completion metadata to port 9000)
- Docker CLI (subprocess calls to docker build, docker run)

### 3. Scanner Containers

Three Docker images, each running different tools:

#### osi-sca-source-scanner
- **Purpose**: Scan source code for dependency vulnerabilities
- **Tools**: Trivy, Grype, OSV Scanner, Dependency-Check
- **Input**: Git repository URL + branch (or local folder)
- **Output**: JSON file with merged vulnerability results + CycloneDX SBOM

#### osi-sca-image-scanner
- **Purpose**: Scan container images for vulnerabilities
- **Tools**: Trivy (image mode)
- **Input**: Container image URI (e.g., `nginx:latest` or ECR URL)
- **Output**: JSON file with image scan results + CycloneDX SBOM

#### osi-sast-scanner
- **Purpose**: Scan source code for secrets and security issues
- **Tools**: Gitleaks, TruffleHog, Semgrep
- **Input**: Git repository URL + branch (or local folder)
- **Output**: JSON file with SAST findings + CycloneDX SBOM

---

## Execution Flow (Step by Step)

1. **Client sends request** (HTTP POST or ZeroMQ)
   - Contains: app name, service name, scanner type, repository info

2. **FastAPI service receives HTTP POST /trigger-scan**
   - Validates the JSON payload
   - Checks scanner name is supported
   - Checks required fields are present

3. **FastAPI forwards to ZeroMQ worker**
   - Opens ZeroMQ REQ socket
   - Sends JSON payload as string
   - Waits for response (timeout: 10 seconds)

4. **Worker receives request on ZeroMQ REP socket**
   - Parses JSON payload
   - Validates again (scanner name, required fields)
   - Immediately sends ACK response
   - Client receives 200 OK

5. **Worker starts background thread**
   - Builds Docker image from scanner folder
   - Creates temporary Azure credentials file (if configured)
   - Runs Docker container with environment variables
   - Mounts `/tmp` from host so container can write results

6. **Scanner container starts**
   - Reads environment variables (APP_NAME, SERVICE_NAME, REPO_URL, etc.)
   - If Git mode: clones repository
   - Resolves branch to commit SHA
   - Runs scanning tools (Trivy, Grype, Gitleaks, etc.)
   - Each tool outputs JSON
   - Merges all JSON outputs using `jq`

7. **Scanner container generates output**
   - Creates unified JSON with all tool results
   - Generates CycloneDX SBOM using Trivy
   - Writes both to `/tmp/<app>/<service>/<branch>/<commit>/`
   - Uploads to Azure Blob Storage (if credentials provided)

8. **Container exits**
   - Exit 0 on success
   - Exit non-zero on failure

9. **Worker receives completion**
   - Container exits and results are visible on host under `/tmp`
   - Worker sends completion metadata to port 9000 (ZeroMQ PUSH)
   - Port 9000 is a stub; no service listens there

10. **Client checks results**
    - Results are available on host under `/tmp/<app>/<service>/<branch>/<commit>/`
    - Or visible in Azure Blob Storage (if uploads enabled)

---

## Environment Variables

Only environment variables that actually exist in `.env`:

| Variable | Purpose |
|---|---|
| `ZMQ_WORKER_PORT` | Port number where worker listens (default: 9002) |
| `ZMQ_LISTEN_ADDRESS` | Full bind address for worker (e.g., `tcp://0.0.0.0:9002`) |
| `ZMQ_WORKER_ADDRESS` | Client connect address for worker (e.g., `tcp://localhost:9002`) |
| `SBOM_BACKEND_ADDRESS` | Metadata sink address (e.g., `tcp://localhost:9000`) |
| `SBOM_PROCESSOR_PORT` | Port number of stub SBOM processor (default: 9000) |
| `ZMQ_ACK_TIMEOUT_MS` | How long to wait for worker response (default: 10000 ms) |
| `SBOM_FORWARD_TIMEOUT_MS` | How long to wait when sending completion metadata (default: 5000 ms) |
| `AZURE_STORAGE_CONNECTION_STRING` | Azure Blob credentials (optional; uploads skipped if missing) |
| `AZURE_CONTAINER_NAME` | Azure Blob container name (optional) |
| `AZURE_STORAGE_ACCOUNT` | Azure storage account (optional) |
| `AZURE_STORAGE_KEY` | Azure storage key (optional) |

---

## How to Run

The system requires **3 terminal tabs/windows running simultaneously**. Each handles a different component of the execution pipeline.

### Terminal 1 — Start OSI Scanner Worker (Port 9002)

**Purpose**: Actual scan execution engine. Builds Docker images, runs scanner containers, generates SBOM/results.

```bash
cd /path/to/Osi_Scanner
python3 zmq_worker.py
```

**Expected startup logs**:
```
INFO ZMQ worker listening on tcp://0.0.0.0:9002
INFO SBOM forwarding target: tcp://localhost:9000
```

**Role**: Scanner execution worker (backend processing)

---

### Terminal 2 — Start Polling / Orchestration Worker (Port 9001)

**Purpose**: Receives incoming scan requests, validates payloads, sends ACK responses, fetches repository metadata, forwards requests to scanner worker.

```bash
cd /path/to/Osi_Scanner/secops-polling-migration
python3 -m src.main
```

Or with uvicorn directly:
```bash
uvicorn src.main:app --host 0.0.0.0 --port 9001
```

**Expected startup logs**:
```
INFO Uvicorn running on http://0.0.0.0:9001
INFO ZMQ connected to scanner worker at tcp://localhost:9002
```

**Role**: HTTP API + validation layer (frontend processing)

---

### Terminal 3 — Trigger Test Scan Request

**Purpose**: Acts as client/test sender. Sends trigger_scan payload to polling worker.

```bash
cd /path/to/Osi_Scanner/secops-polling-migration

# Option 1: Use provided test script
python3 scripts/trigger_scan.py

# Option 2: Use curl to send HTTP request
curl -X POST http://localhost:9001/trigger-scan \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "my-app",
    "service_name": "my-service",
    "scanner_name": "osi_sca_source_scanner",
    "repo_url": "https://github.com/example/repo.git",
    "repo_branch": "main"
  }'
```

**Expected response**:
```json
{
  "message": "Scan triggered",
  "execution_id": "550e8400-e29b-41d4-a716-446655440000",
  "scanner": "osi_sca_source_scanner"
}
```

**Role**: Client/test trigger

---

### Complete Execution Flow

```
Terminal 3                    Terminal 2                    Terminal 1
trigger_scan                  Polling Worker                Scanner Worker
                              (port 9001)                   (port 9002)
│                             │                             │
├─ Sends request ────────────>│                             │
│                             │                             │
│                    ┌─ Validate payload                    │
│                    ├─ Send ACK to client                  │
│                    ├─ Fetch Git commit SHA                │
│                    └─ Forward to scanner ──────────────>  │
│                             │                             │
│                             │              ┌─ Load env    │
│                             │              ├─ Docker build│
│                             │              ├─ Docker run  │
│                             │              ├─ Git clone   │
│                             │              ├─ Execute scan│
│                             │              ├─ Gen SBOM    │
│                             │              └─ Save results│
│                             │                             │
│     HTTP 200 OK             │                             │
│   <────────────────────────┤                             │
│  (immediate response)       │                             │
│                             │                             │
│   (results available)       │                             │
│   in /tmp after             │                             │
│   container exits           │                             │
│                             │                             ▼
│                             │              Results: /tmp/<app>/<service>/...
```

---

### Port Architecture

| Port | Service | Purpose |
|---|---|---|
| **9001** | Polling/Orchestration Worker | HTTP API, validation, ACK responses, request forwarding |
| **9002** | Scanner Worker | Docker execution, actual scans, SBOM generation, result storage |
| **9000** | SBOM Consumer (stub) | Future backend integration (metadata is pushed here but no service listens) |

---

### Execution Steps (What Happens)

1. **Terminal 3**: Sends scan request (HTTP or ZeroMQ)
2. **Terminal 2**: Receives request on port 9001
3. **Terminal 2**: Validates payload
4. **Terminal 2**: Sends ACK to client immediately
5. **Terminal 2**: Fetches Git metadata (commit SHA)
6. **Terminal 2**: Forwards request to Terminal 1 (port 9002)
7. **Terminal 1**: Receives request on port 9002
8. **Terminal 1**: Builds Docker image from scanner folder
9. **Terminal 1**: Runs Docker container with environment variables
10. **Terminal 1**: Container clones repository
11. **Terminal 1**: Container runs scanning tools
12. **Terminal 1**: Container generates merged JSON results
13. **Terminal 1**: Container generates CycloneDX SBOM
14. **Terminal 1**: Container uploads to Azure (if configured)
15. **Terminal 1**: Container writes results to `/tmp/<app>/<service>/<branch>/<commit>/`
16. **Terminal 1**: Container exits (success or failure)
17. **Terminal 1**: Results available on host

---

### Quick Local Test (Single Terminal)

If you want to test everything without multiple terminals, use the local test harness:

```bash
cd /path/to/Osi_Scanner

python3 setup_and_run.py \
  --scanner osi_sca_source_scanner \
  --app-name my-app \
  --service-name my-service \
  --repo-url https://github.com/example/repo.git \
  --repo-branch main
```

This script:
- Clears the worker port
- Starts the worker
- Sends one scan request directly to the worker
- Monitors the log until completion
- Prints a summary report
- Cleans up (unless `--no-cleanup` flag is used)

---

## Output Locations

Scan results are written to the host's `/tmp` directory in this structure:

```
/tmp/<app_name>/<service_name>/<branch_name>/<commit_sha>/
├── osi-sca-source-scanner/
│   └── <timestamp>.json          (merged vulnerability results)
├── osi-sca-image-scanner/
│   └── <timestamp>.json          (image scan results)
├── osi-sast-scanner/
│   └── <timestamp>.json          (SAST findings)
└── sbom/
    └── <timestamp>.json          (CycloneDX SBOM)
```

**Example**:
```
/tmp/my-app/auth-service/main/a1b2c3d4/
├── osi-sca-source-scanner/20260508-113000.json
├── osi-sca-image-scanner/20260508-113001.json
├── osi-sast-scanner/20260508-113002.json
└── sbom/20260508-113005.json
```

**Optional**: If Azure credentials are configured in `.env`, identical results are also uploaded to Azure Blob Storage under the same path structure.

---

## Logs and Debugging

### Worker Logs

```bash
# If running with setup_and_run.py:
tail -f /tmp/osi-zmq-worker.log

# If running directly:
python3 zmq_worker.py  # logs to stdout
```

### Check Worker Status

```bash
# Verify worker is listening
netstat -tlnp | grep 9002

# Or:
ss -tlnp | grep 9002
```

### Inspect Results

```bash
# Find all scan results
find /tmp -type d -name "my-app"

# List results for specific scan
ls -la /tmp/my-app/my-service/main/a1b2c3d4/

# View JSON result
cat /tmp/my-app/my-service/main/a1b2c3d4/osi-sca-source-scanner/*.json | jq .
```

### Docker Container Logs

```bash
# See what a scanner container output during execution
# Logs are printed by worker while container runs
# Check /tmp/osi-zmq-worker.log for "[container]" lines
```

---

## Common Issues

| Problem | Likely Cause | Solution |
|---|---|---|
| "Address already in use :9002" | Another process is using port 9002 | `fuser -k 9002/tcp` or `pkill -f zmq_worker` |
| Worker won't start | Missing .env or required variables not set | Create .env with `ZMQ_WORKER_PORT`, `ZMQ_LISTEN_ADDRESS`, etc. |
| "Connection refused" when sending request | Worker not running | Start worker: `python3 zmq_worker.py` |
| Scan request times out | Worker not responding within 10 seconds | Check /tmp/osi-zmq-worker.log for errors |
| Docker build fails | Base image not found | Check internet access, verify FROM line in Dockerfile |
| Results not in /tmp | Container exited with error | Check worker logs for Docker error messages |
| Azure upload fails | Connection string missing or invalid | Set `AZURE_STORAGE_CONNECTION_STRING` in .env (optional; not required) |
| Git clone fails | Private repository or on-prem Git with SSL issues | Set `IS_HOSTED_ON_PREM=True` in payload for on-prem repos |

---

## Architecture Notes

**Why ZeroMQ?**
- Provides immediate ACK/rejection synchronously while processing happens asynchronously
- Language-agnostic message format (JSON strings)
- Fire-and-forget completion metadata

**Why Docker?**
- Isolates scanning tools so they don't pollute the host system
- Reproducible builds across environments
- Easy to add new scanners by creating new Dockerfiles

**Why background threads?**
- Client gets immediate acknowledgment (knows request was accepted)
- Docker build + run can take 30–300 seconds without blocking other requests

**Why separate services?**
- FastAPI (port 9001) provides HTTP interface for external clients
- Worker (port 9002) handles internal orchestration via ZeroMQ
- Separation of concerns: one handles I/O, the other handles execution

---

## For More Details

For complete technical documentation including:
- Function-by-function reference
- Detailed sequence diagrams
- Environment variable propagation rules
- Container entrypoint logic
- Bug fixes and engineering changes
- Operational procedures
- Debugging techniques

See [TECHNICAL.md](./TECHNICAL.md).
  --scan-job-id 1010 \
  --scanner-agent-id agent-001 \
  --environment dev
```

Useful notes:

- The script expects Linux utilities such as `docker`, `python3`, `pip3`, `ss`, and `fuser`.
- `--no-cleanup` leaves the worker running after the smoke test.
- `--scan-timeout` controls how long the script watches the worker log.

## Scanner Images

### Source scanner

`osi-sca-source-scanner` scans source repositories or mounted folders with Trivy, Grype, OSV Scanner, and OWASP Dependency-Check. It generates a CycloneDX SBOM and structured JSON output.

### Image scanner

`osi-sca-image-scanner` scans container images with Trivy and also supports folder mode when `FOLDER_PATH` is provided.

### SAST scanner

`osi-sast-scanner` runs Gitleaks, TruffleHog, and Semgrep, then generates a CycloneDX SBOM.

Each scanner has its own Dockerfile, entrypoint, and Azure upload helper. The worker selects the folder by scanner name and rebuilds the image for every scan request.

## Operational Notes

- The worker always attempts to forward completion metadata, even if Docker build or run fails.
- Azure uploads are optional; they fail open when credentials are missing.
- Structured outputs live under `/tmp/<app>/<service>/<branch-or-version>/...`.
- `secops-polling-migration` is the current production caller.
- `osi-sca-binary-scanner/` is not present in this workspace.

For implementation details, see [TECHNICAL.md](./TECHNICAL.md).
