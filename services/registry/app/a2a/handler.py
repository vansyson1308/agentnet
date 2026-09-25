"""AgentNet's implementation of the official SDK ``RequestHandler`` (ADR-0009 D2).

The SDK dispatchers own the protocol (parsing, version check, JSON-RPC/REST
framing, SSE); this class owns AgentNet semantics by delegating to
``service.py`` in a worker thread. Every non-A2A exception is converted to a
generic ``InternalError`` here, because the JSON-RPC dispatcher would
otherwise echo ``str(exception)`` to the client.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Callable, Optional

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import (
    A2AError,
    ExtendedAgentCardNotConfiguredError,
    InternalError,
    InvalidRequestError,
    PushNotificationNotSupportedError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from google.protobuf.json_format import ParseDict
from starlette.concurrency import run_in_threadpool

from ..task_dispatch import dispatch_execute
from . import config, mapping, metrics, service
from .fanout import Fanout, StreamLimiter

logger = logging.getLogger(__name__)

STATE_PRINCIPAL = "agentnet.principal"
STATE_BINDING = "agentnet.binding"
STATE_HTTP_REQUEST_ID = "agentnet.http_request_id"
STATE_BASE_URL = "agentnet.base_url"


class AgentNetRequestHandler(RequestHandler):
    def __init__(self, session_factory: Callable[[], Any], fanout: Fanout, limiter: StreamLimiter) -> None:
        self._session_factory = session_factory
        self.fanout = fanout
        self.limiter = limiter

    # ── plumbing ───────────────────────────────────────────────────────

    def _call(self, context: ServerCallContext) -> service.CallInfo:
        principal = context.state.get(STATE_PRINCIPAL)
        if principal is None:  # the gateway authenticates first; never reached
            raise InvalidRequestError(message="authentication required")
        return service.CallInfo(
            principal=principal,
            tenant=context.tenant or "",
            binding=context.state.get(STATE_BINDING, config.BINDING_JSONRPC),
            extensions=set(context.requested_extensions or ()),
            request_id=context.state.get(STATE_HTTP_REQUEST_ID),
            base_url=context.state.get(STATE_BASE_URL, ""),
        )

    async def _db(self, fn: Callable[..., Any], *args: Any) -> Any:
        def _run() -> Any:
            db = self._session_factory()
            try:
                return fn(db, *args)
            finally:
                db.close()

        return await run_in_threadpool(_run)

    async def _guard(self, operation: str, context: ServerCallContext, coro) -> Any:
        started = time.monotonic()
        binding = context.state.get(STATE_BINDING, config.BINDING_JSONRPC)
        try:
            result = await coro
        except A2AError:
            metrics.record_request(operation, binding, "a2a_error", time.monotonic() - started)
            raise
        except Exception:  # noqa: BLE001
            logger.exception("a2a %s failed", operation)
            metrics.record_request(operation, binding, "internal_error", time.monotonic() - started)
            raise InternalError(message="internal error")
        metrics.record_request(operation, binding, "ok", time.monotonic() - started)
        return result

    async def _guard_stream(self, operation: str, context: ServerCallContext, gen: AsyncGenerator) -> AsyncGenerator:
        started = time.monotonic()
        binding = context.state.get(STATE_BINDING, config.BINDING_JSONRPC)
        first = True
        try:
            async for item in gen:
                if first:
                    metrics.record_request(operation, binding, "ok", time.monotonic() - started)
                    first = False
                yield item
        except A2AError:
            if first:
                metrics.record_request(operation, binding, "a2a_error", time.monotonic() - started)
            raise
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:  # noqa: BLE001
            logger.exception("a2a %s stream failed", operation)
            if first:
                metrics.record_request(operation, binding, "internal_error", time.monotonic() - started)
            raise InternalError(message="internal error")
        finally:
            await gen.aclose()

    # ── creation ───────────────────────────────────────────────────────

    async def _create(self, call: service.CallInfo, params: pb.SendMessageRequest) -> service.SendResult:
        result: service.SendResult = await self._db(service.send_begin, call, params)
        if result.task_id is not None and result.needs_escrow:
            plan: Optional[service.DispatchPlan] = await self._db(service.escrow_and_link, call, result.task_id)
            if plan is not None:
                channel = await dispatch_execute(
                    message=plan.message,
                    task_session_id=plan.task_session_id,
                    callee_agent_id=plan.callee_agent_id,
                    callee_endpoint=plan.callee_endpoint,
                )
                await self._db(service.record_fulfillment_channel, plan.task_session_id, channel)
            await self.fanout.publish(result.task_id)
        return result

    # ── SendMessage ────────────────────────────────────────────────────

    async def on_message_send(self, params: pb.SendMessageRequest, context: ServerCallContext) -> pb.Task | pb.Message:
        return await self._guard("SendMessage", context, self._send(params, context))

    async def _send(self, params: pb.SendMessageRequest, context: ServerCallContext) -> pb.Task | pb.Message:
        call = self._call(context)
        result = await self._create(call, params)
        if result.message is not None:
            return result.message
        task_id = result.task_id
        snap, _seq, terminal = await self._db(service.own_snapshot, task_id, result.history_length)
        if terminal or result.return_immediately or config.blocking_wait_seconds() == 0:
            return snap
        deadline = time.monotonic() + config.blocking_wait_seconds()
        with self.fanout.waiter(task_id) as event:
            while time.monotonic() < deadline:
                await self.fanout.wait(event, min(1.0, max(0.05, deadline - time.monotonic())))
                snap, _seq, terminal = await self._db(service.own_snapshot, task_id, result.history_length)
                if terminal:
                    break
        return snap

    # ── streams ────────────────────────────────────────────────────────

    async def on_message_send_stream(self, params: pb.SendMessageRequest, context: ServerCallContext) -> AsyncGenerator[Any, None]:
        async for item in self._guard_stream("SendStreamingMessage", context, self._send_stream(params, context)):
            yield item

    async def _send_stream(self, params: pb.SendMessageRequest, context: ServerCallContext) -> AsyncGenerator[Any, None]:
        call = self._call(context)
        if not self.limiter.acquire(call.principal.key, None, per_principal=config.max_streams_per_principal(), per_task=config.max_subscribers_per_task()):
            raise InvalidRequestError(message="too many concurrent streams for this credential")
        try:
            result = await self._create(call, params)
            if result.message is not None:
                metrics.record_stream_opened(call.binding)
                yield result.message
                return
            snap, seq, terminal = await self._db(service.own_snapshot, result.task_id, result.history_length)
            metrics.record_stream_opened(call.binding)
            yield snap
            if not terminal:
                async for event in self._follow(result.task_id, seq):
                    yield event
        finally:
            self.limiter.release(call.principal.key, None)

    async def on_subscribe_to_task(self, params: pb.SubscribeToTaskRequest, context: ServerCallContext) -> AsyncGenerator[Any, None]:
        async for item in self._guard_stream("SubscribeToTask", context, self._subscribe(params, context)):
            yield item

    async def _subscribe(self, params: pb.SubscribeToTaskRequest, context: ServerCallContext) -> AsyncGenerator[Any, None]:
        call = self._call(context)
        snap, seq, task_id, terminal = await self._db(service.task_by_id_for_stream, call, params.id)
        if terminal:
            raise UnsupportedOperationError(message="the task is in a terminal state; use GetTask")
        if not self.limiter.acquire(call.principal.key, task_id, per_principal=config.max_streams_per_principal(), per_task=config.max_subscribers_per_task()):
            raise InvalidRequestError(message="too many concurrent streams")
        try:
            metrics.record_stream_opened(call.binding)
            yield snap
            async for event in self._follow(task_id, seq):
                yield event
        finally:
            self.limiter.release(call.principal.key, task_id)

    async def _follow(self, task_id: uuid.UUID, after_seq: int) -> AsyncGenerator[Any, None]:
        """Replay durable events after ``after_seq`` until a terminal status
        event, the stream lifetime bound, or client disconnect."""
        deadline = time.monotonic() + config.stream_max_seconds()
        seq = after_seq
        with self.fanout.waiter(task_id) as event:
            while time.monotonic() < deadline:
                events, terminal, changed = await self._db(service.poll_events, task_id, seq)
                if changed:
                    await self.fanout.publish(task_id)
                for ev_seq, kind, payload in events:
                    seq = ev_seq
                    if kind == "status":
                        update = ParseDict(payload, pb.TaskStatusUpdateEvent(), ignore_unknown_fields=True)
                        yield update
                        if mapping.is_terminal(mapping.state_name(update.status.state)):
                            return
                    elif kind == "artifact":
                        yield ParseDict(payload, pb.TaskArtifactUpdateEvent(), ignore_unknown_fields=True)
                if terminal and not events:
                    return
                await self.fanout.wait(event)

    # ── reads, cancel ──────────────────────────────────────────────────

    async def on_get_task(self, params: pb.GetTaskRequest, context: ServerCallContext) -> pb.Task | None:
        async def _run() -> pb.Task:
            call = self._call(context)
            hl = params.history_length if params.HasField("history_length") else None
            snap, _seq, changed = await self._db(service.get_task, call, params.id, hl)
            if changed:
                await self.fanout.publish(uuid.UUID(snap.id))
            return snap

        return await self._guard("GetTask", context, _run())

    async def on_list_tasks(self, params: pb.ListTasksRequest, context: ServerCallContext) -> pb.ListTasksResponse:
        async def _run() -> pb.ListTasksResponse:
            return await self._db(service.list_tasks, self._call(context), params)

        return await self._guard("ListTasks", context, _run())

    async def on_cancel_task(self, params: pb.CancelTaskRequest, context: ServerCallContext) -> pb.Task | None:
        async def _run() -> pb.Task:
            snap = await self._db(service.cancel_task, self._call(context), params.id)
            await self.fanout.publish(uuid.UUID(snap.id))
            return snap

        return await self._guard("CancelTask", context, _run())

    # ── not offered (truthful card: pushNotifications=false, extendedAgentCard=false) ──

    async def _push_unsupported(self, context: ServerCallContext) -> Any:
        async def _run() -> Any:
            raise PushNotificationNotSupportedError()

        return await self._guard("PushConfig", context, _run())

    async def on_create_task_push_notification_config(self, params: pb.TaskPushNotificationConfig, context: ServerCallContext) -> pb.TaskPushNotificationConfig:
        return await self._push_unsupported(context)

    async def on_get_task_push_notification_config(self, params: pb.GetTaskPushNotificationConfigRequest, context: ServerCallContext) -> pb.TaskPushNotificationConfig:
        return await self._push_unsupported(context)

    async def on_list_task_push_notification_configs(self, params: pb.ListTaskPushNotificationConfigsRequest, context: ServerCallContext) -> pb.ListTaskPushNotificationConfigsResponse:
        return await self._push_unsupported(context)

    async def on_delete_task_push_notification_config(self, params: pb.DeleteTaskPushNotificationConfigRequest, context: ServerCallContext) -> None:
        return await self._push_unsupported(context)

    async def on_get_extended_agent_card(self, params: pb.GetExtendedAgentCardRequest, context: ServerCallContext) -> pb.AgentCard:
        async def _run() -> Any:
            raise ExtendedAgentCardNotConfiguredError()

        return await self._guard("GetExtendedAgentCard", context, _run())


__all__ = ["AgentNetRequestHandler", "TaskNotFoundError"]
