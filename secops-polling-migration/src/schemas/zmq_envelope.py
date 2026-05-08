from typing import Any, Optional

from pydantic import BaseModel, Field


class ZMQEnvelope(BaseModel):
    """
    Standard message envelope for this service.

    Inbound JSON keys:
    - from: sender service/container identifier (human-readable, not an IP)
    - command: routing key for the handler
    - trace_id: correlation id
    - data: payload for the command
    """

    command: str = Field(..., description="Action to be performed")
    sender: str = Field(..., alias="from", description="Identifier of the sender service")
    trace_id: str = Field(..., description="Trace ID for message tracking")
    data: dict[str, Any] = Field(..., description="Payload for processing")

    class Config:
        populate_by_name = True


class ZMQAcknowledgement(BaseModel):
    command: str
    trace_id: str
    status: str
    message: Optional[str] = None

