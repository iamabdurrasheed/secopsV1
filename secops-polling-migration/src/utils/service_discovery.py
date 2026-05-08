"""
Service discovery registry.

Maps a human-readable service id to a ZMQ TCP address.
"""

from typing import Optional


SERVICE_DISCOVERY = {
    # Common scanner senders (used in local/test setups)
    "scanner-agent": {"host": "127.0.0.1", "port": 9001},
    "osi-scanner":   {"host": "127.0.0.1", "port": 9002},
    # Where this worker should PUSH results
    "result-manager": {"host": "127.0.0.1", "port": 9000},
}


def get_service_address(service_id: str) -> Optional[str]:
    """Returns the tcp address for a given service id (e.g. tcp://127.0.0.1:5557)."""

    config = SERVICE_DISCOVERY.get(service_id)
    if not config:
        return None
    return f"tcp://{config['host']}:{config['port']}"

