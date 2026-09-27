"""Browser probe results -> maintenance incidents + ``browser_journey`` SLI.

Input is STRUCTURAL only (``BrowserReport``: rule ids, counts, selector
classes, safe paths, numbers), validated strictly -- whether it came from the
in-process probe or from the scheduled deep-tier job through the event-producer
ingress. One page with any finding is one bad SLI sample; each (page, rule)
is its own incident fingerprint, so unrelated defects are never bundled.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from .browser import PROBE_VERSION, rule_class
from .config import MaintenanceSettings
from .incidents import Violation, ingest_violation, observe_healthy, record_observation
from .taxonomy import IncidentClass, Priority, Severity, TrustClass

_RULE = re.compile(r"^(axe:[a-z0-9\-]{1,55}|[a-z_]{3,40})$")
_SEL = re.compile(r"^[A-Za-z0-9_.\-#>: ]{0,80}$")
_PATH = re.compile(r"^/[A-Za-z0-9._~/\-]{0,127}$")
_PAGE = re.compile(r"^[a-z0-9_]{1,40}$")
SOURCE = "browser_probe"


class FindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: str
    count: int = Field(ge=0, le=100000)
    selectors: List[str] = Field(default_factory=list, max_length=10)
    numbers: Dict[str, float] = Field(default_factory=dict)
    paths: List[str] = Field(default_factory=list, max_length=10)

    @field_validator("rule")
    @classmethod
    def _rule(cls, v):
        if not _RULE.match(v):
            raise ValueError("rule id")
        return v

    @field_validator("selectors")
    @classmethod
    def _sels(cls, v):
        if any(not _SEL.match(s) for s in v):
            raise ValueError("selector class")
        return v

    @field_validator("paths")
    @classmethod
    def _paths(cls, v):
        if any(not _PATH.match(p) for p in v):
            raise ValueError("path")
        return v

    @field_validator("numbers")
    @classmethod
    def _nums(cls, v):
        if len(v) > 8 or any(not re.match(r"^[a-z_]{1,32}$", k) for k in v):
            raise ValueError("numbers")
        return v


class PageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: str
    path: str
    status: Optional[int] = Field(default=None, ge=0, le=999)
    final_path: Optional[str] = None
    findings: List[FindingIn] = Field(default_factory=list, max_length=40)
    metrics: Dict[str, float] = Field(default_factory=dict)

    @field_validator("page")
    @classmethod
    def _page(cls, v):
        if not _PAGE.match(v):
            raise ValueError("page")
        return v

    @field_validator("path")
    @classmethod
    def _p(cls, v):
        if not _PATH.match(v):
            raise ValueError("path")
        return v

    @field_validator("final_path")
    @classmethod
    def _fp(cls, v):
        if v is not None and not _PATH.match(v):
            raise ValueError("final_path")
        return v


class BrowserReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(pattern=r"^(production|staging|preview)$")
    collector_version: str = Field(default=PROBE_VERSION, max_length=32)
    pages: List[PageIn] = Field(min_length=1, max_length=30)


def ingest_browser(db: Session, settings: MaintenanceSettings, report: BrowserReport, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    opened: List[str] = []
    for pg in report.pages:
        record_observation(db, sli="browser_journey", target=report.target, source=SOURCE, collector_version=report.collector_version, trust_class=TrustClass.TRUSTED_PROBE,
                           ok=not pg.findings, payload={"page": pg.page, "rules": sorted({f.rule for f in pg.findings})}, observed_at=now)
        failing = set()
        for f in pg.findings:
            if f.count <= 0:
                continue
            cls, prio = rule_class(f.rule)
            ref = f"{pg.page}:{f.rule}"
            failing.add(ref)
            inc, created = ingest_violation(
                db, settings,
                Violation(target=report.target, incident_class=IncidentClass(cls), desired_state_ref=ref, failure=f.rule, severity=Severity.MAJOR,
                          base_priority=Priority(prio), source=SOURCE, collector_version=report.collector_version, sli="browser_journey", path=pg.path,
                          payload={"page": pg.page, "rule": f.rule, "count": f.count, "selectors": f.selectors, "numbers": f.numbers, "paths": f.paths, "status": pg.status, "final_path": pg.final_path}),
                now=now,
            )
            if created:
                inc.provenance = {"contract": "performance_budgets" if f.rule == "performance" else "browser_experience", "failure": f.rule, "page": pg.page}
                opened.append(str(inc.id))
        # every rule this page passed: healthy for its incidents
        from .orm import MaintenanceIncident  # noqa: PLC0415

        for inc in db.query(MaintenanceIncident).filter(MaintenanceIncident.target == report.target, MaintenanceIncident.status == "open", MaintenanceIncident.desired_state_ref.like(f"{pg.page}:%")).all():
            if inc.desired_state_ref not in failing:
                observe_healthy(db, settings, target=report.target, desired_state_ref=inc.desired_state_ref, sli="browser_journey", source=SOURCE, collector_version=report.collector_version, now=now, record=False)
    db.flush()
    return {"opened": opened}


__all__ = ["BrowserReport", "PageIn", "FindingIn", "ingest_browser"]
