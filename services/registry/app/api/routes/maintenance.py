"""Maintenance OS API (ADR-0010 D19): operator-only reads and owner decisions,
plus one sanitized public summary.

Authority is the ONE operator dependency (``society/operator_auth.require_operator``:
durable ``users.society_role``, user JWTs only). No secret query params, no
universal token, no client-side checks. Owner decisions resume the persisted
case; nothing here calls a model or holds a release credential.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ...database import get_db
from ...maintenance import owner as owner_mod
from ...maintenance.browser_ingest import BrowserReport, ingest_browser
from ...maintenance import slo as slo_mod
from ...maintenance import status as status_mod
from ...maintenance.config import get_maintenance_settings
from ...maintenance.kpi import kpis
from ...maintenance.orm import MaintenanceIncident, MaintenanceRelease, MaintenanceReleaseFreeze, RepairCase
from ...models import User
from ...society.operator_auth import require_event_producer, require_operator

router = APIRouter(prefix="/maintenance")


class OwnerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=300)


class FreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=300)


@router.get("/summary")
def public_summary(db: Session = Depends(get_db)):
    """Public, aggregate, structural."""
    return status_mod.public_summary(db, get_maintenance_settings())


@router.post("/observations/browser")
def ingest_browser_report(report: BrowserReport, db: Session = Depends(get_db), producer: User = Depends(require_event_producer)):
    """Deep-tier browser results from the scheduled probe job. Structural only
    (strict schema: rule ids, counts, selector classes, safe paths, numbers);
    an event-producer (or operator) user JWT is required."""
    s = get_maintenance_settings()
    if not s.monitoring_enabled:
        raise HTTPException(status_code=409, detail="maintenance monitoring is disabled")
    out = ingest_browser(db, s, report)
    db.commit()
    return {"pages": len(report.pages), "incidents_opened": len(out["opened"])}


@router.get("/status")
def status(db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    return status_mod.operator_status(db, get_maintenance_settings())


@router.get("/incidents")
def incidents(db: Session = Depends(get_db), operator: User = Depends(require_operator), status: Optional[str] = Query(None, pattern="^(open|recovered|closed)$"), limit: int = Query(100, ge=1, le=500)):
    q = db.query(MaintenanceIncident)
    if status:
        q = q.filter(MaintenanceIncident.status == status)
    return [status_mod.incident_row(i) for i in q.order_by(MaintenanceIncident.opened_at.desc()).limit(limit).all()]


@router.get("/cases")
def cases(db: Session = Depends(get_db), operator: User = Depends(require_operator), state: Optional[str] = Query(None, max_length=32), limit: int = Query(100, ge=1, le=500)):
    q = db.query(RepairCase)
    if state:
        q = q.filter(RepairCase.state == state)
    return [status_mod.case_row(c) for c in q.order_by(RepairCase.started_at.desc()).limit(limit).all()]


def _case(db: Session, case_id: uuid.UUID) -> RepairCase:
    c = db.query(RepairCase).filter(RepairCase.id == case_id).with_for_update().first()
    if c is None:
        raise HTTPException(status_code=404, detail="case not found")
    return c


@router.get("/cases/{case_id}")
def case_detail(case_id: uuid.UUID, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    return status_mod.case_detail(db, _case(db, case_id))


@router.post("/cases/{case_id}/resume")
def resume_case(case_id: uuid.UUID, body: Optional[OwnerDecision] = None, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    c = _case(db, case_id)
    try:
        owner_mod.resume(db, c, owner=operator.email, note=(body.note if body else ""))
    except owner_mod.OwnerActionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    db.commit()
    return status_mod.case_row(c)


@router.post("/cases/{case_id}/refuse")
def refuse_case(case_id: uuid.UUID, body: Optional[OwnerDecision] = None, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    c = _case(db, case_id)
    try:
        owner_mod.refuse(db, c, owner=operator.email, note=(body.note if body else ""))
    except owner_mod.OwnerActionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    db.commit()
    return status_mod.case_row(c)


@router.get("/releases")
def releases(db: Session = Depends(get_db), operator: User = Depends(require_operator), limit: int = Query(50, ge=1, le=200)):
    return [status_mod.release_row(r) for r in db.query(MaintenanceRelease).order_by(MaintenanceRelease.created_at.desc()).limit(limit).all()]


@router.get("/error-budget")
def error_budget(db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    s = get_maintenance_settings()
    return {"target": s.target, "budgets": [b.as_dict() for b in slo_mod.budgets(db, target=s.target)], "exhausted": slo_mod.error_budget_exhausted(db, target=s.target)}


@router.get("/kpis")
def maintenance_kpis(db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    return kpis(db)


@router.post("/release-freezes")
def freeze_releases(body: FreezeRequest, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    fr = owner_mod.open_release_freeze(db, owner=operator.email, reason=body.reason)
    db.commit()
    return {"id": str(fr.id), "reason_code": fr.reason_code}


@router.post("/release-freezes/{freeze_id}/lift")
def lift_freeze(freeze_id: uuid.UUID, body: FreezeRequest, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    fr = db.query(MaintenanceReleaseFreeze).filter(MaintenanceReleaseFreeze.id == freeze_id).with_for_update().first()
    if fr is None:
        raise HTTPException(status_code=404, detail="freeze not found")
    try:
        owner_mod.lift_release_freeze(db, fr, owner=operator.email, reason=body.reason)
    except owner_mod.OwnerActionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    db.commit()
    return {"id": str(fr.id), "lifted_at": fr.lifted_at.isoformat()}


_CONSOLE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Maintenance status</title><style>
body{font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;background:#0f1419;color:#e6e8eb}
h1{font-size:18px}h2{font-size:15px;margin-top:22px;border-bottom:1px solid #2a3340}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #243040;padding:4px 6px;text-align:left;vertical-align:top}
.bad{color:#ff8a80}.ok{color:#9be59b}input{width:100%;max-width:560px;padding:6px;background:#17202a;color:#e6e8eb;border:1px solid #33414f}
button{padding:6px 10px}code{color:#c8d1db}</style></head><body>
<h1>AgentNet Maintenance OS &mdash; operator status</h1>
<p>Paste an operator access token (kept in this tab only), then load. Kill switch: <code>MAINTENANCE_AUTONOMY_ENABLED=false</code>.</p>
<label for="t">Operator token</label><br><input id="t" type="password" autocomplete="off"> <button id="go">Load</button>
<div id="out"></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function table(rows,cols){if(!rows||!rows.length)return '<p>none</p>';return '<table><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+esc(typeof r[c]==='object'?JSON.stringify(r[c]):r[c])+'</td>').join('')+'</tr>').join('')+'</table>';}
async function load(){const tok=document.getElementById('t').value;try{sessionStorage.setItem('mt',tok)}catch(e){}
const r=await fetch('status',{headers:{Authorization:'Bearer '+tok}});const out=document.getElementById('out');
if(!r.ok){out.innerHTML='<p class="bad">HTTP '+r.status+'</p>';return}const s=await r.json();
out.innerHTML='<h2>Switches</h2>'+table([s.flags],Object.keys(s.flags))+
'<h2>Nothing stranded</h2><p class="'+(s.nothing_stranded.stranded_cases?'bad':'ok')+'">stranded cases: '+s.nothing_stranded.stranded_cases+'</p>'+
'<h2>Error budget</h2>'+table(s.error_budget,['sli','target','total','bad','remaining_fraction','exhausted','judged'])+
'<h2>Awaiting the owner</h2>'+table(s.awaiting_owner,['id','priority','risk_class','terminal_reason','promotion_id'])+
'<h2>Active repair cases</h2>'+table(s.active_cases,['id','state','priority','risk_class','next_action_at','deadline_at','attempts','plan_revision','model_cost_usd'])+
'<h2>Open incidents</h2>'+table(s.open_incidents,['class','priority','desired_state_ref','observations','first_observed_at','cases'])+
'<h2>Releases in flight</h2>'+table(s.releases_in_flight,['status','head_sha','pr_url','services','healthy_streak','failure_reason'])+
'<h2>Recent releases</h2>'+table(s.recent_releases,['status','head_sha','pr_url','rollback','completed_at'])+
'<h2>Release freezes</h2>'+table(s.release_freezes,['reason_code','owner_only','opened_at'])+
'<h2>Heartbeats</h2>'+table(s.heartbeats,['component','age_seconds','cycles','errors','last_error_class'])+
'<h2>KPIs (30 days)</h2>'+table([s.kpis],['cases','auto_repair_rate','rollback_rate','escalation_rate','false_positive_rate','mttd_seconds','mttr_seconds','model_cost_usd'])+
'<h2>Toil</h2>'+table([s.kpis.toil],Object.keys(s.kpis.toil))+
'<h2>Recent outcomes</h2>'+table(s.recent_outcomes,['state','priority','terminal_reason','terminal_at']);}
document.getElementById('go').onclick=load;try{const v=sessionStorage.getItem('mt');if(v){document.getElementById('t').value=v}}catch(e){}
</script></body></html>"""


@router.get("/console", response_class=HTMLResponse)
def console():
    """A static page; every datum it shows comes from the operator-only /status."""
    return HTMLResponse(_CONSOLE, headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'"})
