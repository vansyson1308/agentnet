"""The company cycle's Observe step must reach the model as ONE object.

The context builder shows an event payload only while its canonical JSON
fits ``context.TXT_LONG``. Past that it becomes a string preview, cut
mid-structure, which drops the keys that sort last -- ``instructions``
("no high-value change is a valid outcome"), ``portfolio`` and ``trigger``
-- and hands the model a blob of escaped quotes. These tests pin the worst
case: every status set at its largest, six-digit counts, a full portfolio.
"""

from __future__ import annotations

import json
import uuid

from services.registry.app.models import AgentStatus, TaskStatus
from services.registry.app.society.company import CYCLE_INSTRUCTIONS, FUNCTION_ROLE_MAP, cycle_event_payload
from services.registry.app.society.context import TXT_LONG, _bounded_json

BIG = 999_999

A2A_TASK_STATES = (
    "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED", "TASK_STATE_REJECTED", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED",
)
# a2a_audit_log.result is free text; a generous superset of what the code writes
AUDIT_RESULTS = ("created", "escrow_reserved", "canceled", "rejected", "refused", "unauthenticated", "not_found", "error")
REMOTE_STATES = ("discovered", "verified", "degraded", "quarantined", "blocked")      # CHECK constraint
OUTBOUND_STATUSES = ("pending", "sent", "succeeded", "failed", "timeout", "refused")  # CHECK constraint


def _worst_case_evidence():
    return {
        "window": {"start": "2026-09-25T01:32:40.537293+00:00", "end": "2026-09-26T01:32:40.537293+00:00"},
        "marketplace": {
            "tasks_by_status": {s.value: BIG for s in TaskStatus},
            "agents_by_status": {s.value: BIG for s in AgentStatus},
            "new_agents": BIG,
        },
        "task_success_rate": 0.1234,
        "a2a": {
            "inbound_tasks_by_state": {s: BIG for s in A2A_TASK_STATES},
            "inbound_completion_rate": 0.1234,
            "inbound_audit_results": {r: BIG for r in AUDIT_RESULTS},
            "remote_agents_by_state": {s: BIG for s in REMOTE_STATES},
            "outbound_calls_by_status": {s: BIG for s in OUTBOUND_STATUSES},
        },
        "signup_funnel": {"signups": BIG, "verified": BIG},
        "security": {"society_intents_denied": BIG, "open_incidents": BIG},
    }


def _payload():
    portfolio = {"active_hypotheses": BIG, "max_active_hypotheses": BIG, "high_risk_investigations": BIG,
                 "max_high_risk_investigations": BIG, "full": True}
    return cycle_event_payload(uuid.uuid4(), "scheduled", _worst_case_evidence(), portfolio)


def test_worst_case_cycle_payload_reaches_the_model_as_one_object():
    payload = _payload()
    size = len(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False))
    assert size <= TXT_LONG, f"company.cycle payload is {size} chars; the context shows at most {TXT_LONG}"
    seen = _bounded_json(payload, TXT_LONG)
    assert "_truncated" not in seen
    assert seen["instructions"] == CYCLE_INSTRUCTIONS and seen["portfolio"]["full"] is True and seen["trigger"] == "scheduled"
    assert seen["evidence"]["a2a"]["outbound_calls_by_status"]["refused"] == BIG
    if "fitness" not in seen:  # dropped to fit, and said so
        assert seen["omitted"] == ["fitness"]


def test_a_normal_cycle_keeps_its_fitness_summary():
    evidence = _worst_case_evidence()
    evidence["marketplace"]["tasks_by_status"] = {"completed": 12, "failed": 3}
    evidence["a2a"]["inbound_audit_results"] = {"created": 40, "rejected": 12}
    payload = cycle_event_payload(uuid.uuid4(), "operator", evidence, {"active_hypotheses": 1, "full": False})
    assert "fitness" in payload and "omitted" not in payload


def test_the_static_role_map_is_not_repeated_in_every_cycle():
    payload = _payload()
    assert "function_roles" not in payload
    assert all(role not in json.dumps(payload) for role in ("architect+builder",))
    assert FUNCTION_ROLE_MAP["research"] == "scout"  # still defined, and still in the operator status
