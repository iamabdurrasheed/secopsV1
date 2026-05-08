from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from src.constants.polling_constants import DefaultValues

class ScanTriggerPayload(BaseModel):
    app_name: Optional[str] = None
    service_name: Optional[str] = None
    scanner_name: str
    version: Optional[str] = DefaultValues.VERSION
    s3_bucket: Optional[str] = None
    scan_launcher_url: str
    
    app_service_id: Optional[str] = None
    scanner_agent_id: Optional[str] = None
    service_environment_id: Optional[str] = None
    
    image_uri: Optional[str] = None
    target_url: Optional[str] = None
    repo_image_scm_type: Optional[str] = None
    
    repo_url: Optional[str] = None
    repo_branch: Optional[str] = None
    repo_source_scm_type: Optional[str] = None
    repo_name: Optional[str] = None
    
    api_domain_url: Optional[str] = None
    scan_job_id: Optional[int] = None
    agent_mode: Optional[bool] = False
    
    aws_access_key: Optional[str] = None
    aws_secret_key: Optional[str] = None
    aws_region: Optional[str] = None
    
    auth_token: Optional[str] = None
    scm_base: Optional[str] = None
    project_id: Optional[str] = None
    is_private_repo: Optional[bool] = False
    
    initiated_by: str = DefaultValues.INITIATED_BY
    scan_source: str = DefaultValues.SCAN_SOURCE
    
    class Config:
        extra = "allow"

class ScanResponse(BaseModel):
    message: str
    execution_id: str
    scanner: Optional[str] = None
    domain: Optional[str] = None
    version: Optional[str] = None
    scan_type: Optional[str] = None
