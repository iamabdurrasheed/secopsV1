import uuid
from datetime import datetime
from src.utils.logger import logger
from src.schemas.polling_schemas import ScanTriggerPayload, ScanResponse
from src.services.scm_service import SCMService
from src.services.webhook_service import WebhookService
from src.utils.exceptions import InvalidPayloadError, SCMIntegrationError, ScanLaunchError

from src.constants.polling_constants import ScannerConstants, SCMType

class PollingService:
    @staticmethod
    async def run_api_trigger(payload: ScanTriggerPayload, tenant: str, resource_path: str) -> ScanResponse:
        execution_id = str(uuid.uuid4())
        logger.info(f"[{execution_id}] API trigger started for scanner: {payload.scanner_name}")
        
        if payload.agent_mode:
            logger.info(f"[{execution_id}] Agent mode detected - triggering scan directly")

            # Ensure source-code scans always carry the latest commit hash as version
            try:
                if payload.repo_source_scm_type and payload.repo_source_scm_type.lower() not in {SCMType.DAST, SCMType.ECR}:
                    latest_commit = SCMService.get_latest_commit(payload.repo_source_scm_type, payload)
                    if latest_commit:
                        payload.version = latest_commit
            except Exception as e:
                logger.error(f"[{execution_id}] Failed to resolve latest commit for agent mode: {e}")

            await WebhookService.trigger_scan(payload, tenant, resource_path)
            return ScanResponse(
                message="Agent mode scan triggered",
                execution_id=execution_id,
                scanner=payload.scanner_name,
                version=payload.version,
            )

        has_image_uri = payload.image_uri is not None
        has_target_url = payload.target_url is not None
        has_repo_image_scm_type = payload.repo_image_scm_type is not None
        
        exclude_validation = ScannerConstants.EXCLUDE_VALIDATION
        
        if not has_image_uri and not has_target_url and not has_repo_image_scm_type and payload.scanner_name not in exclude_validation:
            if not payload.repo_branch or not payload.repo_source_scm_type:
                raise InvalidPayloadError(detail="Missing required fields: repo_branch, repo_source_scm_type")

        # Handle direct scan triggers (image scans, security scans, excluded scanners)
        if has_image_uri or has_repo_image_scm_type or has_target_url or payload.scanner_name in exclude_validation:
            scan_type = ScannerConstants.REPO_IMAGE_SCM_TYPE if has_repo_image_scm_type else ScannerConstants.IMAGE_URI
            if has_image_uri and payload.image_uri and ":" in payload.image_uri:
                payload.repo_branch = payload.image_uri.split(":")[-1]
            
            await WebhookService.trigger_scan(payload, tenant, resource_path)
            return ScanResponse(
                message="Image/Security scan triggered",
                execution_id=execution_id,
                scan_type=scan_type
            )

        scm_type = payload.repo_source_scm_type.lower()
        logger.info(f"[{execution_id}] Scanner {payload.scanner_name}: Processing {scm_type} source code scan")

        try:
            if scm_type == SCMType.DAST:
                if payload.scanner_name.lower() in ScannerConstants.EXCLUDED_SCANNERS:
                    payload.version = f"dast-scan-{datetime.utcnow().isoformat()}"
                    await WebhookService.trigger_scan(payload, tenant, resource_path)
                    return ScanResponse(message="DAST scan triggered", execution_id=execution_id, scanner=payload.scanner_name)

                domain_url = SCMService.get_health_check(payload)
                if domain_url:
                    payload.version = f"dast-scan-{datetime.utcnow().isoformat()}"
                    await WebhookService.trigger_scan(payload, tenant, resource_path)
                    return ScanResponse(message="DAST scan triggered", domain=domain_url, execution_id=execution_id)
                else:
                    raise SCMIntegrationError(detail="Missing or invalid domain URL for DAST")

            # Source Code Scans
            latest_commit = SCMService.get_latest_commit(scm_type, payload)
            if not latest_commit:
                raise SCMIntegrationError(detail=f"Failed to access repository or fetch commits from {scm_type}")

            if latest_commit != payload.version:
                payload.version = latest_commit
                await WebhookService.trigger_scan(payload, tenant, resource_path)
                return ScanResponse(message="Scan triggered", version=latest_commit, execution_id=execution_id)

            return ScanResponse(message="No new commit found", execution_id=execution_id)
            
        except Exception as e:
            logger.error(f"[{execution_id}] Error during scan trigger: {e}")
            raise ScanLaunchError(detail=str(e))
