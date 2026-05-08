# ZeroMQ Integration Guide: FastAPI → Scanner Service

This document explains how to integrate the Docker-based vulnerability scanner with an external FastAPI service using ZeroMQ as the communication layer.

---

## 1. What is ZeroMQ

ZeroMQ (ZMQ) is a high-performance asynchronous messaging library. It is not a message broker like RabbitMQ or Kafka. There is no central server. Instead, ZeroMQ gives you smart sockets that handle queuing, reconnection, and message framing automatically.

Key properties:

- Brokerless — no separate server process to run or manage
- Language-agnostic — Python, Go, Java, C, and others all speak the same wire protocol
- Transport-agnostic — works over TCP, IPC (Unix sockets), or in-process
- Lightweight — adds minimal overhead compared to HTTP or gRPC

### Core concepts

| Concept | Meaning |
|---|---|
| Socket | A ZeroMQ endpoint. Not a raw OS socket. Has built-in buffering and retry logic. |
| Context | A container that manages one or more sockets. One per process is the standard. |
| Pattern | The communication style (request-reply, push-pull, pub-sub, etc.) |
| Endpoint | A string like `tcp://0.0.0.0:5555` that a socket binds to or connects to. |
| Frame | A single message unit. Messages can have multiple frames. |

### How sockets work

One side calls `bind()` — it listens at an address.
The other side calls `connect()` — it dials that address.

Either side can bind or connect depending on the pattern. ZeroMQ handles reconnection automatically if the other side goes down and comes back.

---

## 2. Messaging Patterns

ZeroMQ supports several patterns. The two most relevant here are:

### Request-Reply (REQ/REP)

```
FastAPI (REQ)  ──────►  Scanner (REP)
               ◄──────
```

- FastAPI sends a request and blocks until it gets a reply.
- The scanner receives the request, runs the scan, and sends back the result.
- Strictly synchronous: one send must be followed by one receive on each side.
- Simple to implement and reason about.

### Push-Pull (PUSH/PULL)

```
FastAPI (PUSH)  ──────►  Scanner (PULL)
Result Collector (PULL)  ◄──────  Scanner (PUSH)
```

- FastAPI pushes jobs into a queue. The scanner pulls jobs and processes them.
- Results are pushed to a separate collector socket.
- Fully asynchronous. FastAPI does not wait.
- Better for high-throughput or long-running scans.

### Which pattern to use for this project

Use **Request-Reply (REQ/REP)** for this scanner service.

Reasons:

- Vulnerability scans are triggered by a specific request and the caller needs the result or at least a confirmation.
- The scan is a discrete job with a clear start and end.
- REQ/REP maps naturally to the existing HTTP request model of FastAPI.
- Simpler to implement, debug, and monitor.
- No need for a separate result collector process.

If scan volume grows and scans take more than 30 seconds, consider upgrading to a **DEALER/ROUTER** pattern (async variant of REQ/REP) or **PUSH/PULL** with a result callback.

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        External Network                         │
│                                                                 │
│   ┌──────────────┐         TCP / ZeroMQ          ┌──────────┐  │
│   │   FastAPI    │  ──── REQ socket ────────►    │  Docker  │  │
│   │   Service    │  ◄─── REP socket ────────     │Container │  │
│   └──────────────┘                               └────┬─────┘  │
│                                                       │         │
│                                              ┌────────▼──────┐  │
│                                              │  zmq_worker   │  │
│                                              │   (Python)    │  │
│                                              └────────┬──────┘  │
│                                                       │         │
│                                              ┌────────▼──────┐  │
│                                              │ entrypoint.sh │  │
│                                              │ (Trivy/Grype/ │  │
│                                              │  OSV/DepCheck)│  │
│                                              └────────┬──────┘  │
│                                                       │         │
│                                              ┌────────▼──────┐  │
│                                              │  Azure Blob   │  │
│                                              │   Storage     │  │
│                                              └───────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Component roles

| Component | Role |
|---|---|
| FastAPI Service | Receives HTTP scan requests from clients, forwards them to the scanner via ZeroMQ REQ socket, waits for result, returns HTTP response |
| ZeroMQ channel | TCP transport between FastAPI and the Docker container |
| zmq_worker.py | Python process inside the container that listens on a REP socket, parses the job, calls entrypoint.sh as a subprocess, collects output, sends reply |
| entrypoint.sh | Unchanged scan logic — runs scanners, merges results, uploads to Azure Blob |

---

## 4. Communication Flow

```
Client
  │
  │  POST /scan  { repo_url, branch, app_name, ... }
  ▼
FastAPI
  │
  │  zmq REQ.send(json payload)
  ▼
Docker Container — zmq_worker.py (REP socket listening on tcp://0.0.0.0:5555)
  │
  │  subprocess.run(["bash", "entrypoint.sh"], env=payload_as_env)
  ▼
entrypoint.sh
  │  ├── Trivy
  │  ├── Grype
  │  ├── OSV Scanner
  │  └── Dependency-Check
  │
  │  Merges results → uploads to Azure Blob
  ▼
zmq_worker.py
  │
  │  zmq REP.send(json result: { status, blob_url, scan_id })
  ▼
FastAPI
  │
  │  HTTP 200 { status: "completed", blob_url: "https://..." }
  ▼
Client
```

---

## 5. Implementation

### 5.1 Python ZeroMQ worker inside the container

Create `zmq_worker.py` at `/app/zmq_worker.py` inside the container:

```python
import zmq
import json
import subprocess
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LISTEN_ADDRESS = os.environ.get("ZMQ_LISTEN_ADDRESS", "tcp://0.0.0.0:5555")
ENTRYPOINT = "/app/entrypoint.sh"


def build_env(payload: dict) -> dict:
    env = os.environ.copy()
    for key, value in payload.items():
        env[key.upper()] = str(value)
    return env


def run_scan(payload: dict) -> dict:
    env = build_env(payload)
    scan_id = payload.get("scan_job_id", "unknown")

    logger.info(f"Starting scan job: {scan_id}")

    result = subprocess.run(
        ["bash", ENTRYPOINT],
        env=env,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("SCAN_TIMEOUT_SECONDS", 3600)),
    )

    if result.returncode == 0:
        logger.info(f"Scan job {scan_id} completed successfully")
        return {
            "status": "completed",
            "scan_job_id": scan_id,
            "stdout": result.stdout[-2000:],
        }
    else:
        logger.error(f"Scan job {scan_id} failed: {result.stderr[-500:]}")
        return {
            "status": "failed",
            "scan_job_id": scan_id,
            "error": result.stderr[-2000:],
        }


def main():
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind(LISTEN_ADDRESS)
    logger.info(f"ZMQ worker listening on {LISTEN_ADDRESS}")

    while True:
        try:
            raw = socket.recv_string()
            payload = json.loads(raw)
            result = run_scan(payload)
            socket.send_string(json.dumps(result))
        except json.JSONDecodeError as e:
            socket.send_string(json.dumps({"status": "error", "error": f"Invalid JSON: {e}"}))
        except subprocess.TimeoutExpired:
            socket.send_string(json.dumps({"status": "error", "error": "Scan timed out"}))
        except Exception as e:
            logger.exception("Unexpected error")
            socket.send_string(json.dumps({"status": "error", "error": str(e)}))


if __name__ == "__main__":
    main()
```

### 5.2 FastAPI client side

```python
import zmq
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

ZMQ_SCANNER_ADDRESS = "tcp://scanner-container-host:5555"


class ScanRequest(BaseModel):
    app_name: str
    service_name: str
    repo_url: str
    branch: str
    version: str
    is_hosted_on_prem: str = "False"
    scanner_agent_id: str = ""
    scan_job_id: str = ""
    app_service_id: str = ""
    base_url: str = ""
    auth_token: str = ""
    service_environment_id: str = ""


def get_zmq_socket():
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.setsockopt(zmq.RCVTIMEO, 3_600_000)  # 1 hour timeout in ms
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(ZMQ_SCANNER_ADDRESS)
    return socket


@app.post("/scan")
def trigger_scan(request: ScanRequest):
    socket = get_zmq_socket()
    try:
        socket.send_string(json.dumps(request.model_dump()))
        raw = socket.recv_string()
        result = json.loads(raw)
    except zmq.Again:
        raise HTTPException(status_code=504, detail="Scanner did not respond in time")
    finally:
        socket.close()

    if result.get("status") == "failed":
        raise HTTPException(status_code=500, detail=result.get("error"))

    return result
```

### 5.3 Dockerfile changes

Add to the final stage of the Dockerfile:

```dockerfile
RUN pip install pyzmq azure-storage-blob python-dotenv

COPY zmq_worker.py /app/zmq_worker.py
COPY blob_storage.py /app/blob_storage.py
COPY upload_to_blob.py /app/upload_to_blob.py

EXPOSE 5555

CMD ["python3", "/app/zmq_worker.py"]
```

The `CMD` starts the ZeroMQ worker. The worker calls `entrypoint.sh` as a subprocess when a job arrives.

### 5.4 Environment variables for the container

```bash
ZMQ_LISTEN_ADDRESS=tcp://0.0.0.0:5555
SCAN_TIMEOUT_SECONDS=3600
AZURE_STORAGE_CONNECTION_STRING=<connection_string>
AZURE_STORAGE_CONTAINER=scan-results
```

---

## 6. Running with Docker Compose

```yaml
version: "3.8"

services:
  scanner:
    build: .
    ports:
      - "5555:5555"
    environment:
      - ZMQ_LISTEN_ADDRESS=tcp://0.0.0.0:5555
      - SCAN_TIMEOUT_SECONDS=3600
      - AZURE_STORAGE_CONNECTION_STRING=${AZURE_STORAGE_CONNECTION_STRING}
      - AZURE_STORAGE_CONTAINER=${AZURE_STORAGE_CONTAINER}
    volumes:
      - scan-results:/home/appsecuser/scan-results

  fastapi:
    build: ./fastapi-service
    ports:
      - "8000:8000"
    environment:
      - ZMQ_SCANNER_ADDRESS=tcp://scanner:5555
    depends_on:
      - scanner

volumes:
  scan-results:
```

---

## 7. Message Schema

### Request (FastAPI → Scanner)

```json
{
  "app_name": "my-app",
  "service_name": "auth-service",
  "repo_url": "https://github.com/org/repo.git",
  "branch": "main",
  "version": "1.0.0",
  "is_hosted_on_prem": "False",
  "scanner_agent_id": "agent-001",
  "scan_job_id": "job-abc123",
  "app_service_id": "svc-001",
  "base_url": "https://api.example.com",
  "auth_token": "<token>",
  "service_environment_id": "env-prod"
}
```

### Reply (Scanner → FastAPI)

Success:
```json
{
  "status": "completed",
  "scan_job_id": "job-abc123",
  "stdout": "...[last 2000 chars of scan output]..."
}
```

Failure:
```json
{
  "status": "failed",
  "scan_job_id": "job-abc123",
  "error": "...[stderr output]..."
}
```

---

## 8. Best Practices

### Security

- Never pass raw credentials in the ZeroMQ message body. Use environment variables injected at container startup or a secrets manager.
- Enable ZeroMQ CurveZMQ encryption if the channel crosses untrusted networks. For internal Docker networks, plain TCP is acceptable.
- Run the container as a non-root user (already done via `appsecuser`).
- Restrict the ZeroMQ port (5555) to internal network only — do not expose it publicly.

### Fault tolerance

- Set `RCVTIMEO` on the FastAPI REQ socket so it does not block forever if the scanner crashes.
- Set `LINGER=0` so the socket closes immediately on disconnect without waiting to drain.
- The worker catches all exceptions and always sends a reply — a REP socket that does not reply will deadlock the REQ side.
- Add a health check endpoint in the worker (a separate HTTP server or a second ZMQ socket) so orchestrators like Kubernetes can detect unhealthy workers.

### Scalability

- To run multiple scanner workers, switch from REQ/REP to **DEALER/ROUTER** or use a **PUSH/PULL** pattern with a shared job queue.
- Each worker container pulls jobs independently — ZeroMQ distributes messages round-robin across connected PULL sockets automatically.
- For very high scan volumes, add a ZeroMQ proxy (broker device) between FastAPI and the workers to fan out jobs.

### Observability

- Log the `scan_job_id` in every log line inside `zmq_worker.py` and `entrypoint.sh` for end-to-end traceability.
- Emit scan duration as a metric (start time before subprocess, end time after).
- Store the full stdout/stderr of each scan run alongside the JSON result in Azure Blob for post-mortem debugging.

---

## 9. Why Not HTTP Between Services

| Concern | HTTP | ZeroMQ |
|---|---|---|
| Broker required | No | No |
| Long-running jobs | Needs timeout tuning, keep-alive | Native, no HTTP timeout issues |
| Overhead | Headers, TLS handshake per request | Minimal framing |
| Async fan-out | Needs load balancer | Built into PUSH/PULL |
| Simplicity | Very familiar | Slightly more setup |

For scans that can take 10–60 minutes, ZeroMQ is a better fit than HTTP because there are no proxy timeouts, no keep-alive issues, and no need for polling or webhooks to get the result.

---

## 10. End-to-End Summary

1. FastAPI receives `POST /scan` with repo and metadata.
2. FastAPI opens a ZeroMQ REQ socket and sends the payload as JSON.
3. The Docker container's `zmq_worker.py` receives the message on its REP socket.
4. The worker sets environment variables from the payload and calls `bash entrypoint.sh` as a subprocess.
5. `entrypoint.sh` clones the repo, runs Trivy, Grype, OSV Scanner, and Dependency-Check in parallel, merges results, and uploads to Azure Blob.
6. The worker sends back a JSON reply with status and scan job ID.
7. FastAPI returns the result to the original HTTP caller.

The scan logic, JSON structure, blob path convention, and all scanner behavior remain completely unchanged. ZeroMQ only replaces the mechanism by which the job is triggered and the result is returned.
