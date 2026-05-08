from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os
from src.constants.polling_constants import DefaultValues
import re

class Settings(BaseSettings):
    base_host: str = DefaultValues.BASE_HOST
    default_port: str = DefaultValues.DEFAULT_PORT
    default_auth_token: str = DefaultValues.DEFAULT_AUTH_TOKEN
    
    aws_region: str = DefaultValues.AWS_REGION

    # ZeroMQ Configuration
    SERVICE_ID: str = "secops-polling"
    ZMQ_HOST: str = "0.0.0.0"
    ZMQ_PORT: int = 9001
    ZMQ_PATTERN: str = "REP"

    # Outbound sink routing
    ZMQ_RESULT_SINK_SERVICE_ID: str = "result-manager"

    # OSI Scanner worker (outbound scan dispatch)
    OSI_WORKER_ADDRESS: str = "tcp://localhost:9002"
    OSI_WORKER_TIMEOUT_MS: int = 10000

    # Processing concurrency hardening
    ZMQ_WORKERS: int = 5
    ZMQ_QUEUE_MAXSIZE: int = 1000
    
    # Use .env file if it exists
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

def get_api_config(tenant: str = DefaultValues.TENANT, api_domain_url: Optional[str] = None) -> dict:
    if api_domain_url:
        # Avoid forcing HTTPS for localhost or IP addresses
        local_host_patterns = ["localhost", "127.0.0.1"]
        is_ip_or_local = any(host_pattern in api_domain_url for host_pattern in local_host_patterns)
        
        hostname_match = re.search(r"http://([^:/]+)", api_domain_url)
        if hostname_match:
            hostname = hostname_match.group(1)
            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", hostname):
                is_ip_or_local = True

        if api_domain_url.startswith("http://") and not is_ip_or_local:
            api_domain_url = api_domain_url.replace("http://", "https://", 1)
            
        web_api_url = f"{api_domain_url.rstrip('/')}{DefaultValues.API_V1_PATH}"
        auth_token = settings.default_auth_token
        return {"web_api_url": web_api_url, "auth_token": auth_token}
        
    if tenant == DefaultValues.TENANT:
        port = settings.default_port
        auth_token = settings.default_auth_token
    else:
        tenant_upper = tenant.upper()
        port = os.environ.get(f"{tenant_upper}_PORT")
        auth_token = os.environ.get(f"{tenant_upper}_AUTH_TOKEN")
        if not port or not auth_token:
            raise ValueError(f"Missing configuration for tenant '{tenant}'. Required: {tenant_upper}_PORT and {tenant_upper}_AUTH_TOKEN")
            
    protocol = "https" if port == DefaultValues.HTTPS_PORT else "http"
    web_api_url = f"{protocol}://{settings.base_host}:{port}{DefaultValues.API_V1_PATH}"
    return {"web_api_url": web_api_url, "auth_token": auth_token}
