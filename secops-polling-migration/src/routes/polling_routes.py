from fastapi import APIRouter, Request, status, Path
from src.schemas.polling_schemas import ScanTriggerPayload, ScanResponse
from src.controllers.polling_controller import PollingController

from src.constants.polling_constants import DefaultValues

router = APIRouter(tags=["Polling"])

@router.post("/trigger-scan", response_model=ScanResponse, status_code=status.HTTP_200_OK)
async def trigger_scan_default(request: Request, payload: ScanTriggerPayload):
    """
    Trigger a scan for the default tenant.
    Equivalent to API Gateway root path trigger without tenant context.
    """
    return await PollingController.run_api_trigger(request, payload, tenant=DefaultValues.TENANT)

# @router.post("/{tenant}/trigger-scan", response_model=ScanResponse, status_code=status.HTTP_200_OK)
# async def trigger_scan_tenant(request: Request, payload: ScanTriggerPayload, tenant: str = Path(...)):
#     """
#     Trigger a scan for a specific tenant.
#     Equivalent to API Gateway mapping /<tenant>/trigger-scan.
#     """
#     return await PollingController.run_api_trigger(request, payload, tenant=tenant)

# @router.post("/poll/scheduled", status_code=status.HTTP_200_OK)
# async def trigger_scheduled_polling():
#     """
#     Endpoint to trigger the scheduled polling manually.
#     Equivalent to EventBridge scheduled invocation.
#     """
#     return await PollingController.run_scheduled_polling()
