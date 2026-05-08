import json
import zmq
import zmq.asyncio
import httpx
import boto3
from datetime import datetime
from src.utils.logger import logger
from src.schemas.polling_schemas import ScanTriggerPayload
from src.utils.config import get_api_config, settings
from src.constants.polling_constants import DefaultValues, ScanStatus

class WebhookService:
    @staticmethod
    def validate_image_exists(payload: ScanTriggerPayload) -> bool:
        if not payload.image_uri:
            return True
            
        try:
            if ".dkr.ecr." not in payload.image_uri or ".amazonaws.com/" not in payload.image_uri:
                return True
                
            registry_part, repo_part = payload.image_uri.split(".amazonaws.com/", 1)
            repository_name, tag = repo_part.rsplit(":", 1) if ":" in repo_part else (repo_part, DefaultValues.IMAGE_TAG)
            region = registry_part.split(".dkr.ecr.")[1].split(".")[0]
            
            ecr_client = boto3.client(
                "ecr",
                aws_access_key_id=payload.aws_access_key,
                aws_secret_access_key=payload.aws_secret_key,
                region_name=region
            )
            
            response = ecr_client.describe_images(
                repositoryName=repository_name,
                imageIds=[{"imageTag": tag}]
            )
            return bool(response.get("imageDetails"))
        except Exception as e:
            logger.error(f"Image validation failed: {e}")
            return False

    @staticmethod
    async def create_scan_job(config: dict, payload: ScanTriggerPayload) -> int:
        create_url = f"{config['web_api_url']}{DefaultValues.SCAN_JOBS_ENDPOINT}"
        logger.info(f"Creating scan job at: {create_url}")
        
        run_version = payload.version
        if payload.image_uri and ":" in payload.image_uri:
            run_version = payload.image_uri.split(":")[-1]
            
        create_payload = {
            "run_on": datetime.now().strftime(DefaultValues.DATETIME_FORMAT),
            "run_status": ScanStatus.IN_PROGRESS,
            "run_version": run_version,
            "appservice": payload.app_service_id,
            "scanner_agent_id": payload.scanner_agent_id,
            "service_environment_id": payload.service_environment_id,
            "initiated_by": payload.initiated_by,
            "scan_source": payload.scan_source
        }
        
        try:
            async with httpx.AsyncClient(verify=False, timeout=DefaultValues.DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    create_url,
                    json=create_payload,
                    headers={"Authorization": f"Token {config['auth_token']}"}
                )
                response.raise_for_status()
                result = response.json()
                job_id = result.get("data", {}).get("id")
                logger.info(f"Successfully created scan job with ID: {job_id}")
                return job_id
        except httpx.HTTPError as error:
            logger.error(f"Failed to create scan job: {error}")
            if hasattr(error, 'response') and error.response:
                logger.error(f"Response status: {error.response.status_code}")
                logger.error(f"Response body: {error.response.text}")
            raise Exception(f"Failed to create scan job: {str(error)}")
        except Exception as exception:
            logger.error(f"Unexpected error creating scan job: {exception}")
            raise

    @staticmethod
    async def update_scanjob_status(scan_job_id: int, status: str, config: dict, payload: ScanTriggerPayload = None):
        update_url = f"{config['web_api_url']}{DefaultValues.SCAN_JOBS_ENDPOINT}{scan_job_id}/"
        logger.info(f"Updating scan job {scan_job_id} status to: {status}")
        
        run_version = DefaultValues.VERSION
        if payload:
            if payload.image_uri and ":" in payload.image_uri:
                run_version = payload.image_uri.split(":")[-1]
            elif payload.repo_source_scm_type and payload.repo_source_scm_type.lower() != "dast":
                run_version = payload.version
                
        update_payload = {
            "run_status": status,
            "run_version": run_version,
            "scanner_agent_id": payload.scanner_agent_id if payload else None,
            "service_environment_id": payload.service_environment_id if payload else None,
            "initiated_by": payload.initiated_by if payload else DefaultValues.INITIATED_BY,
            "scan_source": payload.scan_source if payload else DefaultValues.SCAN_SOURCE
        }
        
        async with httpx.AsyncClient(verify=False, timeout=DefaultValues.DEFAULT_TIMEOUT) as client:
            response = await client.patch(
                update_url,
                json=update_payload,
                headers={"Authorization": f"Token {config['auth_token']}"}
            )
            response.raise_for_status()
            logger.info(f"Successfully updated scan job {scan_job_id} status")

    @classmethod
    async def trigger_scan(cls, payload: ScanTriggerPayload, tenant: str = DefaultValues.TENANT, resource_path: str = "/"):
        config = get_api_config(tenant, payload.api_domain_url)
        
        # Build dictionary from model to add extra dynamically
        payload_dict = payload.model_dump(exclude_unset=True)
        if "auth_token" in payload_dict:
            payload_dict["pat_token"] = payload_dict.pop("auth_token")
            
        payload_dict.update({
            "tenant": tenant,
            "base_url": config["web_api_url"],
            "auth_token": config["auth_token"]
        })
        
        scan_job_id = payload.scan_job_id
        if not scan_job_id:
            if payload.image_uri or payload.repo_image_scm_type == "ecr":
                if not cls.validate_image_exists(payload):
                    raise ValueError(f"Image not found: {payload.image_uri}")
            try:
                scan_job_id = await cls.create_scan_job(config, payload)
                logger.info(f"Scan job created with ID: {scan_job_id}")
            except Exception as e:
                import uuid
                scan_job_id = str(uuid.uuid4().int)[:8]
                logger.warning(f"create_scan_job failed ({e}) — using generated scan_job_id: {scan_job_id}")
            payload_dict["scan_job_id"] = scan_job_id
        else:
            if payload.image_uri or payload.repo_image_scm_type == "ecr":
                if not cls.validate_image_exists(payload):
                    raise ValueError(f"Image not found: {payload.image_uri}")
            try:
                await cls.update_scanjob_status(scan_job_id, ScanStatus.IN_PROGRESS, config, payload)
            except Exception as e:
                logger.warning(f"Could not update scan job status (non-fatal): {e}")

        # Send payload to OSI Scanner ZMQ worker
        ctx = zmq.asyncio.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, settings.OSI_WORKER_TIMEOUT_MS)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(settings.OSI_WORKER_ADDRESS)
        try:
            logger.info(f"Sending scan payload to OSI worker at {settings.OSI_WORKER_ADDRESS} scan_job_id={scan_job_id}")
            await sock.send_string(json.dumps(payload_dict))
            ack = json.loads(await sock.recv_string())
            logger.info(f"OSI worker ACK: {ack}")

            if ack.get("status") == "rejected":
                try:
                    await cls.update_scanjob_status(scan_job_id, ScanStatus.FAILED, config, payload)
                except Exception as update_error:
                    logger.error(f"Failed to update ScanJob {scan_job_id} to FAILED: {update_error}")
                raise Exception(f"OSI worker rejected payload: {ack.get('error')}")

            return {"success": True, "response": ack}
        except zmq.Again:
            try:
                await cls.update_scanjob_status(scan_job_id, ScanStatus.FAILED, config, payload)
            except Exception as update_error:
                logger.error(f"Failed to update ScanJob {scan_job_id} to FAILED: {update_error}")
            raise Exception(f"OSI worker unreachable at {settings.OSI_WORKER_ADDRESS} (timeout)")
        except Exception as trigger_error:
            try:
                await cls.update_scanjob_status(scan_job_id, ScanStatus.FAILED, config, payload)
            except Exception as update_error:
                logger.error(f"Failed to update ScanJob {scan_job_id} to FAILED: {update_error}")
            raise Exception(f"OSI worker dispatch failed: {str(trigger_error)}")
        finally:
            sock.close()
