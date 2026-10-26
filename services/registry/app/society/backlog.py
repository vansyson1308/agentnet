"""The self-improvement loop's work queue: structural Scout inputs (no model call).

In this order:

* bench: ACTIVE dev tasks (tasks.json) the running revision's harness fails on
  >= 1 of its runs, from its latest ``society_bench_reports`` row (else the latest
  row). A task the harness delivers on every run is never an item (that is the
  regression set's job). Holdout tasks never enter the backlog or any context.
  The item says the gap is the BUILDER HARNESS's (the task itself is solved).
* incidents: open maintenance incidents with no live repair case (the kernel
  has nothing running for them).
* issues: open issues the repository owner labelled ``agent-ok``
  (``promotion_github.list_agent_ok_issues``; the credential stays there).

Each item is published ONCE as a ``backlog.item`` event (task id / issue number,
failure class, counts). An empty backlog with no open candidate = idle heartbeat.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session

from ..maintenance.orm import MaintenanceIncident, RepairCase
from ..maintenance.state_machine import NON_TERMINAL, CaseState
from ..models import CodeCandidate, CodeCandidateStatus
from .config import SocietySettings
from .events import EventType, emit_event

FAILS_OF_3 = 1
#: Where a bench gap is fixed: the builder harness the bench measures (never the task's files).
HARNESS_PATHS = ("services/registry/app/maintenance/harness.py", "services/registry/app/maintenance/activities.py",
                 "services/registry/app/maintenance/repo_tools.py", "services/registry/app/society/engineering/build_engine.py")
_OPEN = (CodeCandidateStatus.READY, CodeCandidateStatus.REJECTED, CodeCandidateStatus.FAILED, CodeCandidateStatus.ABANDONED)


def running_revision(settings: SocietySettings) -> str:
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA", "").strip()
    if sha:
        return sha
    try:
        return subprocess.run(["git", "-C", settings.repo_root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def latest_report(db: Session, revision: str = "") -> Optional[Dict[str, Any]]:
    """The latest bench report of ``revision``; else the latest one (``stale`` = True)."""
    cols = "id, revision, judge_revision, path, model, repeat, summary, per_task, created_at"
    row = db.execute(text(f"SELECT {cols} FROM society_bench_reports WHERE revision = :r ORDER BY created_at DESC LIMIT 1"), {"r": revision}).mappings().first() if revision else None
    stale = row is None
    row = row or db.execute(text(f"SELECT {cols} FROM society_bench_reports ORDER BY created_at DESC LIMIT 1")).mappings().first()
    return {**dict(row), "stale": stale} if row else None


def trend(db: Session, n: int = 7, *, with_cost: bool = False) -> List[Dict[str, Any]]:
    """The last ``n`` bench reports, latest first: overall and dev scores (never holdout);
    cost only for the operator view -- public society routes stay structural."""
    rows = db.execute(text("SELECT revision, judge_revision, path, repeat, summary, created_at FROM society_bench_reports ORDER BY created_at DESC LIMIT :n"), {"n": n}).mappings()
    out = []
    for r in rows:
        s = r["summary"] or {}
        out.append({"at": r["created_at"].isoformat() if r["created_at"] else None, "revision": str(r["revision"])[:12], "candidate": r["revision"] != r["judge_revision"],
                    "path": r["path"], "repeat": r["repeat"], "samples": (s.get("config") or {}).get("builder_samples"), "pass_at_1": s.get("pass_at_1"),
                    "pass_at_k": s.get("pass_at_k"), "dev_pass_at_1": ((s.get("splits") or {}).get("dev") or {}).get("pass_at_1"),
                    "delivered": s.get("delivered"), "runs": s.get("runs"), **({"cost_usd": s.get("cost_usd")} if with_cost else {})})
    return out


def bench_items(report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items = []
    for task_id, v in sorted(((report or {}).get("per_task") or {}).items()):
        runs = list(v.get("runs") or [])
        fails = len(runs) - int(v.get("delivered") or 0)
        if v.get("split") != "dev" or not runs or fails * 3 < FAILS_OF_3 * len(runs):
            continue
        cls = Counter(r for r in runs if r != "pass").most_common(1)[0][0]
        items.append({"source": "bench", "key": f"bench:{task_id}:{report['id']}", "task_id": task_id, "failure_class": cls,
                      "delivered": len(runs) - fails, "runs": len(runs), "revision": str(report["revision"])[:12],
                      "gap": "builder_harness", "harness_paths": list(HARNESS_PATHS)})
    return items


def incident_items(db: Session) -> List[Dict[str, Any]]:
    """Open maintenance incidents no live repair case covers (structural fields only). A
    resumable escalation is live: it waits on the owner, not on the Society."""
    live = db.query(RepairCase.incident_id).filter(or_(RepairCase.state.in_([s.value for s in NON_TERMINAL]),
                                                       and_(RepairCase.state == CaseState.SAFELY_ESCALATED.value, RepairCase.resumable.is_(True))))
    rows = (db.query(MaintenanceIncident).filter(MaintenanceIncident.status == "open", MaintenanceIncident.id.notin_(live))
            .order_by(MaintenanceIncident.priority, MaintenanceIncident.opened_at).limit(10).all())
    return [{"source": "maintenance_incident", "key": f"incident:{i.id}:{i.case_count}", "incident_id": str(i.id), "failure_class": i.incident_class,
             "priority": i.priority, "target": i.target, "cases_so_far": int(i.case_count or 0), "sli_ref": i.desired_state_ref} for i in rows]


def issue_items(provider: Any) -> List[Dict[str, Any]]:
    fetch = getattr(provider, "list_agent_ok_issues", None)
    if fetch is None:
        return []
    try:
        issues = fetch() or []
    except Exception:  # noqa: BLE001 -- an unreachable GitHub is no backlog, never a crash
        return []
    return [{"source": "github_issue", "key": f"issue:{i['number']}:{i['updated_at']}", "issue_number": int(i["number"]), "failure_class": "agent_ok_issue",
             "title": {"_untrusted": True, "text": str(i.get("title") or "")[:120]},
             "objectives": [lb for lb in i.get("labels") or [] if re.fullmatch(r"O[1-5]", str(lb))]} for i in issues[:10]]


#: Key results the company grows through product work (kr_gap_items). The others have their own
#: supply: core journey -> maintenance incidents, bench pass@1 -> bench items, spend -> none (a meta KR).
GROWTH_METRICS = ("escrow_tasks_completed_7d", "a2a_agents_live_card")
#: The north star's product key results (goals.py), supplied only while it is on. External
#: income (external_jobs_paid_30d) has no product lever: it is the owner's outreach.
NORTH_STAR_GROWTH_METRICS = ("revenue_usd_30d", "paying_callers_30d", "outside_agents_joined_7d", "verified_signups_7d")


def growth_metrics() -> tuple:
    from . import goals  # noqa: PLC0415

    return GROWTH_METRICS + (NORTH_STAR_GROWTH_METRICS if goals.enabled() else ())


def kr_gap_items(db: Session, now: Any = None) -> List[Dict[str, Any]]:
    """One item per growth key result of an ACTIVE objective that is off target (or has no
    reading yet), keyed per ISO week: the company cycle turns it into ONE proposed ticket
    whose proof is the metric's deterministic probe (tickets.supply). No model, no event."""
    from . import charter, metrics, work_packet  # noqa: PLC0415
    from .events import utcnow  # noqa: PLC0415

    now = now or utcnow()
    week = "%d-W%02d" % now.isocalendar()[:2]
    items, growth = [], growth_metrics()
    for obj in charter.objectives(db):
        if obj["status"] != "active":
            continue
        for kr in obj["key_results"]:
            m = kr["metric_id"]
            if m not in growth:
                continue
            current = metrics.read(db, m, now)
            target = float(kr["target"])
            off = current is None or (current < target if kr["direction"] == "up" else current > target)
            if not off:
                continue
            base = {"source": "kr_gap", "metric_id": m, "objective_id": obj["id"], "current": current, "target": target,
                    "failure_class": "key_result_off_target"}
            plays = work_packet.REVENUE_PLAYS.get(m) if m in NORTH_STAR_GROWTH_METRICS else None
            if plays:  # one ticket per in-repo play that can move revenue (work_packet.REVENUE_PLAYS)
                items += [{**base, "key": f"kr_gap:{m}:{p['id']}:{week}", "play": p["id"], "play_title": p["title"]} for p in plays]
            else:
                items.append({**base, "key": f"kr_gap:{m}:{week}"})
    return items


def collect(db: Session, settings: SocietySettings, provider: Any = None) -> List[Dict[str, Any]]:
    return bench_items(latest_report(db, running_revision(settings))) + incident_items(db) + issue_items(provider)


def open_candidates(db: Session) -> int:
    return db.query(CodeCandidate.id).filter(CodeCandidate.status.notin_(_OPEN)).count()


def publish(db: Session, settings: SocietySettings, items: List[Dict[str, Any]]) -> int:
    """One ``backlog.item`` event per new item (idempotent per key). The caller commits."""
    n = 0
    for item in items:
        before = db.execute(text("SELECT 1 FROM society_events WHERE idempotency_key = :k"), {"k": f"backlog:{item['key']}"[:160]}).first()
        if before is None:
            emit_event(db, event_type=EventType.BACKLOG_ITEM, payload=item, actor_type="system", idempotency_key=f"backlog:{item['key']}"[:160])
            n += 1
    return n

