# OSI Scanner — Complete Technical Reference

This document is the authoritative technical manual for OSI Scanner. It covers architecture, execution flow, inter-process communication, Docker orchestration, storage semantics, APIs, and operational debugging for platform engineers, maintainers, DevOps teams, and contributors.

## SECTION 1 — System Overview & Architecture

### What This System Does

OSI Scanner is a Docker-based security scanning orchestration engine. It accepts scan requests over ZeroMQ (a message-oriented transport), validates them, dispatches scanner containers in the background, injects configuration through environment variables, waits for completion, and forwards metadata about each scan to a downstream result sink.

The system prioritizes:

- **Isolation**: Each scan runs in a fresh Docker container with its own entrypoint and filesystem.
- **Asynchrony**: Scan dispatch is non-blocking; the worker acknowledges requests immediately and processes them in background threads.
- **Fail-open**: Missing Azure credentials skip uploads; SBOM forwarding failures do not fail scans.
- **Centralization**: All configuration lives in a single root `.env` file; scanner images do not carry their own credentials.

### Why This Architecture

**Why ZeroMQ instead of HTTP?**

- Tight synchronous handshaking: client waits for explicit ACK/rejection before continuing.
- Fire-and-forget completion metadata: worker pushes results without waiting for downstream acknowledgment.
- Language-agnostic: works from Python, Java, Go, Node, etc.

**Why Docker containers?**

- Tool isolation: Trivy, Grype, OSV Scanner, Gitleaks, TruffleHog, Semgrep do not pollute host.
- Dependency encapsulation: each scanner has its own builder stage with verified tool versions.
- Reproducibility: exact toolset can be re-built deterministically on any Linux host.
- Security: non-root execution, minimal base images, checksum verification.

**Why background threads?**

- ACK is sent before Docker starts; clients know immediately whether their request was accepted.
- Docker build + run can take 30–300 seconds; blocking the REP socket would block all other clients.

**Why port layering (9000 / 9001 / 9002)?**

- 9002 is the worker (core dispatcher).
- 9001 is secops-polling-migration (production HTTP caller).
- 9000 is the stub SBOM result processor (where metadata is pushed, but no actual service listens today).

---

## SECTION 2 — Complete Architecture Diagram

```
┌────────────────────────────────────────────────────────────────────┐
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ Production Upstream: secops-polling-migration (port 9001)   │  │
│  │                                                             │  │
│  │  HTTP Routes:                                              │  │
│  │    GET  /health                                            │  │
│  │    POST /trigger-scan                                      │  │
│  │                                                             │  │
│  │  Request Body: ScanTriggerPayload                           │  │
│  │    (includes: app_name, service_name, scanner_name,        │  │
│  │     repo_url, repo_branch, image_uri, aws_keys, etc.)      │  │
│  └────────────────┬────────────────────────────────────────────┘  │
│                   │                                               │
│                   │ ZeroMQ REQ (sends payload)                    │
│                   ▼                                               │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ OSI Scanner Worker: zmq_worker.py (port 9002)              │  │
│  │                                                             │  │
│  │  Main Loop:                                                │  │
│  │    1. Bind ZeroMQ REP socket on tcp://0.0.0.0:9002         │  │
│  │    2. Blocking recv_string() [waits for JSON payload]      │  │
│  │    3. Parse JSON → validate_payload()                      │  │
│  │    4. If invalid: send rejected response                   │  │
│  │    5. If valid: send acknowledged response                 │  │
│  │    6. Start run_docker_scan() in daemon thread             │  │
│  │    7. Loop back to step 2                                  │  │
│  │                                                             │  │
│  │  ACK Response (sent synchronously):                        │  │
│  │    {"status": "acknowledged",                              │  │
│  │     "scan_job_id": "1001",                                 │  │
│  │     "message": "Payload validated. Scan starting."}        │  │
│  └─────────────┬──────────────────────────────────────────────┘  │
│                │                                                  │
│    Background │ run_docker_scan() daemon thread:                │
│    Threads    │                                                  │
│    (async)    │  1. Load Azure env from root .env                │
│               │  2. Build payload → Docker env args              │
│               │  3. docker build -t osi-scanner-<name> folder/   │
│               │  4. docker run --rm -v /tmp:/tmp -e VARS image   │
│               │  5. Read stdout/stderr line by line              │
│               │  6. Call forward_sbom() in finally block         │
│               │                                                  │
│               ▼                                                  │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ Scanner Containers (built fresh for each scan)             │  │
│  │                                                             │  │
│  │ Supported scanners:                                        │  │
│  │   • osi-sca-source-scanner (Trivy, Grype, OSV, Dep-Check) │  │
│  │   • osi-sca-image-scanner (Trivy image mode)               │  │
│  │   • osi-sast-scanner (Gitleaks, TruffleHog, Semgrep)       │  │
│  │                                                             │  │
│  │ Container Entrypoint Flow:                                 │  │
│  │   1. Git clone OR read mounted /FOLDER_PATH                │  │
│  │   2. Resolve branch → commit SHA (via git rev-parse)       │  │
│  │   3. Run scanner tools (Trivy, Grype, etc.)                │  │
│  │   4. Merge JSON outputs                                    │  │
│  │   5. Generate CycloneDX SBOM                               │  │
│  │   6. Upload to Azure Blob (if AZURE_STORAGE_CONNECTION_.. │  │
│  │   7. Write /tmp/<app>/<service>/<branch>/<commit>/<files> │  │
│  │   8. Exit 0 (success) or non-zero (failure)                │  │
│  └─────────────┬──────────────────────────────────────────────┘  │
│                │                                                  │
│                │ Scan results + SBOM JSON                        │
│                ▼                                                  │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ Host /tmp Tree                                              │  │
│  │                                                             │  │
│  │ /tmp/<app_name>/<service_name>/<branch_name>/              │  │
│  │     <commit_sha>/                                           │  │
│  │       osi-sca-source-scanner/<timestamp>.json               │  │
│  │       osi-sca-image-scanner/<timestamp>.json                │  │
│  │       osi-sast-scanner/<timestamp>.json                     │  │
│  │       sbom/<timestamp>.json                                 │  │
│  └─────────────┬──────────────────────────────────────────────┘  │
│                │                                                  │
│    forward_sbom() │ Scan completion metadata (async):           │
│    (in worker)    │ event: "scan_completed"                      │
│                   │ scan_job_id, scanner_name, app_name,         │
│                   │ service_name, service_environment_id,        │
│                   │ app_service_id, scanner_agent_id             │
│                   ▼                                               │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ Stub SBOM Result Processor (port 9000)                      │  │
│  │ [No actual service listens; metadata is pushed then lost]   │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │ Azure Blob Storage                                          │  │
│  │ (optional; only if AZURE_STORAGE_CONNECTION_STRING is set) │  │
│  │                                                             │  │
│  │ Paths:                                                      │  │
│  │   <app>/<service>/<branch>/<commit>/<scanner>/<time>.json  │  │
│  │   <app>/<service>/<branch>/<commit>/sbom/<time>.json       │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

---

## SECTION 3 — Environment & Configuration

### Complete Environment Variable Reference

| Variable | Type | Required | Default | Consumed By | Description |
|---|---|---|---|---|---|
| `ZMQ_WORKER_PORT` | int | Yes | none | `setup_and_run.py`, `zmq_worker.py` | Port the worker REP socket binds on. Used for `ss -tlnp` readiness checks and `fuser` cleanup. |
| `SBOM_PROCESSOR_PORT` | int | Yes | none | `setup_and_run.py`, `zmq_worker.py` (for reference) | Port of downstream SBOM sink. Not actively used but referenced for documentation. |
| `ZMQ_LISTEN_ADDRESS` | string | Yes | none | `zmq_worker.py` | Full bind address: `tcp://0.0.0.0:9002`. Passed to `socket.bind()`. |
| `ZMQ_WORKER_ADDRESS` | string | Yes | none | `zmq_worker.py`, `setup_and_run.py` | Client connect address: `tcp://localhost:9002`. Used by REQ clients and passed to containers via env. |
| `SBOM_BACKEND_ADDRESS` | string | Yes | none | `zmq_worker.py` | PUSH target: `tcp://localhost:9000`. Where `forward_sbom()` sends completion metadata. |
| `ZMQ_ACK_TIMEOUT_MS` | int | Yes | none | `setup_and_run.py` | Timeout when waiting for worker ACK: 10000 (10 seconds). |
| `SBOM_FORWARD_TIMEOUT_MS` | int | No | 5000 | `zmq_worker.py` | Timeout when pushing completion metadata: 5 seconds. Failures are logged but non-fatal. |
| `AZURE_STORAGE_CONNECTION_STRING` | string | No | none | Scanner containers (blob_storage.py) | Primary upload credential. If missing or starts with `your_`, uploads are skipped. |
| `AZURE_CONTAINER_NAME` | string | No | none | Scanner containers | Azure blob container name. Mapped to `AZURE_STORAGE_CONTAINER` by worker. |
| `AZURE_STORAGE_CONTAINER` | string | No | `scan-results` | Scanner containers (blob_storage.py) | Compatibility alias. Set by worker to `AZURE_CONTAINER_NAME` if not present. |
| `AZURE_STORAGE_ACCOUNT` | string | No | none | Scanner containers | Azure storage account name. Forwarded when present. |
| `AZURE_STORAGE_KEY` | string | No | none | Scanner containers | Azure storage account key. Forwarded when present (for compatibility). |

### Container-Injected Environment Variables

These are set by `build_env_args()` and `_write_azure_env_file()` from the scan payload:

| Payload Field | Container Variable | Source | Example |
|---|---|---|---|
| `app_name` | `APP_NAME` | Payload | `my-app` |
| `service_name` | `SERVICE_NAME` | Payload | `auth-service` |
| `scanner_name` | `SCANNER_NAME` | Payload | `osi_sca_source_scanner` |
| `scan_job_id` | `SCAN_JOB_ID` | Payload | `1001` |
| `scanner_agent_id` | `SCANNER_AGENT_ID` | Payload | `agent-001` |
| `service_environment_id` | `SERVICE_ENVIRONMENT_ID` | Payload | `dev` |
| `app_service_id` | `APP_SERVICE_ID` | Payload | `svc-001` |
| `auth_token` | `AUTH_TOKEN` | Payload (optional) | (token string) |
| `repo_url` | `REPO_URL` | Payload | `https://github.com/org/repo.git` |
| `repo_branch` | `BRANCH` | Payload | `main` |
| `is_hosted_on_prem` | `IS_HOSTED_ON_PREM` | Payload | `False` |
| `version` | `VERSION` | Payload | `1.0.0` |
| `image_uri` | `IMAGE_URI` | Payload | `nginx:latest` |
| `aws_access_key` | `AWS_ACCESS_KEY_ID` | Payload | (key) |
| `aws_secret_key` | `AWS_SECRET_ACCESS_KEY` | Payload | (secret) |
| `aws_region` | `AWS_DEFAULT_REGION` | Payload | `us-east-1` |
| `api_domain_url` | `BASE_URL` | Payload | `https://api.example.com` |

### How .env Files Are Loaded

1. **Root `.env` loading** (`setup_and_run.py` + `zmq_worker.py`):
   - `from dotenv import load_dotenv` loads `.env` into `os.environ`.
   - If `dotenv` is not installed, fallback to manual parsing (splitting on `=`).
   - Fallback parsing is strict: skips comment lines, blank lines, and lines without `=`.

2. **Precedence**:
   - Environment variables already set in the shell take precedence.
   - `.env` values are merged in via `setdefault()` (do not override existing).

3. **Azure env file generation** (`_write_azure_env_file()`):
   - When Azure credentials exist, a temporary file is created: `/tmp/osi_azure_<scan_job_id>_<uuid>.env`.
   - Passed to Docker via `--env-file` so connection strings with `=` are preserved.
   - Deleted after container runs (in `finally` block).

4. **Why separate Azure env file?**
   - Docker `-e KEY=VALUE` truncates on first `=`; connection strings like `DefaultEndpointProtocol=https;...` become invalid.
   - `--env-file` passes raw values without re-parsing on `=`.

### Fallback Behavior

- **Missing Azure connection string**: `blob_storage.py` catches `EnvironmentError`, logs `[SKIP]`, and exits with 0. Scan completes without upload.
- **Missing SBOM backend**: `forward_sbom()` logs timeout/connection error as warning. Scan not affected.
- **Missing required ZMQ variables**: Worker fails at startup with `RuntimeError` (intentional hard failure).

---

## SECTION 4 — Entry Point / Bootstrap Scripts

### `setup_and_run.py` — Local Smoke Test Harness

**Purpose**: Automate local testing of the worker without requiring production infrastructure.

**Execution Phases**:

| Phase | Function | Purpose |
|---|---|---|
| 0 | `kill_existing_services()` | Run `fuser -k <port>/tcp` to free `ZMQ_WORKER_PORT`. Check `ss -tlnp` to confirm. |
| 1 | `check_prerequisites()` | Verify Docker, Python 3, pip3, and Docker daemon are available. Exit if missing. |
| 2 | `install_dependencies()` | Run `pip3 install -r requirements.txt --quiet`. |
| 3 | `start_worker()` | Spawn `python3 zmq_worker.py` as subprocess. Redirect stdout/stderr to `/tmp/osi-zmq-worker.log`. Wait for port to bind (poll `ss -tlnp` up to 20 times, 1s each). |
| 4 | `build_payload()` + `send_scan()` | Build dict from CLI args. Open ZeroMQ REQ socket, send JSON payload, wait for ACK (timeout: `ZMQ_ACK_TIMEOUT_MS`). |
| 5 | `tail_worker_logs()` | Read `/tmp/osi-zmq-worker.log` in a loop. Look for completion markers: `"Scan completed successfully"`, `"SBOM result forwarded"`, `"Docker error"`. Stop when found or timeout exceeded. |
| 6 | `report()` | Print final status, log paths, PID, port layout, and debugging commands. |
| 7 | `cleanup()` | Terminate worker and FastAPI processes (if `--no-cleanup` is not set). |

---

## SECTION 5 — Core Worker Functions

### `zmq_worker.py` — Core Orchestrator

**File Purpose**: Central dispatcher that receives scan jobs, validates payloads, launches Docker containers asynchronously.

#### Function: `require_env(name)`

**PURPOSE**: Reads a required environment variable and fails hard if missing or empty.

**PARAMETERS**: `name` (str): Environment variable name.

**RETURN VALUE**: str — The environment variable value. Raises `RuntimeError` if missing or empty.

**LOGIC**: 
```python
value = os.environ.get(name)
if not value:
    raise RuntimeError(f"{name} must be set in the root .env")
return value
```

**SIDE EFFECTS**: Process exits if variable missing (early fail strategy).

---

#### Function: `load_azure_env()`

**PURPOSE**: Extract Azure Blob Storage credentials from root `.env`. Ignore placeholder values. Return dict for injection into containers.

**PARAMETERS**: None.

**RETURN VALUE**: dict — Keys are Azure env var names (e.g., `AZURE_STORAGE_ACCOUNT`), values are credential strings. Empty dict if no real credentials.

**LOGIC**: 
- Filter placeholders (values starting with "your_").
- Map `AZURE_CONTAINER_NAME` → `AZURE_STORAGE_CONTAINER` for compatibility.
- Warn if connection string missing.

---

#### Function: `_write_azure_env_file(azure_env, scan_job_id)`

**PURPOSE**: Write Azure credentials to a temporary file so Docker can inject them without truncating connection strings on `=`.

**PARAMETERS**: 
- `azure_env` (dict): Azure environment variables.
- `scan_job_id` (str): For temp file naming.

**RETURN VALUE**: str — Path to temp file (e.g., `/tmp/osi_azure_1001_<uuid>.env`). None if `azure_env` is empty.

**SIDE EFFECTS**: Creates file in `/tmp` with `delete=False` (caller responsible for cleanup).

---

#### Function: `build_env_args(payload)`

**PURPOSE**: Transform scan request payload fields into Docker `-e KEY=VALUE` command-line arguments.

**PARAMETERS**: `payload` (dict): Scan request from client.

**RETURN VALUE**: list — Docker `-e` arguments, e.g., `["-e", "APP_NAME=my-app", "-e", "SERVICE_NAME=auth-service", ...]`.

**LOGIC**: Map payload fields to Docker env names via `FIELD_ENV_MAP`. Skip None values.

---

#### Function: `validate_payload(payload)`

**PURPOSE**: Validate scan request payload. Check scanner name, required fields, and scanner-specific fields.

**PARAMETERS**: `payload` (dict): Parsed JSON from client.

**RETURN VALUE**: str — Error message if validation fails. None if valid.

**LOGIC**: 
- Check scanner_name is in `VALID_SCANNER_TYPES`.
- Check common required fields present.
- Check scanner-specific fields present.
- Return None if all pass.

---

#### Function: `forward_sbom(payload, scan_job_id)`

**PURPOSE**: Push scan completion metadata to the downstream result sink via ZeroMQ PUSH socket. Non-blocking, fire-and-forget.

**PARAMETERS**: 
- `payload` (dict): Original scan request.
- `scan_job_id` (str): Scan identifier.

**RETURN VALUE**: None.

**SIDE EFFECTS**: Network call to `SBOM_BACKEND_ADDRESS`. Logs info, warning messages. Non-fatal: exceptions caught and logged.

---

#### Function: `run_docker_scan(payload)`

**PURPOSE**: Execute a single scan: build Docker image, run container, handle results, forward metadata.

**PARAMETERS**: `payload` (dict): Scan request.

**RETURN VALUE**: None.

**SIDE EFFECTS**: 
- Docker build + run (host-wide impact).
- Writes to `/tmp/<app>/<service>/...` (via container).
- Logs lines to `/tmp/osi-zmq-worker.log`.
- Network call to Azure Blob Storage (if configured).
- Creates + deletes temp Azure env file.

**LOGIC**:
1. Load Azure env.
2. Build Docker env args.
3. Create Azure env file.
4. Docker build.
5. Docker run with env file.
6. Log output.
7. Forward metadata (finally).
8. Cleanup.

---

#### Function: `main()`

**PURPOSE**: Main worker event loop. Bind ZMQ REP socket, accept payloads, validate, respond, dispatch to threads.

**RETURN VALUE**: None (runs forever until signal/exception).

**LOGIC**:
1. Bind REP socket to `LISTEN_ADDRESS`.
2. Loop: receive JSON → parse → validate → send response → dispatch thread (if valid).
3. Daemon threads run `run_docker_scan()`.

---

## SECTION 6 — API Layer

### Route: `POST /trigger-scan`

**File**: `secops-polling-migration/src/routes/polling_routes.py`

**HTTP Method**: POST

**Path**: `/trigger-scan`

**Request Schema**: `ScanTriggerPayload`

**Request Body Example**:

```json
{
  "app_name": "my-app",
  "service_name": "auth-service",
  "scanner_name": "osi_sca_source_scanner",
  "scan_launcher_url": "http://worker:9002",
  "scanner_agent_id": "agent-001",
  "service_environment_id": "dev",
  "repo_url": "https://github.com/org/repo.git",
  "repo_branch": "main"
}
```

**Processing**:
1. `PollingController.run_api_trigger()` is called.
2. Payload forwarded to worker via ZMQ REQ.

**Response (HTTP 200)**:

```json
{
  "message": "Scan triggered",
  "execution_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Status Codes**:
- **200 OK**: Scan accepted.
- **400 Bad Request**: Missing required fields.
- **503 Service Unavailable**: Worker unreachable.

---

### Route: `GET /health`

**Path**: `/health`

**Response**: `{"status": "SecOps polling server is running"}`

**Status Code**: 200 OK

---

## SECTION 7 — Container / Worker Images

### Scanner 1: `osi-sca-source-scanner`

**Base Image**: `debian:bookworm-slim`

**Multi-Stage Build**: Trivy, Grype, OSV Scanner, Dependency-Check, jq, Python Azure SDK.

**Entrypoint Flow**:
1. Git clone repo.
2. Run scanners in parallel (Trivy, Grype, OSV, Dependency-Check).
3. Merge JSON results.
4. Generate CycloneDX SBOM.
5. Upload to Azure.
6. Write results to `/tmp/<app>/<service>/...`.

---

### Scanner 2: `osi-sca-image-scanner`

**Base Image**: `debian:bookworm-slim`

**Key Differences**: Only Trivy. Installs Docker CLI and AWS CLI for ECR.

**Entrypoint Flow**:
1. AWS credentials setup (if ECR).
2. Trivy image scan.
3. Generate SBOM.
4. Upload to Azure.
5. Write results to `/tmp/<app>/<service>/...`.

---

### Scanner 3: `osi-sast-scanner`

**Base Image**: `debian:bookworm-slim`

**Tools**: Gitleaks, TruffleHog, Semgrep, Trivy.

**Entrypoint Flow**:
1. Git clone repo.
2. Run SAST tools in parallel.
3. Merge results.
4. Generate SBOM.
5. Upload to Azure.
6. Write results.

---

## SECTION 8 — External Integrations

### Azure Blob Storage

**Credentials Flow**:
1. Worker loads `AZURE_STORAGE_CONNECTION_STRING` from root `.env`.
2. Worker writes to temp file.
3. Docker receives via `--env-file`.
4. Inside container: `blob_storage.py` uploads.

**Upload Path Format**:
```
<app>/<service>/<branch>/<commit>/<scanner>/<timestamp>.json
```

**Fallback**: If connection string missing or invalid, uploads skipped (exit 0).

---

### Git Repository Access

**Clone Logic**:
```bash
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" /tmp/repo
```

**SSL Verification**: If `IS_HOSTED_ON_PREM=True`, disable SSL verification.

**Commit Resolution**: `git rev-parse "$BRANCH"` resolves branch to commit SHA.

---

## SECTION 9 — Output Schema

### Source SCA Merged Result

**File**: `/tmp/<app>/<service>/<branch>/<commit>/osi-sca-source-scanner/<timestamp>.json`

**Structure**:
```json
{
  "metadata": {
    "scanner_agent_id": "agent-001",
    "scan_job_id": "1001"
  },
  "tools": {
    "trivy": {...},
    "grype": {...}
  }
}
```

---

### CycloneDX SBOM

**Format**: CycloneDX 1.4 JSON.

**Generated By**: Trivy with OSI metadata injection.

---

## SECTION 10 — Network Topology

| Port | Service | Bind | Role |
|---|---|---|---|
| 9000 | SBOM Processor (stub) | none | Result sink (no listener) |
| 9001 | secops-polling-migration | 0.0.0.0:9001 | HTTP API |
| 9002 | zmq_worker.py | 0.0.0.0:9002 | ZMQ REP dispatcher |

---

## SECTION 11 — Bug Fixes

### Fix 1: Hardcoded Ports → Environment Variables

**Issue**: Ports were hardcoded in Python source.

**Solution**: All ports now read from root `.env`.

**Impact**: Multi-environment deployments without code changes.

---

### Fix 2: Azure Connection Strings Truncated

**Issue**: Docker `-e KEY=VALUE` truncates on first `=`.

**Solution**: Use `--env-file` for Azure credentials.

**Impact**: Azure uploads now work reliably.

---

### Fix 3: Missing Required .env Variables Not Caught

**Issue**: Worker silently used hardcoded fallback.

**Solution**: `require_env()` enforces presence.

**Impact**: Clear error message if variable missing.

---

## SECTION 12 — How to Add a New Scanner

**Step 1**: Create directory `osi-my-scanner`.

**Step 2**: Create Dockerfile with base image, tool install, user creation, entrypoint copy.

**Step 3**: Create `entrypoint.sh` that:
- Clones repo or reads mounted folder.
- Runs scanning tools.
- Merges JSON results.
- Generates SBOM.
- Uploads to Azure.
- Writes to `/tmp/<app>/<service>/...`.

**Step 4**: Copy `blob_storage.py` and `upload_to_blob.py`.

**Step 5**: Register scanner in `zmq_worker.py`:
```python
VALID_SCANNER_TYPES = {
    "osi_my_scanner": "osi-my-scanner",
}
SCANNER_EXTRA_FIELDS = {
    "osi_my_scanner": ["repo_url"],
}
```

---

## SECTION 13 — Operational Runbook

### Starting the System

**Local Testing**:
```bash
cd /path/to/Osi_Scanner
python3 zmq_worker.py
# In another terminal:
python3 setup_and_run.py --scanner osi_sca_source_scanner ...
```

**Production**:
```bash
# Terminal 1: Start Worker
python3 zmq_worker.py &

# Terminal 2: Start FastAPI
cd secops-polling-migration
uvicorn src.main:app --host 0.0.0.0 --port 9001 &

# Clients now POST to http://<host>:9001/trigger-scan
```

---

### Debugging

**Check Worker Status**:
```bash
netstat -tlnp | grep 9002
```

**Tail Logs**:
```bash
tail -f /tmp/osi-zmq-worker.log
```

**Find Results**:
```bash
find /tmp -type d -name "my-app"
```

---

### Key Log Messages

| Message | Meaning | Action |
|---|---|---|
| `ZMQ worker listening on tcp://0.0.0.0:9002` | Worker started | None |
| `[<id>] Acknowledged` | Scan dispatched | None |
| `[<id>] Docker build error` | Build failed | Check Dockerfile |
| `[<id>] Scan completed successfully` | Success | Check `/tmp` for results |
| `[<id>] SBOM forward timed out` | Stub sink unreachable | Expected (no service listening on 9000) |

---

### Common Failures

| Symptom | Cause | Fix |
|---|---|---|
| `Address already in use :9002` | Port conflict | `fuser -k 9002/tcp` |
| `ZMQ_WORKER_ADDRESS must be set` | Missing `.env` | Create/populate `.env` |
| `[scan_id] Docker build error: manifest not found` | Base image pull failed | Check internet access, FROM line |
| `Azure upload fails` | Connection string missing | Set `AZURE_STORAGE_CONNECTION_STRING` |
| `Git clone fails` | Private repo / SSL issue | Set `IS_HOSTED_ON_PREM=True` for on-prem |

---

## SECTION 14 — Complete File Structure

**Root**:
- `.env` — Single source of truth (all config).
- `README.md` — High-level overview.
- `TECHNICAL.md` — This file (complete internals).
- `requirements.txt` — Python dependencies.
- `setup_and_run.py` — Local test harness.
- `zmq_worker.py` — Core dispatcher.

**Scanner Folders** (`osi-{sca-source,sca-image,sast}-scanner/`):
- `Dockerfile` — Multi-stage build.
- `entrypoint.sh` — Container logic.
- `blob_storage.py` — Azure upload helper.
- `upload_to_blob.py` — CLI wrapper for uploads.

**secops-polling-migration**:
- `src/main.py` — FastAPI app.
- `src/routes/polling_routes.py` — HTTP routes.
- `src/schemas/polling_schemas.py` — Request/response models.
- `src/controllers/polling_controller.py` — Business logic.
- `src/services/polling_service.py` — Service layer.
- `src/services/webhook_service.py` — Webhook orchestration.

---

## APPENDIX: Azure Storage Troubleshooting & Verification

### Overview of Recent Fixes

The Azure Storage implementation has been improved with:

1. **Enhanced logging** — Full trace of upload attempts, credential checks, retries
2. **Retry logic** — 3 attempts with exponential backoff (1s, 2s, 4s)
3. **Credential validation** — Explicit checks before attempting uploads
4. **Better error messages** — Clear indication of what went wrong and why

### Files Modified

- `blob_storage.py` — All 3 scanner folders (source, image, SAST)
- `upload_to_blob.py` — All 3 scanner folders

### How to Verify Azure Credentials Are Set

**Step 1: Check Root .env File**

```bash
cat .env | grep AZURE
```

Expected output (example):
```
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointProtocol=https;AccountName=myaccount;AccountKey=...
AZURE_CONTAINER_NAME=scan-results
```

**Important**: The connection string MUST NOT start with `your_`. Values like `your_connection_string` are treated as placeholders and ignored.

---

### Step 2: Verify Environment Variables Are Passed to Container

During a scan, the worker logs environment setup. Look for this in worker logs:

```
[<scan_id>] Building Docker image from osi-sca-source-scanner/
[<scan_id>] Running Docker container for osi_sca_source_scanner
```

The docker command will include `--env-file /tmp/osi_azure_<id>_<uuid>.env` if Azure credentials are configured.

---

### Step 3: Check Container Logs During Scan

Inside the container, `upload_to_blob.py` will log detailed messages. Check worker stdout:

**If credentials are configured (SUCCESS case)**:
```
[<scan_id>] [container] [INFO] Starting Azure Blob upload...
[<scan_id>] [container] [INFO] Local file: /tmp/merged.json
[<scan_id>] [container] [INFO] Blob path: app/service/branch/commit/scanner/file.json
[<scan_id>] [container] [INFO] Attempting to upload: /tmp/merged.json → app/service/branch/commit/...
[<scan_id>] [container] [INFO] File size: 12345 bytes
[<scan_id>] [container] [INFO] Azure BlobServiceClient created successfully
[<scan_id>] [container] [INFO] Uploading (attempt 1/3)...
[<scan_id>] [container] [INFO] Upload successful → https://myaccount.blob.core.windows.net/...
[<scan_id>] [container] [SUCCESS] Uploaded to: https://...
```

**If credentials are NOT configured (SKIP case)**:
```
[<scan_id>] [container] [WARNING] [SKIP] Azure credentials not configured in environment. Skipping upload.
[<scan_id>] [container] [WARNING] [INFO] To enable Azure uploads, set AZURE_STORAGE_CONNECTION_STRING in .env
```

**If there's an authentication error**:
```
[<scan_id>] [container] [ERROR] Azure upload failed: Invalid connection string format
[<scan_id>] [container] [ERROR] [ERROR] Azure upload failed: ...
[<scan_id>] [container] [WARNING] [SKIP] Upload skipped due to error
```

---

### Common Azure Upload Issues

| Symptom | Cause | Fix |
|---|---|---|
| `[SKIP] Azure credentials not configured` | AZURE_STORAGE_CONNECTION_STRING is empty or starts with `your_` | Set real connection string in .env |
| `Invalid connection string format` | Connection string is malformed or incomplete | Verify connection string in Azure Portal. Format: `DefaultEndpointProtocol=https;AccountName=...;AccountKey=...` |
| `Authentication failed. Access denied` | Account key is invalid or expired | Regenerate access key in Azure Portal |
| `Container does not exist` | Container name is wrong or doesn't exist | Check AZURE_CONTAINER_NAME matches actual container in Azure |
| `Upload attempt 1 failed: ... Retrying in 1s...` then `[SUCCESS]` | Transient network issue | Normal — retry logic will succeed. If all 3 attempts fail, will log error. |
| Upload times out | Network connectivity issue to Azure | Check firewall rules, ensure host can reach blob.core.windows.net |
| Results stored locally but NOT in Azure | Usually means credentials weren't passed to container | Verify --env-file was used. Check worker logs for `--env-file /tmp/osi_azure_...` |

---

### Manual Azure Upload Test

To test Azure uploads manually without running full scan:

```bash
# Terminal 1: Start worker
python3 zmq_worker.py

# Terminal 2: Create a test file and attempt upload
cd /tmp
echo '{"test": "data"}' > test-upload.json

# Run the upload script directly
python3 /path/to/osi-sca-source-scanner/upload_to_blob.py \
  /tmp/test-upload.json \
  "test-app/test-service/test-branch/test-file.json"
```

Expected output (on success):
```
[INFO] Starting Azure Blob upload...
[INFO] Local file: /tmp/test-upload.json
[INFO] Blob path: test-app/test-service/test-branch/test-file.json
[INFO] Attempting to upload: /tmp/test-upload.json → test-app/test-service/test-branch/test-file.json
[INFO] File size: 17 bytes
[INFO] Azure BlobServiceClient created successfully
[INFO] Uploading (attempt 1/3)...
[INFO] Upload successful → https://myaccount.blob.core.windows.net/scan-results/test-app/test-service/test-branch/test-file.json
[SUCCESS] Uploaded to: https://...
```

---

### Verifying Upload in Azure Portal

1. Go to Azure Portal → Storage Accounts → Your Account → Containers
2. Select the container (default: `scan-results`)
3. Navigate to the blob path: `test-app/test-service/test-branch/test-file.json`
4. File should be visible with timestamp and size

---

### Credential Format Reference

**Connection String** (recommended):
```
DefaultEndpointProtocol=https;AccountName=mystorageaccount;AccountKey=abc123...;EndpointSuffix=core.windows.net
```

**Alternative** (deprecated, less secure):
```
DefaultEndpointProtocol=https;AccountName=mystorageaccount;SharedAccessSignature=sv=2019-...
```

**Where to find**:
1. Azure Portal → Storage Accounts → Your Account → Access Keys
2. Copy "Connection string" field
3. Paste into `.env` as `AZURE_STORAGE_CONNECTION_STRING=...`

---

### Retry Behavior (NEW)

Upload failures now retry automatically:

- **Attempt 1**: Immediate
- **Attempt 2**: Wait 1 second, then retry
- **Attempt 3**: Wait 2 seconds, then retry
- **After attempt 3 fails**: Scan marked complete (non-fatal), warning logged

This handles transient network issues automatically. Results are always stored locally in `/tmp`, so Azure is only bonus (best-effort).

---

### Disable Azure Uploads

To skip Azure uploads entirely (even if credentials are set):

```bash
# Option 1: Remove/comment out AZURE_STORAGE_CONNECTION_STRING in .env
# AZURE_STORAGE_CONNECTION_STRING=...

# Option 2: Set it to empty
AZURE_STORAGE_CONNECTION_STRING=

# Option 3: Set it to a placeholder (will be ignored)
AZURE_STORAGE_CONNECTION_STRING=your_connection_string
```

Scans will still work; results will only be stored locally.

---

**End of TECHNICAL.md**

It does the following:

- Reads the root `.env` at startup.
- Verifies Docker, Python 3, pip3, and the Docker daemon.
- Installs `requirements.txt`.
- Clears the worker port with `fuser`.
- Starts `zmq_worker.py` and waits for the configured worker port to bind.
- Sends one scan request directly to `ZMQ_WORKER_ADDRESS` over a ZeroMQ REQ socket.
- Waits for the worker ACK using `ZMQ_ACK_TIMEOUT_MS`.
- Tails the worker log until completion, error, or timeout.

The script does not start an HTTP front end.

## Upstream Service

`secops-polling-migration` is the production caller. Its current FastAPI route surface is small:

- `GET /health`
- `POST /trigger-scan`

The request model is `ScanTriggerPayload` in `secops-polling-migration/src/schemas/polling_schemas.py`. It accepts a broader set of fields than the worker requires because the upstream service handles request shaping and SCM logic before dispatch.

## Output and Storage

Scanner output lives under the host-mounted `/tmp` tree. The worker mounts `/tmp:/tmp` into each scanner container so the container can write host-visible files without needing a separate volume definition.

Typical scan artifacts include:

- raw scanner results
- merged JSON reports
- CycloneDX SBOMs
- structured copies under `/tmp/<app>/<service>/<branch-or-version>/...`

Azure upload is handled by each scanner image through `upload_to_blob.py` and `blob_storage.py`. If `AZURE_STORAGE_CONNECTION_STRING` is not configured, the upload helper skips the upload and exits successfully.

## Failure Semantics

The current code intentionally fails open in a few places:

- Missing Azure credentials skip uploads instead of failing the scan.
- SBOM forwarding failures are logged but not treated as scan failures.
- The worker ACK is sent before Docker starts, so a later Docker failure does not change the initial request response.

The main hard failure points are invalid payloads, missing required environment variables, Docker build failures, and missing local prerequisites in `setup_and_run.py`.
