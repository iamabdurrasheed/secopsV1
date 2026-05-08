import json
import logging
import os
import subprocess
import threading

import zmq

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set in the root .env")
    return value

ZMQ_WORKER_ADDRESS    = require_env("ZMQ_WORKER_ADDRESS")
LISTEN_ADDRESS        = os.environ.get("ZMQ_LISTEN_ADDRESS", ZMQ_WORKER_ADDRESS)
SBOM_BACKEND_ADDRESS  = require_env("SBOM_BACKEND_ADDRESS")
SBOM_FORWARD_TIMEOUT_MS = int(os.environ.get("SBOM_FORWARD_TIMEOUT_MS", "5000"))

VALID_SCANNER_TYPES = {
    "osi_sca_source_scanner":  "osi-sca-source-scanner",
    "osi-sca-source-scanner":  "osi-sca-source-scanner",
    "osi_sca_image_scanner":   "osi-sca-image-scanner",
    "osi-sca-image-scanner":   "osi-sca-image-scanner",
    "osi_sast_scanner":        "osi-sast-scanner",
    "osi-sast-scanner":        "osi-sast-scanner",
}

COMMON_REQUIRED_FIELDS = [
    "app_name",
    "service_name",
    "scanner_name",
    "scan_job_id",
    "scanner_agent_id",
    "service_environment_id",
]

SCANNER_EXTRA_FIELDS = {
    "osi_sca_source_scanner":  ["repo_url", "repo_branch"],
    "osi-sca-source-scanner":  ["repo_url", "repo_branch"],
    "osi_sca_image_scanner":   ["image_uri"],
    "osi-sca-image-scanner":   ["image_uri"],
    "osi_sast_scanner":        ["repo_url"],
    "osi-sast-scanner":        ["repo_url"],
}

# Maps payload field names → env var names that entrypoints actually read.
# Only fields that differ between the payload key and the env var name are listed here.
# All other fields are uppercased directly (e.g. app_name → APP_NAME).
FIELD_ENV_MAP = {
    "repo_branch":       "BRANCH",
    "aws_access_key":    "AWS_ACCESS_KEY_ID",
    "aws_secret_key":    "AWS_SECRET_ACCESS_KEY",
    "aws_region":        "AWS_DEFAULT_REGION",
    "api_domain_url":    "BASE_URL",
    "is_hosted_on_prem": "IS_HOSTED_ON_PREM",
}


def load_azure_env() -> dict:
    """
    Load Azure Blob Storage credentials from the root .env (already loaded into
    os.environ by dotenv at module startup). Returns Azure vars so they can be
    injected into docker run as -e flags.
    """
    result = {}

    def present(value: str | None) -> bool:
        return bool(value and not value.startswith("your_"))

    for key in (
        "AZURE_STORAGE_ACCOUNT",
        "AZURE_STORAGE_KEY",
        "AZURE_CONTAINER_NAME",
        "AZURE_STORAGE_CONNECTION_STRING",
        "AZURE_STORAGE_CONTAINER",
    ):
        value = os.environ.get(key)
        if present(value):
            result[key] = value

    if "AZURE_STORAGE_CONTAINER" not in result and "AZURE_CONTAINER_NAME" in result:
        result["AZURE_STORAGE_CONTAINER"] = result["AZURE_CONTAINER_NAME"]

    if not result.get("AZURE_STORAGE_CONNECTION_STRING"):
        logger.warning("AZURE_STORAGE_CONNECTION_STRING not set in root .env — Azure upload will be skipped")
    return result


def _write_azure_env_file(azure_env: dict, scan_job_id: str) -> str | None:
    """
    Write Azure credentials to a temp env file so Docker receives them intact.
    Docker -e KEY=VALUE truncates values that contain '=' (e.g. connection strings).
    --env-file passes the raw value without re-parsing on '='.
    Returns the file path, or None if azure_env is empty.
    """
    if not azure_env:
        return None
    import tempfile
    f = tempfile.NamedTemporaryFile(
        mode="w", prefix=f"osi_azure_{scan_job_id}_", suffix=".env",
        delete=False, dir="/tmp"
    )
    for key, value in azure_env.items():
        f.write(f"{key}={value}\n")
    f.close()
    return f.name


def build_env_args(payload: dict) -> list:
    """
    Build the -e KEY=VALUE list for docker run from payload fields only.
    Azure credentials are handled separately via --env-file to preserve
    connection strings that contain '=' characters.
    """
    env = {}

    for key, value in payload.items():
        if value is None:
            continue
        env_key = FIELD_ENV_MAP.get(key, key.upper())
        env[env_key] = str(value)

    args = []
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return args


def validate_payload(payload: dict) -> str | None:
    scanner_name = payload.get("scanner_name", "")
    if scanner_name not in VALID_SCANNER_TYPES:
        return (
            f"Unsupported scanner_name: '{scanner_name}'. "
            f"Must be one of: osi-sca-source-scanner, osi-sca-image-scanner, osi-sast-scanner"
        )

    for field in COMMON_REQUIRED_FIELDS:
        if not payload.get(field):
            return f"Missing required field: {field}"

    for field in SCANNER_EXTRA_FIELDS[scanner_name]:
        if not payload.get(field):
            return f"Missing required field: {field}"

    return None


def forward_sbom(payload: dict, scan_job_id: str) -> None:
    """
    Forward SBOM result metadata to the downstream backend via ZMQ PUSH socket.
    Stub — backend does not exist yet. Failures are non-fatal.
    """
    message = {
        "event": "scan_completed",
        "scan_job_id": scan_job_id,
        "scanner_name": payload.get("scanner_name"),
        "app_name": payload.get("app_name"),
        "service_name": payload.get("service_name"),
        "service_environment_id": payload.get("service_environment_id"),
        "app_service_id": payload.get("app_service_id"),
        "scanner_agent_id": payload.get("scanner_agent_id"),
    }

    context = zmq.Context()
    socket = context.socket(zmq.PUSH)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt(zmq.SNDTIMEO, SBOM_FORWARD_TIMEOUT_MS)

    try:
        socket.connect(SBOM_BACKEND_ADDRESS)
        socket.send_string(json.dumps(message))
        logger.info(f"[{scan_job_id}] SBOM result forwarded to {SBOM_BACKEND_ADDRESS}")
    except zmq.Again:
        logger.warning(f"[{scan_job_id}] SBOM forward timed out — backend unreachable at {SBOM_BACKEND_ADDRESS}. Skipping.")
    except Exception as e:
        logger.warning(f"[{scan_job_id}] SBOM forward failed: {e}. Skipping.")
    finally:
        socket.close()
        context.term()


def run_docker_scan(payload: dict) -> None:
    scanner_name   = payload["scanner_name"]
    folder         = os.path.join(PROJECT_ROOT, VALID_SCANNER_TYPES[scanner_name])
    image_tag      = f"osi-scanner-{scanner_name}"
    scan_job_id    = str(payload.get("scan_job_id", "unknown"))

    azure_env      = load_azure_env()
    env_args       = build_env_args(payload)
    azure_env_file = _write_azure_env_file(azure_env, scan_job_id)

    app_name     = payload.get("app_name", "unknown")
    service_name = payload.get("service_name", "unknown")
    branch       = str(payload.get("repo_branch", "unknown")).replace("/", "-")
    host_base    = f"/tmp/{app_name}/{service_name}/{branch}"

    try:
        logger.info(f"[{scan_job_id}] Building Docker image from {folder}/")
        build_result = subprocess.run(
            ["docker", "build", "-t", image_tag, folder],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        if build_result.returncode != 0:
            logger.error(f"[{scan_job_id}] Docker build error:\n{build_result.stderr[-2000:]}")
            forward_sbom(payload, scan_job_id)
            return

        docker_cmd = ["docker", "run", "--rm", "-v", "/tmp:/tmp"] + env_args
        if azure_env_file:
            docker_cmd += ["--env-file", azure_env_file]
        docker_cmd += [image_tag]

        logger.info(f"[{scan_job_id}] Running Docker container for {scanner_name}")
        result = subprocess.run(
            docker_cmd,
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                logger.info(f"[{scan_job_id}] [container] {line}")
        if result.returncode != 0:
            logger.error(f"[{scan_job_id}] Docker error:\n{result.stderr[-2000:]}")
        else:
            logger.info(f"[{scan_job_id}] Scan completed successfully")
            logger.info(f"[{scan_job_id}] Results saved under: {host_base}/<commit-sha>/")
    except subprocess.CalledProcessError as e:
        logger.error(f"[{scan_job_id}] Docker error: {e.stderr[-1000:]}")
    finally:
        if azure_env_file:
            try:
                os.remove(azure_env_file)
            except OSError:
                pass

    forward_sbom(payload, scan_job_id)


def main() -> None:
    context = zmq.Context()
    socket  = context.socket(zmq.REP)
    socket.bind(LISTEN_ADDRESS)
    logger.info(f"ZMQ worker listening on {LISTEN_ADDRESS}")
    logger.info(f"SBOM forwarding target: {SBOM_BACKEND_ADDRESS} (stub — backend not yet connected)")

    while True:
        try:
            raw     = socket.recv_string()
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            socket.send_string(json.dumps({
                "status":    "rejected",
                "scan_job_id": None,
                "error":     f"Invalid JSON: {e}",
            }))
            continue

        scan_job_id = str(payload.get("scan_job_id", "unknown"))
        error       = validate_payload(payload)

        if error:
            logger.warning(f"[{scan_job_id}] Rejected: {error}")
            socket.send_string(json.dumps({
                "status":    "rejected",
                "scan_job_id": scan_job_id,
                "error":     error,
            }))
            continue

        socket.send_string(json.dumps({
            "status":    "acknowledged",
            "scan_job_id": scan_job_id,
            "message":   "Payload validated. Scan starting.",
        }))
        logger.info(f"[{scan_job_id}] Acknowledged — starting scan in background")

        threading.Thread(target=run_docker_scan, args=(payload,), daemon=True).start()


if __name__ == "__main__":
    main()
