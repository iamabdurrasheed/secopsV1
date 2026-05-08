from fastapi import Request
from src.schemas.polling_schemas import ScanTriggerPayload, ScanResponse
from src.services.polling_service import PollingService

class PollingController:
    @staticmethod
    async def run_api_trigger(request: Request, payload: ScanTriggerPayload, tenant: str = "default") -> ScanResponse:
        resource_path = request.url.path
        return await PollingService.run_api_trigger(payload, tenant, resource_path)

    @staticmethod
    async def run_scheduled_polling():
        """
        Scheduled polling endpoint placeholder. 
        In Lambda this was triggered by EventBridge and interacted directly with Django ORM `RepoMetadata`.
        In FastAPI, you should implement this logic using SQLAlchemy or a similar ORM, 
        and trigger it using a task scheduler like Celery or APScheduler.
        """
        # Placeholder for SQLAlchemy / DB logic to fetch repos and trigger scans
        return {"status": "Scheduled polling executed"}
