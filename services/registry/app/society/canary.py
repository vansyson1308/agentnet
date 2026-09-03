"""Live-model preflight and canary runner for the Autonomous Society Runtime.

    python -m app.society.canary preflight [--json] [--skip-probe] [--no-history-scan]
    python -m app.society.canary gate --role scout --intent CREATE_IMPROVEMENT [--clear]
    python -m app.society.canary run --scenario single|multi|approval [--decide approve|reject]
                                     [--operator-email EMAIL] [--report PATH] [--max-cycles N]
    python -m app.society.canary observe --api URL --scenario ... [--decide ...] [--report PATH]

Credential rules (docs/SOCIETY_LIVE_MODEL_RUNBOOK.md):

* The model credential is read from the environment only
  (``SOCIETY_MODEL_API_KEY``, else ``LLM_API_KEY``). It is never printed,
  never written to a report, never placed in a trace, a context or an
  exception message. Reports carry at most the first 8 hex chars of its
  SHA-256 fingerprint.
* A credential whose fingerprint is in ``COMPROMISED_CREDENTIAL_FINGERPRINTS``
  or matches a provider-key-shaped string anywhere in this checkout's git
  history is refused with ``LIVE MODEL BLOCKED — NO SAFE CREDENTIAL``.
* NO FAKE AUTONOMY: ``run`` and ``observe`` refuse any provider other than
  ``openai_compatible``. ``ScriptedRoleModel``/``FakeModel`` never count as
  live, and every run in a canary report must carry the live provider.

``run`` drives an in-process worker (like the deterministic demo) against
the configured database; ``observe`` only talks to a deployed registry over
HTTP (inject → poll story → decide → report), which is how the staging
canaries are meant to be executed while the staging society worker owns the
model credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from .config import SocietySettings, get_settings

logger = logging.getLogger(__name__)

LIVE_PROVIDER = "openai_compatible"

VERDICT_READY = "LIVE MODEL READY"
VERDICT_NO_CREDENTIAL = "LIVE MODEL BLOCKED — NO SAFE CREDENTIAL"
VERDICT_UNREACHABLE = "LIVE MODEL BLOCKED — PROVIDER UNREACHABLE"
VERDICT_NOT_LIVE = "LIVE MODEL BLOCKED — PROVIDER IS NOT LIVE (NO FAKE AUTONOMY)"

# SHA-256 fingerprints of provider-key-shaped strings that appeared in this
# repository's git history (found by the v1 security gate). The secrets are
# not stored anywhere; a hash cannot be reversed into the key. Any credential
# matching one of these is compromised until independently rotated.
COMPROMISED_CREDENTIAL_FINGERPRINTS: frozenset = frozenset(
    {
        "7557e924b55eba39ac318b87ae34f1fffc435bfc92d207830c6801613b1fdcae",
        "0da0e5d60ebfe5529eca6a5ef6bab689d5884b57f92e5e8009628e60bd2cc781",
        "1ec5a2b2179f6f63cbab18b3195f6957224989e316f9eda470deeba995b01449",
    }
)

# Shape of the provider keys we have seen leak (OpenAI-compatible vendors).
KEY_SHAPE = re.compile(r"(?:sk|dsk)-[A-Za-z0-9_\-]{16,}")

SCENARIOS: Dict[str, Dict[str, Any]] = {
    "single": {
        "event_type": "staging.canary.signal",
        "min_completed_runs": 1,
        "min_roles": 1,
        "description": "one agent (Scout subscribes to staging.canary.signal) wakes, reasons with the live model, emits typed intents",
    },
    "multi": {
        "event_type": "platform.metric.anomaly",
        "min_completed_runs": 2,
        "min_roles": 2,
        "description": "Scout -> Governor (-> Architect ...) chain driven only by society events",
    },
    "approval": {
        "event_type": "platform.metric.anomaly",
        "min_completed_runs": 1,
        "min_roles": 1,
        "requires_gate": True,
        "description": "an intent parks in awaiting_approval; the operator decides; the runtime resumes without re-deciding",
    },
}


class CanaryRefused(RuntimeError):
    """Raised when a canary must not run (fake provider, unsafe credential, ...)."""


# ── credential safety ─────────────────────────────────────────────────


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def scan_git_history_fingerprints(repo_root: str, *, timeout_seconds: float = 120.0) -> Tuple[Set[str], bool]:
    """Fingerprint every provider-key-shaped string in the full git history of
    ``repo_root``. Returns ``(fingerprints, complete)``; the raw strings are
    never retained. ``complete`` is False when git is unavailable, the path is
    not a repository, or the scan hit the time bound."""
    found: Set[str] = set()
    try:
        proc = subprocess.Popen(
            ["git", "-C", repo_root, "log", "--all", "-p", "--no-color"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
        )
    except (OSError, ValueError):
        return found, False
    complete = True
    deadline = time.monotonic() + timeout_seconds
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            for m in KEY_SHAPE.finditer(line):
                found.add(fingerprint(m.group(0)))
            if time.monotonic() > deadline:
                complete = False
                proc.kill()
                break
    finally:
        proc.stdout.close()
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            rc = -1
    if rc != 0:
        complete = False
    return found, complete


@dataclass
class CredentialStatus:
    source: Optional[str]  # env var the credential came from (name only)
    configured: bool
    compromised: bool
    history_scanned: bool
    history_scan_complete: bool
    fingerprint_prefix: Optional[str]
    reason: str

    @property
    def safe(self) -> bool:
        return self.configured and not self.compromised


def credential_source() -> Optional[str]:
    if os.getenv("SOCIETY_MODEL_API_KEY"):
        return "SOCIETY_MODEL_API_KEY"
    if os.getenv("LLM_API_KEY"):
        return "LLM_API_KEY"
    return None


def credential_status(
    settings: SocietySettings,
    *,
    repo_root: Optional[str] = None,
    scan_history: bool = True,
    compromised: Optional[Set[str]] = None,
) -> CredentialStatus:
    key = settings.model_api_key or ""
    source = credential_source()
    if not key:
        return CredentialStatus(source, False, False, False, False, None, "no model credential in the environment")
    denylist = COMPROMISED_CREDENTIAL_FINGERPRINTS if compromised is None else compromised
    fp = fingerprint(key)
    if fp in denylist:
        return CredentialStatus(source, True, True, False, False, fp[:8], "credential fingerprint is on the compromised list (leaked in git history; rotate it)")
    scanned = complete = False
    if scan_history:
        hist, complete = scan_git_history_fingerprints(repo_root or settings.repo_root)
        scanned = True
        if fp in hist:
            return CredentialStatus(source, True, True, True, complete, fp[:8], "credential appears in this repository's git history; it is compromised")
    note = "" if (not scanned or complete) else " (history scan incomplete: git unavailable or time bound hit)"
    return CredentialStatus(source, True, False, scanned, complete, fp[:8], "credential present and not known to be compromised" + note)


def scrub(text: str, settings: SocietySettings) -> str:
    """Defensive: a credential must never reach output, even via a provider echo."""
    key = settings.model_api_key or ""
    if key and key in text:
        text = text.replace(key, "***")
    return KEY_SHAPE.sub("***", text)


# ── provider probe ────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    ok: bool
    status: str
    latency_ms: Optional[int] = None
    requests: int = 0
    retries: int = 0
    timeouts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    json_ok: bool = False
    response_format: str = "json_object"
    error: Optional[str] = None


async def probe_provider(settings: SocietySettings, *, transport: Optional[Callable[..., Any]] = None) -> ProbeResult:
    """One minimal, bounded structured-output request through the same
    retry loop the runtime uses. Reports status/latency/usage only."""
    from .cognition import ModelProviderError, ModelTimeout, OpenAICompatibleModel

    try:
        model = OpenAICompatibleModel(settings, transport=transport)
    except ValueError as exc:
        return ProbeResult(False, "misconfigured", error=scrub(str(exc), settings))
    fmt = model._response_format()
    payload = {
        "model": model.model_name,
        "messages": [
            {"role": "system", "content": "You are a JSON-only service."},
            {"role": "user", "content": 'Reply with exactly {"ok": true} as a JSON object.'},
        ],
        "temperature": 0,
        "max_tokens": 20,
        "response_format": fmt,
    }
    stats = {"requests": 0, "retries": 0, "timeouts": 0}
    t0 = time.monotonic()
    try:
        data = await model._request_with_retries(payload, stats)
    except (ModelProviderError, ModelTimeout) as exc:
        return ProbeResult(False, "error", int((time.monotonic() - t0) * 1000), stats["requests"], stats["retries"], stats["timeouts"], response_format=fmt["type"], error=scrub(str(exc), settings)[:300])
    latency = int((time.monotonic() - t0) * 1000)
    content = None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        pass
    json_ok = False
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            json_ok = isinstance(parsed, dict) and parsed.get("ok") is True
        except ValueError:
            json_ok = False
    usage = (data.get("usage") if isinstance(data, dict) else None) or {}
    return ProbeResult(
        ok=json_ok,
        status="ok" if json_ok else "unexpected_content",
        latency_ms=latency,
        requests=stats["requests"],
        retries=stats["retries"],
        timeouts=stats["timeouts"],
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        json_ok=json_ok,
        response_format=fmt["type"],
        error=None if json_ok else "provider did not return the requested JSON object",
    )


# ── preflight ─────────────────────────────────────────────────────────


@dataclass
class PreflightReport:
    verdict: str
    provider: str
    model_name: str
    base_url_host: Optional[str]
    credential: CredentialStatus
    probe: Optional[ProbeResult]
    flags: Dict[str, Any]
    limits: Dict[str, Any]

    @property
    def ready(self) -> bool:
        return self.verdict == VERDICT_READY

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["credential"]["safe"] = self.credential.safe
        return d


def _host(url: str) -> Optional[str]:
    try:
        return urlparse(url).hostname if url else None
    except ValueError:
        return None


def preflight(
    settings: Optional[SocietySettings] = None,
    *,
    repo_root: Optional[str] = None,
    transport: Optional[Callable[..., Any]] = None,
    skip_probe: bool = False,
    scan_history: bool = True,
) -> PreflightReport:
    settings = settings or get_settings()
    cred = credential_status(settings, repo_root=repo_root, scan_history=scan_history)
    limits = {
        "daily_model_budget_usd": str(settings.daily_model_budget_usd),
        "model_timeout_seconds": settings.model_timeout_seconds,
        "model_request_retries": settings.model_request_retries,
        "model_retry_backoff_seconds": settings.model_retry_backoff_seconds,
        "model_json_schema": settings.model_json_schema,
        "max_runs_per_hour": settings.max_runs_per_hour,
        "max_runs_per_correlation": settings.max_runs_per_correlation,
        "max_causation_depth": settings.max_causation_depth,
    }
    base = dict(
        provider=settings.model_provider,
        model_name=settings.model_name if settings.model_provider == LIVE_PROVIDER else "",
        base_url_host=_host(settings.model_base_url),
        credential=cred,
        probe=None,
        flags=settings.public_flags(),
        limits=limits,
    )
    if settings.model_provider != LIVE_PROVIDER:
        return PreflightReport(verdict=VERDICT_NOT_LIVE, **base)
    if not cred.safe:
        return PreflightReport(verdict=VERDICT_NO_CREDENTIAL, **base)
    if not settings.model_base_url:
        base["probe"] = ProbeResult(False, "misconfigured", error="SOCIETY_MODEL_BASE_URL (or LLM_BASE_URL) is not set")
        return PreflightReport(verdict=VERDICT_UNREACHABLE, **base)
    if skip_probe:
        return PreflightReport(verdict=VERDICT_READY, **base)
    probe = asyncio.run(probe_provider(settings, transport=transport))
    base["probe"] = probe
    return PreflightReport(verdict=VERDICT_READY if probe.ok else VERDICT_UNREACHABLE, **base)


# ── canary report ─────────────────────────────────────────────────────


@dataclass
class CanaryReport:
    scenario: str
    mode: str
    verdict: str
    reasons: List[str]
    provider: str
    model_name: str
    transport: str
    correlation_id: str
    runs: List[Dict[str, Any]] = field(default_factory=list)
    intents: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    approvals: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    totals: Dict[str, Any] = field(default_factory=dict)
    worker_stats: Dict[str, Any] = field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    from .events import utcnow

    return utcnow().isoformat()


def _totals(runs: List[Dict[str, Any]], intents: List[Dict[str, Any]], approvals: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed = [r for r in runs if r["status"] == "completed"]
    return {
        "runs": len(runs),
        "runs_completed": len(completed),
        "runs_dead": sum(1 for r in runs if r["status"] == "dead"),
        "distinct_roles_completed": sorted({r["role"] for r in completed}),
        "tokens_in": sum(int(r.get("tokens_in") or 0) for r in runs),
        "tokens_out": sum(int(r.get("tokens_out") or 0) for r in runs),
        "cost_usd": str(sum(float(r.get("cost_usd") or 0) for r in runs)),
        "model_requests": sum(int(r.get("model_requests") or 0) for r in runs),
        "model_retries": sum(int(r.get("model_retries") or 0) for r in runs),
        "model_timeouts": sum(int(r.get("model_timeouts") or 0) for r in runs),
        "intents": len(intents),
        "intents_by_policy": _count(intents, "policy_decision"),
        "intents_by_execution": _count(intents, "execution_status"),
        "approvals": len(approvals),
    }


def _count(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        k = str(r.get(key))
        out[k] = out.get(k, 0) + 1
    return out


def evaluate(scenario: str, report: CanaryReport, *, decided: Optional[str], expected_model: str) -> Tuple[str, List[str]]:
    """PASS / FAIL / PARKED with reasons; never trusts a self-report."""
    spec = SCENARIOS[scenario]
    reasons: List[str] = []
    runs = report.runs
    for r in runs:
        if r["status"] == "completed" and r.get("model_provider") != LIVE_PROVIDER:
            reasons.append(f"fake autonomy: run {r['id'][:8]} ({r['role']}) used provider {r.get('model_provider')!r}")
        if r["status"] == "completed" and expected_model and r.get("model_name") not in (None, expected_model):
            reasons.append(f"run {r['id'][:8]} reported model {r.get('model_name')!r}, expected {expected_model!r}")
    completed = [r for r in runs if r["status"] == "completed"]
    if len(completed) < spec["min_completed_runs"]:
        reasons.append(f"{len(completed)} completed run(s), scenario needs {spec['min_completed_runs']}")
    roles = {r["role"] for r in completed}
    if len(roles) < spec["min_roles"]:
        reasons.append(f"{len(roles)} role(s) completed a run, scenario needs {spec['min_roles']}")
    if scenario == "multi" and not any(int(e.get("causation_depth") or 0) >= 1 for e in report.events):
        reasons.append("no causation-linked follow-up event: the chain did not continue past the injected event")
    if any(r["status"] == "dead" for r in runs):
        reasons.append("a run reached DEAD (retries exhausted)")
    if scenario == "approval":
        parked = [i for i in report.intents if i.get("execution_status") in ("awaiting_approval", "approved", "executed", "rejected", "denied", "failed") and i.get("policy_decision") == "approval_required"]
        if not parked:
            reasons.append("no intent required human approval (is a gate configured? see `canary gate`)")
            return "FAIL", reasons
        if decided is None:
            return ("PARKED", reasons + ["intent(s) await a human decision"]) if not reasons else ("FAIL", reasons)
        states = {i.get("execution_status") for i in parked}
        want = "executed" if decided == "approve" else "rejected"
        if want not in states:
            reasons.append(f"after {decided}, parked intent states were {sorted(str(s) for s in states)}; expected {want!r}")
        if decided == "reject" and "executed" in states:
            reasons.append("a rejected intent was executed")
        if not any(a.get("decision") for a in report.approvals):
            reasons.append("no intent_approvals audit row was written")
    return ("PASS" if not reasons else "FAIL"), reasons


# ── in-process canary (DB + worker in this process) ──────────────────


def _payload_for(scenario: str, tag: str) -> Dict[str, Any]:
    if scenario == "single":
        return {"signal": "canary", "scenario": "single", "tag": tag, "note": "staging canary signal: observe the platform and report; no change is required"}
    return {
        "metric": f"task_failure_rate_canary_{tag}",
        "value": 0.42,
        "threshold": 0.10,
        "description": "task failure rate above threshold for 15 minutes (staging canary)",
        "severity_score": 70,
        "tag": tag,
    }


def run_canary(
    session_factory,
    settings: Optional[SocietySettings] = None,
    *,
    scenario: str,
    decide: Optional[str] = None,
    operator_email: Optional[str] = None,
    max_cycles: int = 60,
    transport: Optional[Callable[..., Any]] = None,
    worker_id: Optional[str] = None,
    scan_history: bool = True,
    skip_probe: bool = False,
) -> CanaryReport:
    """Inject ONE event and let an in-process worker drive the live model.
    Refuses fake providers and unsafe credentials. Never writes Builder
    output, QA/Security verdicts or grants."""
    from sqlalchemy import func

    from ..models import AgentCapabilityGrant, AgentIntent, AgentRun, CodeCandidate, IntentApproval, SocietyEvent, User
    from . import approvals as approvals_mod
    from .cognition import OpenAICompatibleModel
    from .events import emit_event
    from .operator_auth import is_operator
    from .seed import seed_society
    from .worker import SocietyWorker

    if scenario not in SCENARIOS:
        raise CanaryRefused(f"unknown scenario {scenario!r}; choose one of {sorted(SCENARIOS)}")
    if decide not in (None, "approve", "reject"):
        raise CanaryRefused("--decide must be approve or reject")
    settings = settings or get_settings()
    if settings.model_provider != LIVE_PROVIDER:
        raise CanaryRefused(f"{VERDICT_NOT_LIVE}: SOCIETY_MODEL_PROVIDER={settings.model_provider!r}")
    if not settings.runtime_enabled:
        raise CanaryRefused("SOCIETY_RUNTIME_ENABLED must be true for the canary process (set it for this process only)")
    pre = preflight(settings, transport=transport, skip_probe=skip_probe, scan_history=scan_history)
    if not pre.ready:
        raise CanaryRefused(pre.verdict + (f": {pre.credential.reason}" if not pre.credential.safe else "") + (f": {pre.probe.error}" if pre.probe and pre.probe.error else ""))

    model = OpenAICompatibleModel(settings, transport=transport)
    tag = uuid.uuid4().hex[:8]
    correlation = uuid.uuid4()
    report = CanaryReport(
        scenario=scenario,
        mode="in_process",
        verdict="RUNNING",
        reasons=[],
        provider=settings.model_provider,
        model_name=settings.model_name,
        transport="injected" if transport is not None else "live",
        correlation_id=str(correlation),
        started_at=_now_iso(),
    )
    db = session_factory()
    try:
        # Seed ONLY when the fleet is missing: a canary must never rewrite the
        # operator's grants (limits, cooldowns, gates) back to role defaults.
        if (db.query(func.count(AgentCapabilityGrant.id)).scalar() or 0) == 0:
            seed_society(db)
        operator: Optional[User] = None
        if decide is not None:
            if not operator_email:
                raise CanaryRefused("--decide needs --operator-email (a user with society_role=operator)")
            operator = db.query(User).filter(User.email == operator_email).first()
            if operator is None or not is_operator(operator):
                raise CanaryRefused(f"{operator_email!r} is not a society operator; assign the role first")
        if SCENARIOS[scenario].get("requires_gate"):
            gated = db.query(func.count(AgentCapabilityGrant.id)).filter(func.jsonb_array_length(AgentCapabilityGrant.approval_required_intents) > 0).scalar() or 0
            if not gated:
                raise CanaryRefused("approval scenario needs a gate: python -m app.society.canary gate --role scout --intent CREATE_IMPROVEMENT")
        ev = emit_event(
            db,
            event_type=SCENARIOS[scenario]["event_type"],
            payload=_payload_for(scenario, tag),
            actor_type="user" if operator is not None else "system",
            actor_id=operator.id if operator is not None else None,
            correlation_id=correlation,
            idempotency_key=f"canary-{scenario}-{tag}",
        )
        db.commit()
        logger.info("canary %s: injected %s id=%s correlation=%s", scenario, ev.event_type, ev.id, correlation)
    finally:
        db.close()

    worker = SocietyWorker(session_factory, settings=settings, model=model, worker_id=worker_id or f"canary-{tag}")
    stats = asyncio.run(worker.run_until_idle(max_cycles=max_cycles))
    merged = {k: v for k, v in stats.as_dict().items() if k != "processed_run_ids"}

    if decide is not None:
        db = session_factory()
        try:
            deciding_user = db.query(User).filter(User.email == operator_email).first()  # re-attach in this session
            if deciding_user is None or not is_operator(deciding_user):
                raise CanaryRefused(f"{operator_email!r} lost the operator role while the canary ran; not deciding")
            run_ids = [r.id for r in db.query(AgentRun).filter(AgentRun.correlation_id == correlation).all()]
            parked = db.query(AgentIntent).filter(AgentIntent.run_id.in_(run_ids), AgentIntent.execution_status == "awaiting_approval").all() if run_ids else []
            for intent in parked:
                approvals_mod.decide(db, intent_id=intent.id, user=deciding_user, decision="approved" if decide == "approve" else "rejected", reason=f"canary {scenario} ({decide})")
                logger.info("canary %s: %s intent %s (%s)", scenario, decide, intent.id, intent.intent_type)
        finally:
            db.close()
        if parked:
            stats2 = asyncio.run(worker.run_until_idle(max_cycles=max_cycles))
            for k, v in stats2.as_dict().items():
                if k != "processed_run_ids":
                    merged[k] = (merged.get(k) or 0) + (v or 0)

    db = session_factory()
    try:
        runs = db.query(AgentRun).filter(AgentRun.correlation_id == correlation).order_by(AgentRun.created_at).all()
        run_ids = [r.id for r in runs]
        intents = db.query(AgentIntent).filter(AgentIntent.run_id.in_(run_ids)).order_by(AgentIntent.created_at, AgentIntent.seq).all() if run_ids else []
        events = db.query(SocietyEvent).filter(SocietyEvent.correlation_id == correlation).order_by(SocietyEvent.created_at).all()
        approvals = db.query(IntentApproval).filter(IntentApproval.run_id.in_(run_ids)).all() if run_ids else []
        candidates = db.query(CodeCandidate).filter(CodeCandidate.correlation_id == correlation).all()
        report.runs = [
            {
                "id": str(r.id),
                "role": r.role,
                "status": _ev(r.status),
                "attempt": r.attempt,
                "model_provider": r.model_provider,
                "model_name": r.model_name,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "cost_usd": str(r.cost_usd or 0),
                "model_requests": r.model_requests,
                "model_retries": r.model_retries,
                "model_timeouts": r.model_timeouts,
                "intents_count": r.intents_count,
                "error": scrub(r.error, settings)[:160] if r.error else None,
            }
            for r in runs
        ]
        report.intents = [{"run_id": str(i.run_id), "seq": i.seq, "intent_type": i.intent_type, "risk_class": _ev(i.risk_class), "policy_decision": _ev(i.policy_decision), "execution_status": _ev(i.execution_status)} for i in intents]
        report.events = [{"event_type": e.event_type, "causation_depth": e.causation_depth, "status": _ev(e.status)} for e in events]
        report.approvals = [{"intent_id": str(a.intent_id), "decision": _ev(a.decision), "final_state": a.final_state, "resumed": a.resumed_at is not None} for a in approvals]
        report.candidates = [{"id": str(c.id), "status": _ev(c.status)} for c in candidates]
    finally:
        db.close()
    report.worker_stats = merged
    report.totals = _totals(report.runs, report.intents, report.approvals)
    report.verdict, report.reasons = evaluate(scenario, report, decided=decide, expected_model=settings.model_name)
    report.finished_at = _now_iso()
    return report


def _ev(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


# ── HTTP-only canary (against a deployed registry) ───────────────────


class _Http:
    def __init__(self, base: str, token: Optional[str], timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw[:300]


_ACTIVE_RUN = {"queued", "claimed", "running"}
_ACTIVE_EVENT = {"pending", "dispatched"}


def observe_canary(
    api: str,
    token: Optional[str],
    *,
    scenario: str,
    decide: Optional[str] = None,
    timeout_seconds: float = 900.0,
    poll_seconds: float = 5.0,
    http: Optional[_Http] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CanaryReport:
    """Drive a canary purely over HTTP: the deployed society worker owns the
    model credential; this process only injects, observes and decides."""
    if scenario not in SCENARIOS:
        raise CanaryRefused(f"unknown scenario {scenario!r}")
    if decide not in (None, "approve", "reject"):
        raise CanaryRefused("--decide must be approve or reject")
    if not token:
        raise CanaryRefused("an operator token is required (SOCIETY_CANARY_TOKEN); it is never printed")
    h = http or _Http(api, token)
    status, body = h.call("GET", "/v1/society/status")
    if status != 200 or not isinstance(body, dict):
        raise CanaryRefused(f"GET /v1/society/status returned {status}")
    if body.get("model_provider") != LIVE_PROVIDER:
        raise CanaryRefused(f"{VERDICT_NOT_LIVE}: deployed model_provider={body.get('model_provider')!r}")
    if not body.get("runtime_enabled"):
        raise CanaryRefused("deployed runtime is disabled (SOCIETY_RUNTIME_ENABLED=false on the society worker)")
    if body.get("production_deploy_enabled") is not False:
        raise CanaryRefused("status does not report production_deploy_enabled=false; refusing")

    tag = uuid.uuid4().hex[:8]
    correlation = str(uuid.uuid4())
    report = CanaryReport(scenario=scenario, mode="observe", verdict="RUNNING", reasons=[], provider=LIVE_PROVIDER, model_name="", transport="live", correlation_id=correlation, started_at=_now_iso())
    status, body = h.call("POST", "/v1/society/events", {"event_type": SCENARIOS[scenario]["event_type"], "payload": _payload_for(scenario, tag), "correlation_id": correlation, "idempotency_key": f"canary-{scenario}-{tag}"})
    if status != 201:
        raise CanaryRefused(f"event injection failed with HTTP {status}: {str(body)[:200]}")

    deadline = time.monotonic() + timeout_seconds
    idle_polls = 0
    decided_ids: Set[str] = set()
    detail: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, detail = h.call("GET", f"/v1/society/story/{correlation}/detail")
        if status != 200 or not isinstance(detail, dict):
            raise CanaryRefused(f"story detail returned {status} (operator token required)")
        runs = detail.get("runs") or []
        events = detail.get("events") or []
        if decide is not None:
            for r in runs:
                for i in r.get("intents") or []:
                    if i.get("execution_status") == "awaiting_approval" and i["id"] not in decided_ids:
                        s, b = h.call("POST", f"/v1/society/intents/{i['id']}/{decide}", {"reason": f"canary {scenario} ({decide})"})
                        if s != 200:
                            report.reasons.append(f"{decide} on intent {i['id'][:8]} returned HTTP {s}")
                        decided_ids.add(i["id"])
        active = any(r.get("status") in _ACTIVE_RUN for r in runs) or any(e.get("status") in _ACTIVE_EVENT for e in events)
        waiting_human = decide is None and any(i.get("execution_status") == "awaiting_approval" for r in runs for i in r.get("intents") or [])
        if active:
            idle_polls = 0
        elif runs or waiting_human:
            idle_polls += 1
            if idle_polls >= 2:
                break
        sleep(poll_seconds)
    else:
        report.reasons.append(f"timed out after {int(timeout_seconds)}s waiting for the story to go idle")

    runs = detail.get("runs") or []
    report.runs = [{k: r.get(k) for k in ("id", "role", "status", "attempt", "model_provider", "model_name", "tokens_in", "tokens_out", "cost_usd", "model_requests", "model_retries", "model_timeouts", "intents_count")} | {"error": (KEY_SHAPE.sub("***", str(r.get("error")))[:160] if r.get("error") else None)} for r in runs]
    report.intents = [{"run_id": r.get("id"), "seq": i.get("seq"), "intent_type": i.get("intent_type"), "risk_class": i.get("risk_class"), "policy_decision": i.get("policy_decision"), "execution_status": i.get("execution_status")} for r in runs for i in r.get("intents") or []]
    report.events = [{"event_type": e.get("event_type"), "causation_depth": e.get("causation_depth"), "status": e.get("status")} for e in detail.get("events") or []]
    report.approvals = [{"intent_id": i.get("id"), "decision": (i.get("approval") or {}).get("decision"), "final_state": (i.get("approval") or {}).get("final_state"), "resumed": bool((i.get("approval") or {}).get("resumed_at"))} for r in runs for i in r.get("intents") or [] if i.get("approval")]
    report.candidates = [{"id": c.get("id"), "status": c.get("status")} for c in detail.get("candidates") or []]
    names = {r.get("model_name") for r in runs if r.get("model_name")}
    report.model_name = sorted(names)[0] if len(names) == 1 else ",".join(sorted(str(n) for n in names))
    report.totals = _totals(report.runs, report.intents, report.approvals)
    verdict, reasons = evaluate(scenario, report, decided=decide, expected_model="")
    report.verdict, report.reasons = verdict, report.reasons + reasons
    report.finished_at = _now_iso()
    return report


# ── operator gate (human action, never an intent) ────────────────────


def set_gate(db, *, role: str, intent_type: Optional[str], clear: bool = False) -> List[str]:
    """Require human approval for ``intent_type`` on ``role``'s grant (or clear
    the list). This is an operator action recorded in agent_capability_grants;
    no intent can perform it."""
    from ..models import AgentCapabilityGrant
    from .intents import IntentType

    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.role == role).first()
    if grant is None:
        raise CanaryRefused(f"no grant for role {role!r} (seed the fleet first)")
    if clear:
        grant.approval_required_intents = []
    else:
        if not intent_type:
            raise CanaryRefused("--intent is required unless --clear")
        it = IntentType(intent_type).value
        if it not in set(grant.allowed_intents or []):
            raise CanaryRefused(f"{it} is not an allowed intent for role {role!r}; gates only narrow existing permissions")
        current = list(grant.approval_required_intents or [])
        if it not in current:
            current.append(it)
        grant.approval_required_intents = current
    db.commit()
    return list(grant.approval_required_intents or [])


# ── CLI ───────────────────────────────────────────────────────────────


def _emit(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")
    sys.stdout.flush()


def _write_report(report: CanaryReport, path: Optional[str]) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, default=str)


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover — CLI
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.society.canary", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preflight", help="credential safety + provider probe (no DB needed)")
    p.add_argument("--skip-probe", action="store_true")
    p.add_argument("--no-history-scan", action="store_true")

    g = sub.add_parser("gate", help="operator: require approval for an intent type on a role")
    g.add_argument("--role", required=True)
    g.add_argument("--intent")
    g.add_argument("--clear", action="store_true")

    for name in ("run", "observe"):
        r = sub.add_parser(name)
        r.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
        r.add_argument("--decide", choices=["approve", "reject"])
        r.add_argument("--report")
        if name == "run":
            r.add_argument("--operator-email")
            r.add_argument("--max-cycles", type=int, default=60)
            r.add_argument("--no-history-scan", action="store_true")
        else:
            r.add_argument("--api", default=os.getenv("SOCIETY_CANARY_API_URL", "http://localhost:8100"))
            r.add_argument("--timeout", type=float, default=900.0)
            r.add_argument("--poll", type=float, default=5.0)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "preflight":
            rep = preflight(skip_probe=args.skip_probe, scan_history=not args.no_history_scan)
            _emit(rep.to_dict())
            return 0 if rep.ready else 3
        if args.cmd == "gate":
            from ..database import SessionLocal

            db = SessionLocal()
            try:
                current = set_gate(db, role=args.role, intent_type=args.intent, clear=args.clear)
            finally:
                db.close()
            _emit({"role": args.role, "approval_required_intents": current})
            return 0
        if args.cmd == "run":
            from ..database import SessionLocal

            rep = run_canary(SessionLocal, scenario=args.scenario, decide=args.decide, operator_email=args.operator_email, max_cycles=args.max_cycles, scan_history=not args.no_history_scan)
        else:
            rep = observe_canary(args.api, os.getenv("SOCIETY_CANARY_TOKEN"), scenario=args.scenario, decide=args.decide, timeout_seconds=args.timeout, poll_seconds=args.poll)
        _write_report(rep, args.report)
        _emit(rep.to_dict())
        return 0 if rep.verdict in ("PASS", "PARKED") else 1
    except CanaryRefused as exc:
        _emit({"verdict": "REFUSED", "reason": str(exc)})
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
