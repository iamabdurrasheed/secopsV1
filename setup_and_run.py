#!/usr/bin/env python3
"""
OSI Scanner — Worker Startup & Test Script
==========================================
Starts the ZMQ worker and sends a test scan payload directly via ZMQ.
secops-polling-migration is the production upstream caller (port 9001).
This script is for local testing of the worker only.

Port layout:
  9000 — SBOM backend / result processor (downstream)
  9001 — secops-polling-migration (upstream caller)
  9002 — OSI Scanner ZMQ worker (this service)

Usage:
    # Source SCA scan
    python3 setup_and_run.py \\
        --scanner osi-sca-source-scanner \\
        --app-name my-app \\
        --service-name auth-service \\
        --repo-url https://github.com/org/repo.git \\
        --repo-branch main \\
        --version 1.0.0

    # Image SCA scan (public registry)
    python3 setup_and_run.py \\
        --scanner osi-sca-image-scanner \\
        --app-name my-app \\
        --service-name payment-api \\
        --image-uri docker.io/library/nginx:latest \\
        --repo-branch main \\
        --version 1.0

    # SAST scan
    python3 setup_and_run.py \\
        --scanner osi-sast-scanner \\
        --app-name my-app \\
        --service-name checkout-service \\
        --repo-url https://github.com/org/repo.git \\
        --repo-branch main
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

import zmq

# ─── Load root .env ───────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

# ─── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WORKER_LOG   = "/tmp/osi-zmq-worker.log"
SCAN_TIMEOUT = 600

# ─── Config from .env ─────────────────────────────────────────────────────────

def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set in the root .env")
    return value

ZMQ_WORKER_PORT      = int(require_env("ZMQ_WORKER_PORT"))
SBOM_PROCESSOR_PORT  = int(require_env("SBOM_PROCESSOR_PORT"))
ZMQ_LISTEN_ADDRESS   = require_env("ZMQ_LISTEN_ADDRESS")
ZMQ_WORKER_ADDRESS   = require_env("ZMQ_WORKER_ADDRESS")
SBOM_BACKEND_ADDRESS = require_env("SBOM_BACKEND_ADDRESS")
ZMQ_ACK_TIMEOUT_MS   = int(require_env("ZMQ_ACK_TIMEOUT_MS"))

# ─── Colour helpers ───────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
DIM    = "\033[2m"

def banner(text):
    print(f"\n{BOLD}{CYAN}{'─' * 70}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 70}{RESET}")

def step(text):   print(f"\n{BOLD}[STEP]{RESET} {text}")
def ok(text):     print(f"{GREEN}  ✓ {text}{RESET}")
def warn(text):   print(f"{YELLOW}  ⚠ {text}{RESET}")
def fail(text):   print(f"{RED}  ✗ {text}{RESET}")
def info(text):   print(f"    {text}")
def cmd_echo(c):  print(f"{DIM}  $ {c if isinstance(c, str) else ' '.join(c)}{RESET}")

# ─── Prerequisites ────────────────────────────────────────────────────────────

def check_prerequisites():
    banner("Phase 1 — Checking Prerequisites")
    for binary, flag, label in [
        ("docker",  "--version", "Docker"),
        ("python3", "--version", "Python 3"),
        ("pip3",    "--version", "pip3"),
    ]:
        step(f"Check {label}")
        result = subprocess.run([binary, flag], capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"{label} not found.")
            sys.exit(1)
        ok(result.stdout.strip() or result.stderr.strip())

    step("Check Docker daemon")
    result = subprocess.run(["docker", "ps"], capture_output=True, text=True)
    if result.returncode != 0:
        fail("Docker daemon is not running. Start it with: sudo systemctl start docker")
        sys.exit(1)
    ok("Docker daemon is running")

# ─── Dependencies ─────────────────────────────────────────────────────────────

def install_dependencies():
    banner("Phase 2 — Installing Dependencies")
    step("Install worker dependencies")
    subprocess.run(["pip3", "install", "-r", "requirements.txt", "--quiet"],
                   cwd=PROJECT_ROOT, check=True)
    ok("Worker dependencies installed")

# ─── Port cleanup ─────────────────────────────────────────────────────────────

def kill_existing_services():
    banner("Phase 0 — Clearing Ports")
    for port, name in [(ZMQ_WORKER_PORT, "ZMQ worker")]:
        step(f"Kill anything on port {port} ({name})")
        subprocess.run(f"fuser -k {port}/tcp", shell=True, capture_output=True)
        time.sleep(1)
        result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
        if f":{port}" in result.stdout:
            warn(f"Port {port} still in use — waiting 3s...")
            time.sleep(3)
        else:
            ok(f"Port {port} is free")

# ─── Worker ───────────────────────────────────────────────────────────────────

worker_proc = None

def start_worker(server_ip):
    global worker_proc
    banner("Phase 3 — Starting ZMQ Worker")

    step("Launch zmq_worker.py in background")
    env = os.environ.copy()
    env["ZMQ_LISTEN_ADDRESS"]   = ZMQ_LISTEN_ADDRESS
    env["SBOM_BACKEND_ADDRESS"] = SBOM_BACKEND_ADDRESS

    with open(WORKER_LOG, "w") as log:
        worker_proc = subprocess.Popen(
            ["python3", "zmq_worker.py"],
            cwd=PROJECT_ROOT, stdout=log, stderr=log, env=env,
        )

    info(f"Worker PID         : {worker_proc.pid}")
    info(f"Worker log         : {WORKER_LOG}")
    info(f"ZMQ listen address : {ZMQ_LISTEN_ADDRESS}")
    info(f"SBOM backend       : {SBOM_BACKEND_ADDRESS}")

    step(f"Wait for worker to bind on port {ZMQ_WORKER_PORT}")
    for i in range(20):
        time.sleep(1)
        result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
        if f":{ZMQ_WORKER_PORT}" in result.stdout:
            ok(f"Worker is listening on {ZMQ_LISTEN_ADDRESS}")
            return
        info(f"  Waiting... ({i + 1}/20)")

    fail(f"Worker did not bind on port {ZMQ_WORKER_PORT} within 20 seconds")
    _print_log_tail(WORKER_LOG, 20)
    cleanup()
    sys.exit(1)

# ─── Payload ──────────────────────────────────────────────────────────────────

def build_payload(args):
    return {
        "scanner_name":           args.scanner,
        "app_name":               args.app_name,
        "service_name":           args.service_name,
        "scan_job_id":            args.scan_job_id,
        "scanner_agent_id":       args.scanner_agent_id,
        "service_environment_id": args.environment,
        "app_service_id":         args.app_service_id,
        "auth_token":             args.auth_token,
        "repo_url":               args.repo_url,
        "repo_branch":            args.repo_branch,
        "is_hosted_on_prem":      "False",
        "version":                args.version,
        "image_uri":              args.image_uri,
        "aws_access_key":         args.aws_access_key,
        "aws_secret_key":         args.aws_secret_key,
        "aws_region":             args.aws_region,
        "api_domain_url":         args.api_domain_url,
        "initiated_by":           "system",
        "scan_source":            "web",
        "is_private_repo":        False,
        "agent_mode":             False,
    }

# ─── Send via ZMQ ─────────────────────────────────────────────────────────────

def send_scan(payload, server_ip):
    banner("Phase 4 — Sending Scan Request directly via ZMQ")

    step(f"ZMQ REQ → {ZMQ_WORKER_ADDRESS}")
    sensitive = {"aws_access_key", "aws_secret_key", "auth_token"}
    info("Payload:")
    for k, v in payload.items():
        if v is None:
            continue
        info(f"  {k}: {'***' if k in sensitive and v else v}")

    context = zmq.Context()
    socket  = context.socket(zmq.REQ)
    socket.setsockopt(zmq.RCVTIMEO, ZMQ_ACK_TIMEOUT_MS)
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(ZMQ_WORKER_ADDRESS)

    try:
        socket.send_string(json.dumps(payload))
        ack = json.loads(socket.recv_string())
        print()
        if ack.get("status") == "acknowledged":
            ok(f"Worker ACK — {ack.get('message')}")
            info(f"Response:\n{json.dumps(ack, indent=4)}")
            return ack
        else:
            fail(f"Worker rejected: {ack.get('error')}")
            info(f"Response:\n{json.dumps(ack, indent=4)}")
            cleanup()
            sys.exit(1)
    except zmq.Again:
        fail(f"Worker did not respond within {ZMQ_ACK_TIMEOUT_MS}ms — is it running on {ZMQ_WORKER_ADDRESS}?")
        cleanup()
        sys.exit(1)
    finally:
        socket.close()
        context.term()

# ─── Log tail ─────────────────────────────────────────────────────────────────

def _print_log_tail(path, lines=30):
    try:
        with open(path) as f:
            content = f.readlines()
        print(f"\n{DIM}--- last {lines} lines of {path} ---{RESET}")
        for line in content[-lines:]:
            print(f"{DIM}{line.rstrip()}{RESET}")
        print(f"{DIM}--- end ---{RESET}\n")
    except Exception:
        pass


def tail_worker_logs(scan_job_id):
    banner("Phase 5 — Watching Worker Logs")
    step(f"Tailing {WORKER_LOG} — waiting for scan job {scan_job_id}")
    info(f"Timeout : {SCAN_TIMEOUT} seconds")
    info("Press Ctrl+C to stop watching (scan continues in background)\n")

    completed_markers = [
        f"[{scan_job_id}] Scan completed successfully",
        f"[{scan_job_id}] SBOM result forwarded",
        f"[{scan_job_id}] SBOM forward timed out",
        f"[{scan_job_id}] SBOM forward failed",
    ]
    error_markers = [f"[{scan_job_id}] Docker error"]

    seen_lines   = 0
    deadline     = time.time() + SCAN_TIMEOUT
    final_status = None

    try:
        while time.time() < deadline:
            try:
                with open(WORKER_LOG) as f:
                    lines = f.readlines()
            except FileNotFoundError:
                time.sleep(1)
                continue

            for line in lines[seen_lines:]:
                line = line.rstrip()
                if not line:
                    continue
                if "ERROR" in line or "Docker error" in line:
                    print(f"{RED}{line}{RESET}")
                elif "WARNING" in line or "SKIP" in line:
                    print(f"{YELLOW}{line}{RESET}")
                else:
                    print(line)

                for marker in completed_markers:
                    if marker in line:
                        final_status = "completed"
                for marker in error_markers:
                    if marker in line:
                        final_status = "error"

            seen_lines = len(lines)
            if final_status:
                break
            time.sleep(1)

    except KeyboardInterrupt:
        warn("Log watching interrupted — scan is still running in background")
        return None

    return final_status

# ─── Report ───────────────────────────────────────────────────────────────────

def report(final_status, scan_job_id, server_ip):
    banner("Phase 6 — Final Report")

    if final_status == "completed":
        ok(f"Scan job {scan_job_id} completed successfully")
        info("Results uploaded to Azure Blob Storage (if credentials configured)")
        info(f"SBOM metadata pushed → {SBOM_BACKEND_ADDRESS}")
    elif final_status == "error":
        fail(f"Scan job {scan_job_id} encountered a Docker error")
        info(f"Check: {WORKER_LOG}")
    elif final_status is None:
        warn(f"Scan job {scan_job_id} did not complete within {SCAN_TIMEOUT}s")
        info(f"Scan may still be running. Check: tail -f {WORKER_LOG}")

    print()
    info(f"Worker log  : {WORKER_LOG}")
    info(f"Worker PID  : {worker_proc.pid if worker_proc else 'N/A'}")
    print()
    info("Port layout:")
    info(f"  {SBOM_BACKEND_ADDRESS}          — SBOM result processor")
    info(f"  {server_ip}:9001                — secops-polling-migration (upstream)")
    info(f"  {server_ip}:{ZMQ_WORKER_PORT}   — OSI Scanner ZMQ worker (this service)")
    print()
    info("Live log:  tail -f /tmp/osi-zmq-worker.log")
    info(f"Stop worker: kill {worker_proc.pid if worker_proc else '<pid>'}")

# ─── Cleanup ──────────────────────────────────────────────────────────────────

def cleanup(signum=None, frame=None):
    print(f"\n{YELLOW}Shutting down...{RESET}")
    if worker_proc and worker_proc.poll() is None:
        worker_proc.terminate()
        try:
            worker_proc.wait(timeout=5)
            ok("ZMQ worker stopped")
        except subprocess.TimeoutExpired:
            worker_proc.kill()
            warn("ZMQ worker force-killed")
    if signum is not None:
        sys.exit(0)

# ─── Args ─────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="OSI Scanner — Worker Startup & Test Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server-ip", default="localhost")
    parser.add_argument(
        "--scanner", required=True,
        choices=[
            "osi_sca_source_scanner", "osi-sca-source-scanner",
            "osi_sca_image_scanner",  "osi-sca-image-scanner",
            "osi_sast_scanner",       "osi-sast-scanner",
        ],
    )
    parser.add_argument("--app-name",     required=True)
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--scan-job-id",      default=1001, type=int)
    parser.add_argument("--scanner-agent-id", default="agent-001")
    parser.add_argument("--app-service-id",   default="svc-001")
    parser.add_argument("--environment",      default="dev")
    parser.add_argument("--auth-token",       default="")
    parser.add_argument("--api-domain-url",   default=None)
    parser.add_argument("--repo-url",         default=None)
    parser.add_argument("--repo-branch",      default=None)
    parser.add_argument("--version",          default=None)
    parser.add_argument("--image-uri",        default=None)
    parser.add_argument("--aws-access-key",   default=None)
    parser.add_argument("--aws-secret-key",   default=None)
    parser.add_argument("--aws-region",       default=None)
    parser.add_argument("--no-cleanup",       action="store_true")
    parser.add_argument("--scan-timeout",     type=int, default=SCAN_TIMEOUT)
    return parser.parse_args()

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    global SCAN_TIMEOUT
    SCAN_TIMEOUT = args.scan_timeout

    signal.signal(signal.SIGINT,  cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    banner("OSI Scanner — Worker Startup & Test")
    info(f"Scanner     : {args.scanner}")
    info(f"App         : {args.app_name} / {args.service_name}")
    info(f"Worker log  : {WORKER_LOG}")
    print()
    info("Port layout:")
    info(f"  9000 — SBOM result processor (downstream)")
    info(f"  9001 — secops-polling-migration (upstream caller)")
    info(f"  9002 — OSI Scanner ZMQ worker (this service)")

    check_prerequisites()
    install_dependencies()
    kill_existing_services()
    start_worker(args.server_ip)

    payload      = build_payload(args)
    ack          = send_scan(payload, args.server_ip)
    scan_job_id  = str(ack.get("scan_job_id", args.scan_job_id))
    final_status = tail_worker_logs(scan_job_id)

    report(final_status, scan_job_id, args.server_ip)

    if not args.no_cleanup:
        cleanup()
    else:
        warn("--no-cleanup set — worker is still running")
        info(f"  Worker PID: {worker_proc.pid}")


if __name__ == "__main__":
    main()
