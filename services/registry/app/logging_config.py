"""
Centralized structured-logging setup.

In production we want JSON logs so log aggregators (Loki, ELK, Datadog,
Cloud Logging) can index every record by service / level / request_id /
trace_id without regex parsing. In development we want the same fields
visible but pretty-printed for human eyes.

Behavior:
- ``setup_logging(service_name)`` is the single entry point. Call it
  once near the top of ``main.py`` (or worker.py) before any logger is
  instantiated.
- ``structlog`` writes JSON in non-development; uses the ``ConsoleRenderer``
  (colored, indented) when ``ENVIRONMENT=development``.
- Every record automatically carries ``service``, ``timestamp``,
  ``level`` and any ``request_id`` / ``trace_id`` placed in
  ``structlog.contextvars`` by the middleware below.
- The standard library ``logging`` is re-routed through structlog so
  third-party libs (uvicorn, sqlalchemy) emit in the same format.

Middleware:
- ``RequestIDMiddleware`` — bind ``request_id`` (uuid4) and the
  optional inbound ``X-Request-ID`` to context. Sets the same value on
  the response header for cross-system correlation. Only available in
  services that ship Starlette (registry / payment / simulation /
  dashboard). The worker has no HTTP layer and doesn't need it; we
  guard the import so the worker container can use the same module
  without pulling Starlette into its slim image.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from typing import Optional

import structlog

# Starlette is optional — present for HTTP services, missing on the
# worker. Module-level guard so `from .logging_config import setup_logging`
# always succeeds.
try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request

    _HAS_STARLETTE = True
except ImportError:  # pragma: no cover — worker container path
    _HAS_STARLETTE = False
    BaseHTTPMiddleware = object  # type: ignore[misc,assignment]
    Request = None  # type: ignore[assignment]

_IS_DEV = os.getenv("ENVIRONMENT", "development").lower() == "development"


def setup_logging(service_name: str, level: Optional[str] = None) -> None:
    """Configure stdlib + structlog. Idempotent — safe to call twice."""
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        timestamper,
    ]

    if _IS_DEV:
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any existing handlers so the JSON formatter wins.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Bind service name globally so every record carries it.
    structlog.contextvars.bind_contextvars(service=service_name)

    # Tone down noisy libraries.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


if _HAS_STARLETTE:

    class RequestIDMiddleware(BaseHTTPMiddleware):
        """Bind request_id + trace_id to structlog contextvars per request.

        Honours an inbound ``X-Request-ID`` header so an upstream gateway
        can supply a trace ID and we'll propagate it through logs.
        """

        async def dispatch(self, request: "Request", call_next):
            request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
            trace_id = request.headers.get("X-Trace-ID")
            structlog.contextvars.bind_contextvars(
                request_id=request_id,
                **({"trace_id": trace_id} if trace_id else {}),
            )
            try:
                response = await call_next(request)
            finally:
                structlog.contextvars.unbind_contextvars(
                    "request_id", *(["trace_id"] if trace_id else [])
                )
            response.headers["X-Request-ID"] = request_id
            return response

    def install_request_id_middleware(app) -> None:
        app.add_middleware(RequestIDMiddleware)

else:

    class RequestIDMiddleware:  # type: ignore[no-redef]
        """Stub: starlette not installed in this service."""

    def install_request_id_middleware(app) -> None:  # pragma: no cover
        raise RuntimeError(
            "install_request_id_middleware requires Starlette / FastAPI; "
            "this looks like the worker image which has no HTTP layer."
        )
