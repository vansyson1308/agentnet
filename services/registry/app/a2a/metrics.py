"""Bounded A2A metrics (ADR-0009 D18).

Every label value comes from a closed set defined here. No task, context or
agent id, URL, token or content is ever a label (X1): a caller must not be
able to mint a time series. ``/metrics`` itself stays unserved in production.
"""

from __future__ import annotations

from ..health import _counter, _histogram

OPERATIONS = frozenset(
    {
        "SendMessage",
        "SendStreamingMessage",
        "GetTask",
        "ListTasks",
        "CancelTask",
        "SubscribeToTask",
        "PushConfig",
        "GetExtendedAgentCard",
    }
)
BINDINGS = frozenset({"JSONRPC", "HTTP+JSON"})
RESULTS = frozenset({"ok", "a2a_error", "internal_error", "unauthenticated", "refused"})
TASK_STATES = frozenset(
    {
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)
FETCH_RESULTS = frozenset({"ok", "not_modified", "invalid_card", "ssrf_refused", "http_error", "timeout", "too_large", "error"})
SSRF_REASONS = frozenset({"scheme", "credentials", "port", "private_address", "resolution", "redirect", "host"})
OUTBOUND_RESULTS = frozenset({"succeeded", "failed", "timeout", "refused"})

a2a_requests_total = _counter(
    "agentnet_a2a_requests_total", "A2A operations handled.", ["operation", "binding", "result"]
)
a2a_request_duration_seconds = _histogram(
    "agentnet_a2a_request_duration_seconds", "A2A operation latency (streams: time to first event).", ["operation", "binding"]
)
a2a_tasks_total = _counter("agentnet_a2a_tasks_total", "A2A task state transitions recorded.", ["state"])
a2a_streams_opened_total = _counter("agentnet_a2a_streams_opened_total", "A2A SSE streams opened.", ["binding"])
a2a_federation_fetch_total = _counter("agentnet_a2a_federation_fetch_total", "Remote Agent Card fetches.", ["result"])
a2a_ssrf_rejections_total = _counter(
    "agentnet_a2a_ssrf_rejections_total", "Outbound URLs refused by the safe fetcher.", ["reason_class"]
)
a2a_outbound_calls_total = _counter("agentnet_a2a_outbound_calls_total", "Outbound A2A calls.", ["result"])


def _one_of(value: str, allowed: frozenset, fallback: str = "other") -> str:
    return value if value in allowed else fallback


def record_request(operation: str, binding: str, result: str, seconds: float) -> None:
    op = _one_of(operation, OPERATIONS)
    b = _one_of(binding, BINDINGS)
    a2a_requests_total.labels(operation=op, binding=b, result=_one_of(result, RESULTS)).inc()
    a2a_request_duration_seconds.labels(operation=op, binding=b).observe(max(0.0, seconds))


def record_task_state(state: str) -> None:
    a2a_tasks_total.labels(state=_one_of(state, TASK_STATES)).inc()


def record_stream_opened(binding: str) -> None:
    a2a_streams_opened_total.labels(binding=_one_of(binding, BINDINGS)).inc()


def record_fetch(result: str) -> None:
    a2a_federation_fetch_total.labels(result=_one_of(result, FETCH_RESULTS, "error")).inc()


def record_ssrf_rejection(reason_class: str) -> None:
    a2a_ssrf_rejections_total.labels(reason_class=_one_of(reason_class, SSRF_REASONS, "host")).inc()


def record_outbound(result: str) -> None:
    a2a_outbound_calls_total.labels(result=_one_of(result, OUTBOUND_RESULTS, "failed")).inc()
