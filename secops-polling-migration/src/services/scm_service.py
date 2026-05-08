import base64
import requests
import boto3
from typing import Optional
from src.utils.logger import logger
from src.schemas.polling_schemas import ScanTriggerPayload
from src.constants.polling_constants import DefaultValues, SCMType

class SCMService:
    @staticmethod
    def get_default_branch(payload: ScanTriggerPayload, headers: dict, repo_path: str = None, scm_base: str = None, project_id: str = None) -> str:
        if payload.repo_branch:
            return payload.repo_branch
            
        for branch in DefaultValues.BRANCHES:
            try:
                if repo_path:
                    test_url = f"https://api.github.com/repos/{repo_path}/commits/{branch}"
                    response = requests.get(test_url, headers=headers)
                elif scm_base and project_id:
                    test_url = f"https://{scm_base}/api/v4/projects/{project_id}/repository/commits/{branch}"
                    response = requests.get(test_url, headers=headers, verify=False)
                else:
                    continue
                    
                if response.status_code == 200:
                    return branch
            except Exception as e:
                logger.error(f"Error checking default branch: {e}")
                
        return DefaultValues.DEFAULT_BRANCH

    @classmethod
    def get_latest_commit_github(cls, payload: ScanTriggerPayload) -> Optional[str]:
        headers = {}
        if payload.auth_token and payload.is_private_repo:
            headers["Authorization"] = f"token {payload.auth_token}"
            
        repo_path = payload.repo_url.split("github.com/")[-1].removesuffix(".git")
        branch = cls.get_default_branch(payload, headers, repo_path=repo_path)
        logger.info(f"Fetching latest commit for GitHub repo: {repo_path} on branch: {branch}")
        commits_url = f"https://api.github.com/repos/{repo_path}/commits/{branch}"
        
        try:
            commit_response = requests.get(commits_url, headers=headers, timeout=10)
            if commit_response.status_code == 200:
                sha = commit_response.json().get("sha")
                logger.info(f"GitHub API returned SHA: {sha}")
                return sha
            else:
                logger.error(f"GitHub API returned status {commit_response.status_code}: {commit_response.text}")
        except Exception as e:
            logger.error(f"GitHub commits API error: {e}")
            
        return None

    @classmethod
    def get_latest_commit_gitlab(cls, payload: ScanTriggerPayload) -> Optional[str]:
        headers = {"PRIVATE-TOKEN": payload.auth_token} if payload.auth_token else {}
        branch = cls.get_default_branch(payload, headers, scm_base=payload.scm_base, project_id=payload.project_id)
        commits_url = f"https://{payload.scm_base}/api/v4/projects/{payload.project_id}/repository/commits/{branch}"
        
        try:
            commit_response = requests.get(commits_url, headers=headers, verify=False, timeout=10)
            if commit_response.status_code == 200:
                return commit_response.json().get("id")
        except Exception as e:
            logger.error(f"GitLab commits API error: {e}")
            
        return None

    @staticmethod
    def get_codecommit_client_for_repo(payload: ScanTriggerPayload):
        client = boto3.client(
            "codecommit",
            aws_access_key_id=payload.aws_access_key,
            aws_secret_access_key=payload.aws_secret_key,
            region_name=payload.aws_region or DefaultValues.AWS_REGION
        )
        sts_client = boto3.client(
            "sts",
            aws_access_key_id=payload.aws_access_key,
            aws_secret_access_key=payload.aws_secret_key,
            region_name=payload.aws_region or DefaultValues.AWS_REGION
        )
        return client, sts_client

    @staticmethod
    def get_ecr_client_for_repo(payload: ScanTriggerPayload):
        return boto3.client(
            "ecr",
            aws_access_key_id=payload.aws_access_key,
            aws_secret_access_key=payload.aws_secret_key,
            region_name=payload.aws_region or DefaultValues.AWS_REGION
        )

    @classmethod
    def get_latest_commit_codecommit(cls, payload: ScanTriggerPayload) -> Optional[str]:
        repo_name = payload.repo_name or (payload.repo_url.split("/")[-1] if payload.repo_url else None)
        if not repo_name:
            return None
            
        client, _ = cls.get_codecommit_client_for_repo(payload)
        try:
            branch_info = client.get_branch(repositoryName=repo_name, branchName=payload.repo_branch)
            return branch_info['branch']['commitId']
        except Exception as e:
            logger.error(f"CodeCommit error: {e}")
            return None

    @classmethod
    def get_latest_commit_azure(cls, payload: ScanTriggerPayload) -> Optional[str]:
        if not payload.repo_url:
            return None
            
        try:
            if "@dev.azure.com/" in payload.repo_url:
                parts = payload.repo_url.split("@dev.azure.com/")[1]
                org_project = parts.split("/_git/")[0]
                organization = org_project.split("/")[0]
                project_name = org_project.split("/")[1] if "/" in org_project else org_project
                repo_name = payload.repo_name or (parts.split("/_git/")[1] if "/_git/" in parts else parts.split("/")[-1])
            else:
                return None
        except Exception:
            return None

        headers = {
            "Authorization": f"Basic {base64.b64encode(f':{payload.auth_token}'.encode()).decode()}",
            "Content-Type": "application/json"
        }

        list_url = f"https://dev.azure.com/{organization}/{project_name}/_apis/git/repositories/{repo_name}/commits?searchCriteria.itemVersion.versionType=branch&searchCriteria.itemVersion.version={payload.repo_branch}&$top=1&api-version=6.0"
        
        try:
            response = requests.get(list_url, headers=headers)
            if response.status_code == 200:
                commits = response.json().get("value", [])
                if commits:
                    return commits[0].get("commitId")
        except Exception as e:
            logger.error(f"Azure API error: {e}")
            
        return None

    @classmethod
    def get_latest_tag_ecr(cls, payload: ScanTriggerPayload) -> Optional[str]:
        if not payload.image_uri:
            return None
            
        try:
            registry_part, repo_part = payload.image_uri.split(".amazonaws.com/", 1)
            repository_name = repo_part.split(":")[0] if ":" in repo_part else repo_part
        except Exception:
            return None
            
        client = cls.get_ecr_client_for_repo(payload)
        try:
            list_response = client.list_images(repositoryName=repository_name, maxResults=100)
            if not list_response.get('imageIds'):
                return None
                
            describe_response = client.describe_images(repositoryName=repository_name, imageIds=list_response['imageIds'])
            images = describe_response.get('imageDetails', [])
            images.sort(key=lambda x: x.get('imagePushedAt', 0), reverse=True)
            
            for image in images:
                if image.get('imageTags'):
                    return image['imageTags'][0]
        except Exception as e:
            logger.error(f"ECR API error: {e}")
        return None

    @staticmethod
    def get_health_check(payload: ScanTriggerPayload) -> Optional[str]:
        if not payload.target_url:
            return None
        try:
            response = requests.get(payload.target_url, timeout=120, verify=False)
            if response.status_code == 200:
                return payload.target_url
        except Exception as e:
            logger.error(f"Health check failed for {payload.target_url}: {e}")
        return None

    @classmethod
    def get_latest_commit(cls, scm_type: str, payload: ScanTriggerPayload) -> Optional[str]:
        if not scm_type:
            raise ValueError("scm_type is required")
            
        scm_type = scm_type.lower()
        if scm_type == SCMType.GITHUB:
            return cls.get_latest_commit_github(payload)
        elif scm_type == SCMType.GITLAB:
            return cls.get_latest_commit_gitlab(payload)
        elif scm_type == SCMType.CODECOMMIT:
            return cls.get_latest_commit_codecommit(payload)
        elif scm_type == SCMType.AZURE:
            return cls.get_latest_commit_azure(payload)
        elif scm_type == SCMType.ECR:
            return cls.get_latest_tag_ecr(payload)
        elif scm_type == SCMType.DAST:
            return cls.get_health_check(payload)
        elif scm_type == SCMType.OTHER:
            return payload.version
        else:
            raise ValueError(f"Unsupported SCM type: {scm_type}")
