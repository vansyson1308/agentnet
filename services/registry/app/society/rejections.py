"""Why candidates were rejected, by ONE structured reason each (deterministic; for the owner).

Owner 2026-10-29: 23 candidates rejected -- does the spec gate (#114) work? Every rejected
candidate gets one reason code from what the runtime itself recorded: its latest
``code_candidate.rejected`` event payload (a Builder decline, red-on-base, a test_first already
green on the base, anti-busywork, exhausted builds, already satisfied), its Security verdict, the
first QA check that failed, or the owner closing its PR. Design proposals the spec gate refused
before any build are counted beside them (they never became candidates).

``classify`` is pure (stdlib only): ``deploy/railway/phase5_live.py`` keeps a verbatim copy and
``tests/society/test_rejections.py`` asserts the two agree."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

#: PR #114 (spec gate + Architect test_first) reached main
SINCE_SPEC_GATE = datetime(2026, 10, 27, 4, 47, tzinfo=timezone.utc)
REASONS = ("closed_by_owner", "declined", "build_attempts_exhausted", "already_satisfied", "red_on_base", "test_first_red", "busywork", "security",
           "no_self_judging", "outside_files_allowed", "test_first_green", "ci_orphan", "test_first_tampered", "acceptance_failed", "qa_other", "other")
#: the first failed QA check (engineering/qa.py) -> its reason
QA_CHECKS = {"no_self_judging": "no_self_judging", "allow_list": "outside_files_allowed", "test_first_red_on_base": "test_first_red",
             "test_first_green_on_head": "test_first_green", "ci_collects_new_tests": "ci_orphan", "test_first_intact": "test_first_tampered",
             "acceptance_tests": "acceptance_failed", "acceptance_tests_exist": "acceptance_failed"}


def classify(event: Optional[Dict[str, Any]], qa_failures: Optional[Iterable[str]], security_verdict: Optional[str], error: Optional[str]) -> str:
    event = event if isinstance(event, dict) else {}
    if (error or "").startswith("closed_by_owner"):
        return "closed_by_owner"
    if event.get("declined"):
        # the Builder could not meet the spec inside files_allowed: the design was out of scope
        return "outside_files_allowed" if event.get("reason_code") == "spec_outside_files_allowed" else "declined"
    if event.get("build_attempts_exhausted"):
        return "build_attempts_exhausted"
    if event.get("already_satisfied"):
        return "already_satisfied"
    if event.get("red_on_base") is not None:
        return "red_on_base"
    if event.get("test_first_green") is not None:
        return "test_first_red"  # the pinned test already passed on the base: it cannot judge the change
    if event.get("busywork"):
        return "busywork"
    if security_verdict == "fail":
        return "security"
    names = [str(f).split(":", 1)[0].strip() for f in (qa_failures or [])]
    for name in names:
        if name in QA_CHECKS:
            return QA_CHECKS[name]
    return "qa_other" if names else "other"


def breakdown(db: Session, since: datetime = SINCE_SPEC_GATE) -> Dict[str, Any]:
    """``{"since", "rejected", "by_reason": {reason: n}, "spec_gate_refusals"}`` for candidates REJECTED since ``since``."""
    rows = db.execute(text(
        "SELECT c.qa_report -> 'failures', c.security_report ->> 'verdict', c.error, "
        "       (SELECT e.payload FROM society_events e WHERE e.subject_id = c.id AND e.event_type = 'code_candidate.rejected' ORDER BY e.created_at DESC LIMIT 1) "
        "FROM code_candidates c WHERE c.status = 'rejected' AND c.updated_at >= :t"), {"t": since}).all()
    by: Dict[str, int] = {}
    for failures, verdict, error, payload in rows:
        reason = classify(payload, failures if isinstance(failures, list) else [], verdict, error)
        by[reason] = by.get(reason, 0) + 1
    gate = int(db.execute(text(
        "SELECT COUNT(*) FROM agent_intents WHERE created_at >= :t AND ((intent_type = 'PROPOSE_DESIGN' AND result ->> 'did' = 'gate_refused') "
        "OR error LIKE 'spec gate:%')"), {"t": since}).scalar() or 0)
    return {"since": since.isoformat(), "rejected": len(rows), "by_reason": dict(sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))), "spec_gate_refusals": gate}


def summary(b: Dict[str, Any]) -> str:
    parts = ", ".join(f"{k} {v}" for k, v in b["by_reason"].items()) or "none"
    return f"Rejected since {b['since'][:10]}: {b['rejected']} ({parts}); spec gate refused {b['spec_gate_refusals']} design(s) before a build"


__all__ = ["QA_CHECKS", "REASONS", "SINCE_SPEC_GATE", "breakdown", "classify", "summary"]
