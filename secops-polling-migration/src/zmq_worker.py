import asyncio
import copy
import json
import sys
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import zmq
import zmq.asyncio

from src.constants.polling_constants import DefaultValues
from src.schemas.polling_schemas import ScanResponse, ScanTriggerPayload
from src.schemas.zmq_envelope import ZMQAcknowledgement, ZMQEnvelope
from src.services.polling_service import PollingService
from src.utils.config import settings
from src.utils.logger import logger
from src.utils.service_discovery import get_service_address


Handler = Callable[[ScanTriggerPayload, str, str], Awaitable[None]]


# ZeroMQ + asyncio on Windows requires the selector event loop policy.
# Set it early to avoid runtime failures when the module is imported
# (not only when executed via `__main__`).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class CommandDispatcher:
    """Routes commands to appropriate handlers."""

    def __init__(self) -> None:
        self.handlers: Dict[str, Handler] = {}

    def register(self, command: str, handler: Handler) -> None:
        self.handlers[command] = handler

    def get_handler(self, command: str) -> Optional[Handler]:
        return self.handlers.get(command)


class ZMQTransport:
    """Handles ZeroMQ communication layer."""

    def __init__(self, context: zmq.asyncio.Context) -> None:
        self.context = context

        pattern = getattr(settings, "ZMQ_PATTERN", "REP")
        self.pattern = pattern

        pattern_map = {
            "REP": zmq.REP,
            # Keep for parity with reference repo; ACK is meaningful in REP mode.
            "PULL": zmq.PULL,
        }
        socket_type = pattern_map.get(self.pattern, zmq.REP)
        self.receiver = context.socket(socket_type)

        # Outbound result sink (PUSH)
        self.result_sink = context.socket(zmq.PUSH)
        sink_service_id = getattr(settings, "ZMQ_RESULT_SINK_SERVICE_ID", "result-manager")
        self.result_sink_addr = get_service_address(sink_service_id)
        if self.result_sink_addr:
            self.result_sink.connect(self.result_sink_addr)

    async def bind_receiver(self, address: str) -> None:
        self.receiver.bind(address)

    async def receive_message(self) -> Dict[str, Any]:
        return await self.receiver.recv_json()

    async def send_ack(self, ack: ZMQAcknowledgement) -> None:
        """
        Sends an immediate acknowledgement back to the requester.

        For REP mode, this is a synchronous reply.
        """

        if self.pattern == "REP":
            await self.receiver.send_json(ack.model_dump())
            logger.info(f"[{ack.trace_id}] ACK sent: status={ack.status}, command={ack.command}")
        else:
            logger.warning(
                "Attempted to send ACK in non-REP mode; ACK will be skipped.",
                extra={"trace_id": ack.trace_id},
            )

    async def send_result(self, result_envelope: Dict[str, Any]) -> None:
        if not self.result_sink_addr:
            logger.error(
                "Result sink address not found. Cannot publish result.",
                extra={"trace_id": result_envelope.get("trace_id")},
            )
            return

        try:
            await self.result_sink.send_json(result_envelope)
            trace_id = result_envelope.get("trace_id", "unknown")
            logger.info(f"[{trace_id}] Outbound scan_result PUSH sent to sink")
        except Exception as e:
            trace_id = result_envelope.get("trace_id", "unknown")
            logger.error(f"[{trace_id}] Failed to PUSH scan_result: {e}")

    def close(self) -> None:
        try:
            self.receiver.close(linger=0)
        except Exception:
            pass
        try:
            self.result_sink.close(linger=0)
        except Exception:
            pass


class ZMQWorker:
    """Orchestrates the message processing flow."""

    def __init__(self) -> None:
        self.ctx = zmq.asyncio.Context()
        self.transport = ZMQTransport(self.ctx)
        self.dispatcher = CommandDispatcher()

        self._stop_event = asyncio.Event()
        self._processing_queue: asyncio.Queue[Tuple[str, ScanTriggerPayload, str, str]] = asyncio.Queue(
            maxsize=getattr(settings, "ZMQ_QUEUE_MAXSIZE", 1000)
        )
        self._worker_tasks: list[asyncio.Task[None]] = []

        self.dispatcher.register("trigger_scan", self.handle_trigger_scan)

    async def handle_trigger_scan(self, payload: ScanTriggerPayload, trace_id: str, sender: str) -> None:
        """
        Handles `trigger_scan` command by reusing existing HTTP business logic.

        Publishes outbound `scan_result` to the configured ZMQ sink service.
        """

        try:
            scan_response: ScanResponse = await PollingService.run_api_trigger(
                payload, tenant=DefaultValues.TENANT, resource_path="/zmq"
            )

            # Outbound message should mirror inbound payload fields and carry
            # the final resolved version (latest commit hash when available).
            result_data = payload.model_dump(exclude_none=True)
            result_data["version"] = payload.version
            status_message = scan_response.message

            logger.info(f"[{trace_id}] trigger_scan completed: {status_message}")

            result_envelope = {
                "from": getattr(settings, "SERVICE_ID", "secops-polling"),
                "command": "scan_result",
                "trace_id": trace_id,
                "data": result_data,
            }
            await self.transport.send_result(result_envelope)

        except Exception as e:
            logger.error(f"[{trace_id}] trigger_scan failed: {e}")

            # Keep the outbound contract stable even on failure.
            failure_response = payload.model_dump(exclude_none=True)
            failure_response["version"] = payload.version
            failure_response["error"] = f"trigger_scan failed: {str(e)}"
            result_envelope = {
                "from": getattr(settings, "SERVICE_ID", "secops-polling"),
                "command": "scan_result",
                "trace_id": trace_id,
                "data": failure_response,
            }
            await self.transport.send_result(result_envelope)

    async def _processing_worker_loop(self, worker_index: int) -> None:
        while not self._stop_event.is_set() or not self._processing_queue.empty():
            try:
                item = await asyncio.wait_for(self._processing_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            command, payload, trace_id, sender = item
            try:
                handler = self.dispatcher.get_handler(command)
                if not handler:
                    logger.error(f"[{trace_id}] No handler registered for command={command}")
                    continue
                await handler(payload, trace_id, sender)
            except Exception as e:
                logger.error(f"[{trace_id}] Unhandled exception in processing worker: {e}")
            finally:
                self._processing_queue.task_done()

    @staticmethod
    def _redact_for_preview(obj: Any) -> Any:
        """
        Best-effort redaction for log/terminal previews.
        This is intentionally conservative: it only targets obvious secret-ish keys.
        """

        secret_keys = {
            "auth_token",
            "pat_token",
            "aws_access_key",
            "aws_secret_key",
            "token",
            "password",
            "secret",
            "api_key",
            "apikey",
        }

        def walk(value: Any) -> Any:
            if isinstance(value, dict):
                out: dict[str, Any] = {}
                for k, v in value.items():
                    if isinstance(k, str) and k.lower() in secret_keys:
                        out[k] = "***REDACTED***"
                    else:
                        out[k] = walk(v)
                return out
            if isinstance(value, list):
                return [walk(v) for v in value]
            return value

        try:
            return walk(copy.deepcopy(obj))
        except Exception:
            return obj

    @classmethod
    def _preview_text(cls, raw_msg: Any, max_lines: int = 50) -> str:
        redacted = cls._redact_for_preview(raw_msg)
        preview_lines = json.dumps(redacted, indent=2, default=str).splitlines()
        lines_to_show = preview_lines[:max_lines]
        text = "\n".join(lines_to_show)
        if len(preview_lines) > max_lines:
            text += "\n... (truncated)"
        return text

    async def start(self) -> None:
        address = f"tcp://{getattr(settings, 'ZMQ_HOST', '0.0.0.0')}:{getattr(settings, 'ZMQ_PORT', 9001)}"
        await self.transport.bind_receiver(address)
        logger.info(f"ZMQ Worker started and bound to {address}")

        worker_count = int(getattr(settings, "ZMQ_WORKERS", 5))
        for i in range(worker_count):
            self._worker_tasks.append(asyncio.create_task(self._processing_worker_loop(i)))

        while not self._stop_event.is_set():
            try:
                try:
                    raw_msg = await asyncio.wait_for(self.transport.receive_message(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                preview = self._preview_text(raw_msg, max_lines=50)
                logger.info(f"Incoming message preview:\n{preview}")
                print(f"\n[NEW MESSAGE RECEIVED]\n{preview}\n{'=' * 30}")

                # Envelope validation
                try:
                    envelope = ZMQEnvelope(**raw_msg)
                except Exception as ve:
                    trace_id = raw_msg.get("trace_id", "unknown") if isinstance(raw_msg, dict) else "unknown"
                    command = raw_msg.get("command", "unknown") if isinstance(raw_msg, dict) else "unknown"
                    ack = ZMQAcknowledgement(
                        command=command,
                        trace_id=trace_id,
                        status="rejected",
                        message=f"Envelope validation failed: {str(ve)}",
                    )
                    await self.transport.send_ack(ack)
                    continue

                trace_id = envelope.trace_id
                sender = envelope.sender

                # Unknown command -> reject (still need to ACK in REP mode)
                if not self.dispatcher.get_handler(envelope.command):
                    ack = ZMQAcknowledgement(
                        command=envelope.command,
                        trace_id=trace_id,
                        status="rejected",
                        message="Unknown command",
                    )
                    await self.transport.send_ack(ack)
                    continue

                # Payload validation BEFORE ACK accepted
                try:
                    payload = ScanTriggerPayload(**envelope.data)
                except Exception as pe:
                    ack = ZMQAcknowledgement(
                        command=envelope.command,
                        trace_id=trace_id,
                        status="rejected",
                        message=f"Payload validation failed: {str(pe)}",
                    )
                    await self.transport.send_ack(ack)
                    continue

                # Enqueue boundedly; if queue is full, reject
                try:
                    self._processing_queue.put_nowait((envelope.command, payload, trace_id, sender))
                except asyncio.QueueFull:
                    ack = ZMQAcknowledgement(
                        command=envelope.command,
                        trace_id=trace_id,
                        status="rejected",
                        message="Worker queue full; try again later",
                    )
                    await self.transport.send_ack(ack)
                    continue

                # Immediate ACK accepted after validation + enqueue
                ack = ZMQAcknowledgement(
                    command=envelope.command,
                    trace_id=trace_id,
                    status="accepted",
                    message="Message validated and queued for processing",
                )
                await self.transport.send_ack(ack)

            except asyncio.CancelledError:
                # Allows clean shutdown when the start() task is cancelled.
                break
            except zmq.ZMQError as e:
                if e.errno == zmq.ETERM:
                    break
                logger.error(f"ZMQ error in receive loop: {e}")
            except Exception as e:
                logger.error(f"Unexpected error in receive loop: {e}")

    async def shutdown(self) -> None:
        logger.info("Shutting down ZMQ worker...")
        self._stop_event.set()

        for t in self._worker_tasks:
            t.cancel()

        # Best-effort drain/cancel; we don't want shutdown to hang forever.
        try:
            await asyncio.wait_for(asyncio.gather(*self._worker_tasks, return_exceptions=True), timeout=5.0)
        except Exception:
            pass

        self.transport.close()
        try:
            self.ctx.term()
        except Exception:
            pass


async def main() -> None:
    worker = ZMQWorker()
    try:
        await worker.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("ZMQ worker interrupted; shutting down.")
    except Exception as e:
        logger.error(f"ZMQ worker crashed: {e}", exc_info=True)
        raise
    finally:
        await worker.shutdown()


if __name__ == "__main__":
    # Fix for ZeroMQ on Windows with asyncio
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())

