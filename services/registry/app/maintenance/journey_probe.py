"""Hourly core-journey probe: O1's metric source (deterministic HTTP, no model).

register (or reuse) -> verified login -> two probe agents with a FREE
capability -> escrow task through the ordinary REST API -> callee start +
confirm -> the task reads ``completed`` and its escrow transaction reads
``completed``. Each run writes ONE ``maintenance_observations`` row
(sli=core_journey, ok, duration_s, the step reached); a failure is folded into
a maintenance incident through ``ingest_violation`` as for any trusted probe,
and a success grows the recovery streak of an open one.

Money invariant: the probe moves no money. Its capability's price is 0, so the
escrow it locks and releases is 0 credits; it touches wallets and transactions
only through the existing task APIs (``create_task_with_escrow`` and
``confirm_task_completion`` behind ``/v1/tasks``) and only READS the
transaction status. It never writes a wallet, a transaction or a trigger.

Staging only: refused unless ENVIRONMENT is staging/development and an explicit
``CORE_JOURNEY_PROBE_API_ORIGIN`` + ``CORE_JOURNEY_PROBE_SECRET`` are set. The
identity is the probe's own test user (password derived from the secret, never
printed) and two reused agents, so nothing accumulates but one free task per run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import MaintenanceSettings
from .incidents import Violation, ingest_violation, observe_healthy, record_observation
from .taxonomy import IncidentClass, Priority, Severity, TrustClass

SLI, REF, SOURCE, VERSION = "core_journey", "core_journey:escrow", "core_journey_probe", "cj-1"
CAP = "core-journey-echo"
AGENTS = ("Core_Journey_Caller", "Core_Journey_Callee")
Http = Callable[[str, str, Optional[dict], Optional[str]], Tuple[int, Any]]


@dataclass
class JourneyResult:
    ok: bool
    duration_s: float
    step: str
    task_id: Optional[str] = None
    detail: str = ""


def refusal(settings) -> Optional[str]:
    if os.getenv("ENVIRONMENT", "development").strip().lower() not in ("staging", "development"):
        return "the core-journey probe runs on staging only"
    if not settings.core_journey_probe_api_origin or not os.getenv("CORE_JOURNEY_PROBE_SECRET", "").strip():
        return "CORE_JOURNEY_PROBE_API_ORIGIN and CORE_JOURNEY_PROBE_SECRET are required"
    return None


def urllib_http(origin: str, timeout: float = 20.0) -> Http:
    def call(method: str, path: str, body: Optional[dict], token: Optional[str]) -> Tuple[int, Any]:
        hdrs = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "agentnet-core-journey-probe"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(origin + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read().decode("utf-8", "replace"), exc.code
        except Exception as exc:  # noqa: BLE001 -- DNS, refused, timeout: the journey failed
            return 0, {"error": type(exc).__name__}
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {"raw": raw[:200]}
    return call


def _public_key(secret: str, name: str) -> str:
    import ed25519  # noqa: PLC0415 -- pinned registry dependency

    seed = hashlib.sha256(f"agentnet-core-journey:{name}:{secret}".encode()).digest()
    return base64.b64encode(ed25519.SigningKey(seed).get_verifying_key().to_bytes()).decode()


def run(http: Http, *, email: str, secret: str, verify: Callable[[str], None], clock: Callable[[], float] = time.monotonic) -> JourneyResult:
    t0 = clock()

    def result(ok: bool, step: str, task_id: Optional[str] = None, detail: str = "") -> JourneyResult:
        return JourneyResult(ok, round(clock() - t0, 3), step, task_id, detail[:200])

    password = "Cj1!" + hashlib.sha256(f"core-journey:{secret}".encode()).hexdigest()[:28]
    st, body = http("POST", "/v1/auth/user/register", {"email": email, "password": password}, None)
    if st not in (200, 201) and not (st == 400 and "already" in json.dumps(body).lower()):
        return result(False, "register", detail=f"HTTP {st}")
    verify(email)
    st, body = http("POST", "/v1/auth/user/login", {"email": email, "password": password}, None)
    token = (body or {}).get("access_token") if st == 200 else None
    if not token:
        return result(False, "login", detail=f"HTTP {st}")
    ids = {}
    caps = [{"name": CAP, "version": "1.0", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 0}]
    for name in AGENTS:
        st, body = http("POST", "/v1/agents/", {"name": name, "description": "AgentNet core-journey probe (staging, free)", "capabilities": caps,
                                                 "endpoint": "https://core-journey-probe.invalid/agent", "public_key": _public_key(secret, name)}, token)
        if st != 201:
            st, body = http("GET", f"/v1/agents/?capability={CAP}&limit=1000", None, token)
            body = next((a for a in (body if isinstance(body, list) else []) if a.get("name") == name), None)
        if not (body or {}).get("id"):
            return result(False, "agent", detail=f"{name} HTTP {st}")
        ids[name] = body["id"]
    caller, callee = (ids[n] for n in AGENTS)
    st, body = http("POST", "/v1/tasks/", {"caller_agent_id": caller, "callee_agent_id": callee, "capability": CAP,
                                          "input": {"probe": uuid.uuid4().hex}, "max_budget": 0}, token)
    task_id = (body or {}).get("task_session_id") if st == 201 else None
    if not task_id:
        return result(False, "escrow_task", detail=f"HTTP {st}")
    for step, path, payload in (("start", "start", None), ("complete", "confirm", {"probe": "ok"})):
        st, body = http("PUT", f"/v1/tasks/{task_id}/{path}?agent_id={callee}", payload, token)
        if st != 200:
            return result(False, step, task_id, f"HTTP {st}")
    st, body = http("GET", f"/v1/tasks/{task_id}", None, token)
    if st != 200 or (body or {}).get("status") != "completed":
        return result(False, "complete", task_id, f"HTTP {st} status {(body or {}).get('status')}")
    return result(True, "settled", task_id)


def mark_verified(db: Session, email: str) -> None:
    """Staging has no SMTP: the probe's OWN test user is marked verified, as the staging validator does."""
    db.execute(text("UPDATE users SET is_email_verified = TRUE WHERE lower(email) = lower(:e) AND is_email_verified IS NOT TRUE"), {"e": email})
    db.commit()


def observe(db: Session, ms: MaintenanceSettings, res: JourneyResult, *, target: str, now: Optional[datetime] = None) -> JourneyResult:
    """One observation per run; the payment must read settled (read-only). Commits."""
    if res.ok:
        tx = db.execute(text("SELECT status FROM transactions WHERE task_session_id = :t ORDER BY created_at DESC LIMIT 1"), {"t": res.task_id}).scalar()
        if str(tx) != "completed":
            res = JourneyResult(False, res.duration_s, "settlement", res.task_id, f"escrow transaction {tx}")
    payload = {"duration_s": res.duration_s, "step": res.step, "detail": res.detail}
    if res.ok:
        record_observation(db, sli=SLI, target=target, source=SOURCE, collector_version=VERSION, trust_class=TrustClass.TRUSTED_PROBE, ok=True, payload=payload, observed_at=now)
        observe_healthy(db, ms, target=target, desired_state_ref=REF, sli=SLI, source=SOURCE, collector_version=VERSION, now=now, record=False)
    else:
        ingest_violation(db, ms, Violation(target=target, incident_class=IncidentClass.FUNCTIONAL_CONTRACT, desired_state_ref=REF, failure=f"core_journey_{res.step}",
                                           severity=Severity.MAJOR, base_priority=Priority.P1, source=SOURCE, collector_version=VERSION, sli=SLI, payload=payload), now=now)
    db.commit()
    return res
