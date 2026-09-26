"""Mount the A2A gateway on the registry app (ADR-0009 D3, D6, D17).

* ``POST /a2a``          — JSON-RPC binding (SDK ``create_jsonrpc_routes``).
* ``/a2a/http/...``      — HTTP+JSON binding (SDK ``create_rest_routes``),
  mounted at the root of its own sub-application so the ``/{tenant}/...``
  twins match what the official clients send.
* Agent Cards: ``/.well-known/agent-card.json`` and
  ``/v1/agents/{id}/a2a-card`` (alias ``/v1/agents/{id}/agent-card.json``).

``A2AGateway`` wraps both bindings. Before the SDK sees a request it:
answers 404 unless the gateway is enabled and configured; bounds and caches
the body; checks the content type; authenticates the bearer credential with
the registry's own ``verify_token`` (401 + ``WWW-Authenticate`` otherwise).
On the way out it labels HTTP+JSON bodies ``application/a2a+json``, echoes
activated extensions and marks responses ``no-store``. Rate limiting is the
registry's existing middleware, untouched (no A2A-specific identity rule).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from a2a.extensions.common import HTTP_EXTENSION_HEADER, get_requested_extensions
from a2a.server.context import ServerCallContext
from a2a.server.routes import create_jsonrpc_routes, create_rest_routes
from a2a.server.routes.common import ServerCallContextBuilder
from fastapi import APIRouter, Request as FastAPIRequest
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import cards, config, reconciler
from .auth import A2AUser, AuthenticationFailed, resolve_principal
from .fanout import Fanout, StreamLimiter
from .handler import STATE_BASE_URL, STATE_BINDING, STATE_HTTP_REQUEST_ID, STATE_PRINCIPAL, AgentNetRequestHandler

logger = logging.getLogger(__name__)

SCOPE_PRINCIPAL = "agentnet.a2a.principal"
SCOPE_BINDING = "agentnet.a2a.binding"
JSON_TYPES = ("application/json", "application/a2a+json")
_DROP_HEADERS = ("authorization", "cookie", "proxy-authorization")


class A2ARuntime:
    """Process-wide A2A state: DB sessions, wake-ups, stream bounds, sweep."""

    def __init__(self) -> None:
        self.fanout = Fanout()
        self.limiter = StreamLimiter()
        self.session_factory: Optional[Callable[[], Any]] = None
        self._sweeper: Optional[asyncio.Task] = None

    def new_session(self) -> Any:
        if self.session_factory is None:
            from ..database import SessionLocal

            return SessionLocal()
        return self.session_factory()

    async def start(self) -> None:
        if config.v03_compat_requested():
            logger.error("A2A_V03_COMPAT_ENABLED=true is ignored: A2A 0.3 compatibility is not implemented (ADR-0009 D3)")
        if not config.server_enabled():
            logger.info("A2A gateway disabled (A2A_SERVER_ENABLED=false)")
            return
        if config.public_base_url() is None:
            logger.error("A2A gateway NOT started: A2A_PUBLIC_BASE_URL is missing or invalid")
            return
        from ..config import REDIS_URL

        await self.fanout.start(REDIS_URL)
        self._sweeper = asyncio.create_task(self._sweep_forever())
        logger.info("A2A gateway enabled (JSON-RPC %s, HTTP+JSON %s)", config.JSONRPC_PATH, config.HTTP_JSON_PATH)

    async def _sweep_forever(self) -> None:
        while True:
            try:
                changed = await run_in_threadpool(reconciler.sweep, self.new_session)
                for task_id in changed:
                    await self.fanout.publish(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep sweeping
                logger.exception("a2a reconcile sweep failed")
            await asyncio.sleep(config.reconcile_interval_seconds())

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        await self.fanout.stop()


runtime = A2ARuntime()


class AgentNetContextBuilder(ServerCallContextBuilder):
    """Builds the SDK call context from the gateway's verified principal.

    Credentials never enter the call state. ``?A2A-Version=`` is honoured when
    the header is absent (the SDK ignores the query form)."""

    def build(self, request: Request) -> ServerCallContext:
        headers = {k.lower(): v for k, v in request.headers.items() if k.lower() not in _DROP_HEADERS}
        if not headers.get("a2a-version"):
            query_version = request.query_params.get("A2A-Version") or request.query_params.get("a2a-version")
            if query_version:
                headers["a2a-version"] = query_version[:16]
        principal = request.scope.get(SCOPE_PRINCIPAL)
        return ServerCallContext(
            user=A2AUser(principal),
            state={
                "headers": headers,
                STATE_PRINCIPAL: principal,
                STATE_BINDING: request.scope.get(SCOPE_BINDING, config.BINDING_JSONRPC),
                STATE_HTTP_REQUEST_ID: (headers.get("x-request-id") or "")[:64] or None,
                STATE_BASE_URL: config.public_base_url() or "",
            },
            requested_extensions=get_requested_extensions(request.headers.getlist(HTTP_EXTENSION_HEADER)),
        )


def _error(status: int, grpc_status: str, message: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse({"error": {"code": status, "status": grpc_status, "message": message}}, status_code=status, headers=headers)


def _authenticate(authorization: Optional[str]):
    db = runtime.new_session()
    try:
        return resolve_principal(db, authorization)
    finally:
        db.close()


class A2AGateway:
    """ASGI wrapper in front of one SDK binding (see module docstring)."""

    def __init__(self, app: Any, binding: str) -> None:
        self.app = app
        self.binding = binding

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not config.gateway_ready():
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return

        request = Request(scope, receive)
        limit = config.max_request_bytes()
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await _error(413, "INVALID_ARGUMENT", f"request body exceeds {limit} bytes")(scope, receive, send)
            return
        body = b""
        if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    await _error(413, "INVALID_ARGUMENT", f"request body exceeds {limit} bytes")(scope, receive, send)
                    return
                chunks.append(chunk)
            body = b"".join(chunks)
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if body and content_type not in JSON_TYPES:
                await _error(415, "INVALID_ARGUMENT", "use Content-Type application/json or application/a2a+json")(scope, receive, send)
                return

        try:
            principal = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
        except AuthenticationFailed:
            await _error(
                401, "UNAUTHENTICATED", "a valid AgentNet bearer credential is required",
                headers={"WWW-Authenticate": 'Bearer realm="agentnet-a2a"'},
            )(scope, receive, send)
            return

        scope[SCOPE_PRINCIPAL] = principal
        scope[SCOPE_BINDING] = self.binding
        requested = get_requested_extensions(request.headers.getlist(HTTP_EXTENSION_HEADER))
        activated = sorted(requested & config.SUPPORTED_EXTENSIONS)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if self.binding == config.BINDING_HTTP_JSON and headers.get("content-type", "").startswith("application/json"):
                    headers["content-type"] = config.A2A_MEDIA_TYPE
                if activated:
                    headers[HTTP_EXTENSION_HEADER] = ", ".join(activated)
                headers["cache-control"] = "no-store"
            await send(message)

        await self.app(scope, replay_receive, send_wrapper)


# ── Agent Cards ─────────────────────────────────────────────────────────────

card_router = APIRouter()


def _card_response(request: FastAPIRequest, payload: dict) -> Response:
    etag = cards.card_etag(payload)
    headers = {"Cache-Control": "public, max-age=300", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)


@card_router.get("/.well-known/agent-card.json", include_in_schema=False)
async def network_agent_card(request: FastAPIRequest) -> Response:
    base = config.public_base_url()
    if not config.server_enabled() or base is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return _card_response(request, cards.card_json(cards.network_card(base)))


def conformance(base: str) -> dict:
    """Machine-readable truth about this gateway (what runs, what does not).
    Built from code and configuration, never hand-maintained claims."""
    ops = {
        "SendMessage": "supported",
        "SendStreamingMessage": "supported (SSE)",
        "GetTask": "supported",
        "ListTasks": "supported (party-scoped, cursor paging, pageSize 1-100)",
        "CancelTask": "supported before the agent starts; otherwise -32002",
        "SubscribeToTask": "supported for non-terminal tasks; terminal -> -32004",
        "CreateTaskPushNotificationConfig": "unsupported (-32003)",
        "GetTaskPushNotificationConfig": "unsupported (-32003)",
        "ListTaskPushNotificationConfigs": "unsupported (-32003)",
        "DeleteTaskPushNotificationConfig": "unsupported (-32003)",
        "GetExtendedAgentCard": "unsupported (-32007)",
    }
    return {
        "protocolVersion": config.PROTOCOL_VERSION,
        "specRelease": "1.0.1",
        "sdk": {"python": "a2a-sdk==1.1.5"},
        "bindings": [
            {"protocolBinding": config.BINDING_JSONRPC, "url": f"{base}{config.JSONRPC_PATH}"},
            {"protocolBinding": config.BINDING_HTTP_JSON, "url": f"{base}{config.HTTP_JSON_PATH}", "mediaType": config.A2A_MEDIA_TYPE},
        ],
        "notOffered": ["gRPC", "A2A 0.3 compatibility", "push notifications", "extended agent card", "INPUT_REQUIRED/AUTH_REQUIRED task states"],
        "versionNegotiation": "A2A-Version header (or ?A2A-Version=); missing = 0.3 -> -32009",
        "operations": ops,
        "multiTenancy": "tenant = marketplace agent id (routing only; authorization is per principal)",
        "security": {"scheme": "HTTP Bearer", "credentials": ["agent JWT", "agent-scoped spt_ token (execute to create tasks)", "user JWT (read tasks of owned agents)"]},
        "extensions": [
            {"uri": config.ECONOMICS_EXTENSION_URI, "required": False, "purpose": "escrow-backed payment for paid skills"},
            {"uri": config.FEDERATION_EXTENSION_URI, "required": False, "purpose": "delegation depth", "maxDepth": config.max_federation_depth()},
        ],
        "limits": {
            "maxRequestBytes": config.max_request_bytes(),
            "blockingWaitSeconds": config.blocking_wait_seconds(),
            "maxStreamsPerPrincipal": config.max_streams_per_principal(),
            "maxSubscribersPerTask": config.max_subscribers_per_task(),
            "streamMaxSeconds": config.stream_max_seconds(),
        },
        "evidence": {
            "tests": [
                "tests/society/test_a2a_server.py",
                "tests/society/test_a2a_sdk_interop.py (official a2a-sdk client, both bindings, streaming)",
                "tests/society/test_a2a_federation.py (AgentNet client -> official reference agent)",
                "scripts/a2a/js_interop.mjs (official @a2a-js/sdk 1.2.1 client)",
            ],
            "liveProof": "docs/A2A_LIVE_PROOF.md",
        },
    }


@card_router.get("/v1/a2a/conformance", include_in_schema=False)
async def a2a_conformance() -> Response:
    base = config.public_base_url()
    if not config.server_enabled() or base is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return JSONResponse(conformance(base), headers={"Cache-Control": "public, max-age=300"})


def _federation_summary() -> dict:
    from sqlalchemy import func

    from .orm import A2ARemoteAgent

    db = runtime.new_session()
    try:
        rows = db.query(A2ARemoteAgent.state, func.count(A2ARemoteAgent.id)).group_by(A2ARemoteAgent.state).all()
        return {"remoteAgentsByState": {state: int(n) for state, n in rows}, "total": int(sum(n for _, n in rows))}
    finally:
        db.close()


@card_router.get("/v1/a2a/federation/summary", include_in_schema=False)
async def federation_summary() -> Response:
    """Public and STRUCTURAL only: counts by trust state. Names, URLs, skills
    and credentials stay behind the operator API."""
    if not config.federation_enabled():
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return JSONResponse(await run_in_threadpool(_federation_summary), headers={"Cache-Control": "public, max-age=60"})


def _agent_card_payload(agent_id: str) -> Optional[dict]:
    import uuid as _uuid

    from ..models import Agent

    try:
        aid = _uuid.UUID(agent_id)
    except ValueError:
        return None
    db = runtime.new_session()
    try:
        agent = db.query(Agent).filter(Agent.id == aid).first()
        if not cards.card_available(agent):
            return None
        return cards.card_json(cards.agent_card(agent, config.public_base_url() or ""))
    finally:
        db.close()


@card_router.get("/v1/agents/{agent_id}/a2a-card", include_in_schema=False)
@card_router.get("/v1/agents/{agent_id}/agent-card.json", include_in_schema=False)
async def agent_a2a_card(agent_id: str, request: FastAPIRequest) -> Response:
    if not config.server_enabled() or config.public_base_url() is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    payload = await run_in_threadpool(_agent_card_payload, agent_id)
    if payload is None:
        return JSONResponse({"detail": "Agent not found"}, status_code=404)
    return _card_response(request, payload)


def install(app: Any) -> None:
    """Register the cards and both bindings on the registry app."""
    handler = AgentNetRequestHandler(session_factory=runtime.new_session, fanout=runtime.fanout, limiter=runtime.limiter)
    builder = AgentNetContextBuilder()
    jsonrpc_app = Starlette(routes=create_jsonrpc_routes(handler, rpc_url=config.JSONRPC_PATH, context_builder=builder))
    rest_app = Starlette(routes=create_rest_routes(handler, context_builder=builder))
    from .federation.api import router as federation_router

    app.include_router(card_router)
    app.include_router(federation_router)
    app.router.routes.append(Route(config.JSONRPC_PATH, endpoint=A2AGateway(jsonrpc_app, config.BINDING_JSONRPC), methods=["POST"]))
    app.router.routes.append(Mount(config.HTTP_JSON_PATH, app=A2AGateway(rest_app, config.BINDING_HTTP_JSON)))
