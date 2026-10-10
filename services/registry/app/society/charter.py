"""The company charter (``company_charter.json``): owner-authored purpose, no model.

Trusted config on the RED society path, edited only by the owner (the meaning
gate refuses a spec that names it). Validated at load -- at most five
objectives, every key-result ``metric_id`` resolvable in ``metrics.py`` -- or
refused whole. Effective objective status: an operator override
(``society_objective_status``) first, else the file.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import metrics

CHARTER_PATH = pathlib.Path(__file__).with_name("company_charter.json")
#: The repository path a model-built candidate may never name.
CHARTER_REPO_PATH = "services/registry/app/society/company_charter.json"
STATUSES = ("proposed", "active", "paused", "done")
DEPARTMENTS = ("ceo_office", "strategy_product", "engineering", "quality", "security", "sre_release", "finance", "analytics")
MAX_OBJECTIVES = 5


class CharterError(ValueError):
    pass


def validate(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not str(doc.get("mission") or "").strip():
        raise CharterError("charter has no mission")
    objs = doc.get("objectives") or []
    if not 1 <= len(objs) <= MAX_OBJECTIVES:
        raise CharterError(f"charter needs 1..{MAX_OBJECTIVES} objectives, has {len(objs)}")
    seen = set()
    for o in objs:
        oid = o.get("id")
        if not oid or oid in seen:
            raise CharterError(f"objective id missing or duplicated: {oid!r}")
        seen.add(oid)
        if o.get("status") not in STATUSES or int(o.get("owner_priority") or 0) not in range(1, 6):
            raise CharterError(f"objective {oid}: status must be one of {STATUSES} and owner_priority 1..5")
        for kr in o.get("key_results") or [{}]:  # none at all is refused as an unresolvable metric
            if not metrics.resolvable(str(kr.get("metric_id"))):
                raise CharterError(f"objective {oid}: metric_id {kr.get('metric_id')!r} has no deterministic source")
            if kr.get("direction") not in ("up", "down") or kr.get("target") is None or not kr.get("deadline"):
                raise CharterError(f"objective {oid}: key result {kr.get('metric_id')} needs target, direction up|down and deadline")
    missing = [d for d in DEPARTMENTS if d not in (doc.get("departments") or {})]
    if missing:
        raise CharterError(f"charter misses departments {missing}")
    return doc


def load(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    with open(path or CHARTER_PATH, encoding="utf-8") as f:
        return validate(json.load(f))


def objectives(db: Session, charter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The charter's objectives with their EFFECTIVE status."""
    override = dict(db.execute(text("SELECT objective_id, status FROM society_objective_status")).all())
    return [{**o, "status": str(override.get(o["id"]) or o["status"])} for o in (charter or load())["objectives"]]


def find(db: Session, objective_id: str) -> Optional[Dict[str, Any]]:
    return next((o for o in objectives(db) if o["id"] == objective_id), None)
