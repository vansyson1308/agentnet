"""The self-improvement loop's work queue: structural Scout inputs (no model call).

* bench: DEV tasks (tasks.json) the running revision's harness fails on >= 2 of 3
  runs, from its latest ``society_bench_reports`` row (else the latest row).
  Holdout tasks never enter the backlog.
* issues: open issues the repository owner labelled ``agent-ok``
  (``promotion_github.list_agent_ok_issues``; the credential stays there).

Each item is published ONCE as a ``backlog.item`` event (task id / issue number,
failure class, counts). An empty backlog with no open candidate = idle heartbeat.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import CodeCandidate, CodeCandidateStatus
from .config import SocietySettings
from .events import EventType, emit_event

FAILS_OF_3 = 2
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


def bench_items(report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items = []
    for task_id, v in sorted(((report or {}).get("per_task") or {}).items()):
        runs = list(v.get("runs") or [])
        fails = len(runs) - int(v.get("delivered") or 0)
        if v.get("split") != "dev" or not runs or fails * 3 < FAILS_OF_3 * len(runs):
            continue
        cls = Counter(r for r in runs if r != "pass").most_common(1)[0][0]
        items.append({"source": "bench", "key": f"bench:{task_id}:{report['id']}", "task_id": task_id, "failure_class": cls,
                      "delivered": len(runs) - fails, "runs": len(runs), "revision": str(report["revision"])[:12]})
    return items


def issue_items(provider: Any) -> List[Dict[str, Any]]:
    fetch = getattr(provider, "list_agent_ok_issues", None)
    if fetch is None:
        return []
    try:
        issues = fetch() or []
    except Exception:  # noqa: BLE001 -- an unreachable GitHub is no backlog, never a crash
        return []
    return [{"source": "github_issue", "key": f"issue:{i['number']}:{i['updated_at']}", "issue_number": int(i["number"]), "failure_class": "agent_ok_issue",
             "title": {"_untrusted": True, "text": str(i.get("title") or "")[:120]}} for i in issues[:10]]


def collect(db: Session, settings: SocietySettings, provider: Any = None) -> List[Dict[str, Any]]:
    return bench_items(latest_report(db, running_revision(settings))) + issue_items(provider)


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

