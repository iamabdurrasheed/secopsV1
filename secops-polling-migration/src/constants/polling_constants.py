from enum import Enum

class SCMType(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"
    CODECOMMIT = "codecommit"
    AZURE = "azure"
    ECR = "ecr"
    DAST = "dast"
    OTHER = "other"

class ScanStatus(str, Enum):
    IN_PROGRESS = "In Progress"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"

class DefaultValues:
    TENANT = "default"
    VERSION = "1.0"
    INITIATED_BY = "system"
    SCAN_SOURCE = "web"
    BRANCHES = ["master", "main"]
    DEFAULT_BRANCH = "main"
    AWS_REGION = "us-east-1"
    IMAGE_TAG = "latest"
    DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S+05:30"
    NA = "NA"
    
    # Infrastructure Defaults
    BASE_HOST = "34.194.131.78"
    DEFAULT_PORT = "8000"
    DEFAULT_AUTH_TOKEN = "6991998e92fc3f015ec84cb20622cf7161211f21"
    
    # API Constants
    API_V1_PATH = "/api/v1"
    SCAN_JOBS_ENDPOINT = "/scan-jobs/"
    DEFAULT_TIMEOUT = 30.0
    SCAN_LAUNCH_TIMEOUT = 60.0
    HTTPS_PORT = "443"

class ScannerConstants:
    EXCLUDED_SCANNERS = ["rapid7-security-scanner"]
    EXCLUDE_VALIDATION = ["jfrog-source-scanner"]
    REPO_IMAGE_SCM_TYPE = "repo_image_scm_type"
    IMAGE_URI = "image_uri"
