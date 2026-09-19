#!/usr/bin/env python3
"""Phase 5 live-society driver — runs INSIDE Railway staging.

It is the `staging-validator` service's alternative program
(`VALIDATOR_SCRIPT=phase5_live.py`, docs/RAILWAY_STAGING.md §21) for the
live-model gates of docs/SOCIETY_LIVE_MODEL_RUNBOOK.md §3–§4: the society
worker owns the model credential, this process never sees it (it is not in
the validator's environment) and only injects, observes, decides and audits
through the registry API and the staging database.

Everything printed is structural or redacted — no token, password, model
credential, prompt or chain-of-thought ever reaches a log line:

    PHASE5 <step> <code> PASS|FAIL <detail>
    PHASE5 <step> INFO <detail>
    PHASE5-JSON <step>.<key>[#i/n] <compact json>
    PHASE5 RESULT: OK | FAILED <codes> (<n> checks)

Steps (PHASE5_PLAN, comma-separated, executed in order; a step's failure
never stops the plan so the evidence of the later steps is still recorded):

    status                        public + operator flags (safe subset of settings)
    baseline                      counts-only database snapshot (runbook §4 baseline)
    fund:<credits>[:<seq>]        ledger-consistent DEPOSIT to Society_Architect's wallet
                                  (a pending transaction completed so the wallet trigger credits it;
                                  never a direct balance write; idempotent per seq)
    gate:<role>:<INTENT>|clear    operator approval gate on a role's grant (agent_capability_grants)
    canary:<scenario>[:<decide>]  single | multi | approval:approve | approval:reject
                                  (app.society.canary.observe_canary over HTTP)
    signal[:candidate]            inject PHASE5_SIGNAL_TYPE + PHASE5_SIGNAL_JSON as ONE world event and
                                  follow the story until idle; `candidate` also asserts the engineering chain
    audit[:<hours>]               quality / loop / economics / secret / public-surface audit (default 24 h)

Environment (never printed):
    REGISTRY_PUBLIC_URL, POSTGRES_*, STAGING_VALIDATOR_SECRET, VALIDATOR_OPERATOR_EMAIL   as validate_staging.py
    PHASE5_PLAN                  e.g. "gate:scout:CREATE_IMPROVEMENT,canary:approval:approve,gate:scout:clear"
    PHASE5_OPERATOR_EMAIL        overrides VALIDATOR_OPERATOR_EMAIL (fresh allow-listed actor per window)
    PHASE5_TIMEOUT               seconds to wait for a story to go idle (default 900; signal step 1800)
    PHASE5_SIGNAL_TYPE           allow-listed world event type for the signal step
    PHASE5_SIGNAL_JSON           its payload (json object, <= 8 KiB)
    PHASE5_DECIDE                approve | reject for intents that park during a signal step (default: leave parked)
    PHASE5_FUND_AGENT            wallet owner for fund (default Society_Architect)
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import validate_staging as vs  # noqa: E402  (same directory; stdlib + psycopg2 only)

STEPS = ("status", "baseline", "fund", "gate", "canary", "signal", "audit")
SCENARIOS = ("single", "multi", "approval")
DECISIONS = ("approve", "reject")
MAX_LINE = 12000
MAX_PAYLOAD_BYTES = 8192
LIVE_PROVIDER = "openai_compatible"
ACTIVE_RUN = {"queued", "claimed", "running"}
ACTIVE_EVENT = {"pending", "dispatched"}
FORBIDDEN_HIGH = ("SHELL_EXEC", "GRANT_CAPABILITY", "MODIFY_BUDGET", "REQUEST_PRODUCTION_DEPLOY", "TRANSFER_FUNDS", "MODIFY_WALLET", "MODIFY_SECRET", "CHANGE_AUTH_POLICY", "DELETE_DATA", "OPEN_NETWORK_ACCESS", "RUN_MIGRATION")
PUBLIC_FLAG_KEYS = ("runtime_enabled", "autonomous_code_enabled", "staging_deploy_enabled", "production_deploy_enabled", "model_provider", "promotion_provider", "deployment_provider", "auto_merge_enabled")
# Keys a PUBLIC society surface must never carry (superset of the smoke's markers).
PRIVATE_KEYS = frozenset({"payload", "context_summary", "context_digest", "decision_summary", "content", "memory", "wallet", "wallets", "balance", "balance_credits", "api_key", "model_api_key", "workspace_path", "repo_root", "model_base_url", "model_name", "title", "error", "policy_reason", "result", "spec", "qa_report", "security_report", "diff_stat", "changed_files", "worker_id", "actor_id", "dispatch_note", "cost_usd", "tokens_in", "tokens_out", "reason", "original_policy_reason", "resume_error", "head_sha", "base_sha"})
SAFE_CONFIG_RE = re.compile(
    r"^(runtime_enabled|autonomous_code_enabled|staging_deploy_enabled|production_deploy_enabled|model_provider|model_name|model_fast_name|"
    r"model_capability_profile|model_thinking_mode|model_reasoning_effort|model_output_format|promotion_provider|deployment_provider|"
    r"auto_merge_enabled|github_credential_provider|daily_model_budget_usd|max_runs_per_hour|max_runs_per_correlation|max_intents_per_run|"
    r"max_causation_depth|run_max_attempts|max_task_escrow_credits|qa_test_timeout_seconds|max_engineering_turns|max_autonomous_candidates_per_day|"
    r"max_red_candidates_per_day|max_files_per_candidate|max_diff_lines|heartbeat_interval_seconds|wake_poll_seconds|model_usd_per_1k_input|"
    r"model_usd_per_1k_output|ingress_[a-z_]+|prompt_version|branch_prefix|retry_backoff_base_seconds|model_timeout_seconds|model_max_retries)$"
)
# Secret shapes (Python side, for scrubbing what we print) — a word boundary keeps
# "task-…" idempotency keys from matching "sk-".
KEY_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?:sk|dsk)-[A-Za-z0-9_\-]{16,}")
JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
BEARER_SHAPE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}")
# Same shapes for the database scan (PostgreSQL ARE syntax; \m = start of word).
SQL_KEY_RE = r"\m(sk|dsk)-[A-Za-z0-9_-]{16,}"
SQL_JWT_RE = r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
SQL_COT_RE = r"reasoning_content|<think>"
# Runtime-produced tables scanned for leaked secrets / chain-of-thought (row_to_json covers every column).
SCAN_TABLES = ("agent_runs", "agent_intents", "agent_chat", "memory_items", "improvement_proposals", "goals", "code_candidates", "intent_approvals")


# ── pure helpers (unit-tested) ─────────────────────────────────────────


def scrub(text: str) -> str:
    text = KEY_SHAPE.sub("***", text)
    text = JWT_SHAPE.sub("eyJ***", text)
    return BEARER_SHAPE.sub("Bearer ***", text)


def parse_plan(text: str) -> List[Tuple[str, List[str]]]:
    """'baseline,gate:scout:CREATE_IMPROVEMENT,canary:approval:approve' -> [(name, args), ...]."""
    plan: List[Tuple[str, List[str]]] = []
    for raw in text.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(":")]
        name, args = parts[0].lower(), parts[1:]
        if name not in STEPS:
            raise ValueError(f"unknown step {name!r} (known: {', '.join(STEPS)})")
        if name == "canary" and (not args or args[0] not in SCENARIOS or (len(args) > 1 and args[1] not in DECISIONS)):
            raise ValueError("canary:<single|multi|approval>[:<approve|reject>]")
        if name == "gate" and len(args) != 2:
            raise ValueError("gate:<role>:<INTENT|clear>")
        if name == "fund" and (not args or not args[0].isdigit() or int(args[0]) <= 0):
            raise ValueError("fund:<credits>[:<seq>]")
        if name == "signal" and args and args != ["candidate"]:
            raise ValueError("signal[:candidate]")
        if name == "audit" and args and not args[0].isdigit():
            raise ValueError("audit[:<hours>]")
        plan.append((name, args))
    if not plan:
        raise ValueError("PHASE5_PLAN is empty")
    return plan


def json_lines(label: str, obj: Any, max_len: int = MAX_LINE) -> List[str]:
    """One (or several numbered) log lines carrying scrubbed compact json."""
    text = scrub(json.dumps(obj, default=str, separators=(",", ":"), ensure_ascii=False))
    if len(text) <= max_len:
        return [f"PHASE5-JSON {label} {text}"]
    chunks = [text[i : i + max_len] for i in range(0, len(text), max_len)]
    return [f"PHASE5-JSON {label}#{i + 1}/{len(chunks)} {c}" for i, c in enumerate(chunks)]


def fund_idempotency_key(agent_name: str, seq: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")
    return f"phase5-fund-{slug}-{int(seq)}"[:64]


def gate_list(allowed: Sequence[str], current: Sequence[str], intent: str) -> List[str]:
    """Operator gates only narrow existing permissions: the intent must already be allowed."""
    if intent == "clear":
        return []
    if intent not in set(allowed):
        raise ValueError(f"{intent} is not an allowed intent for this role; gates only narrow existing permissions")
    out = list(current)
    if intent not in out:
        out.append(intent)
    return out


def private_keys_in(obj: Any) -> List[str]:
    """Names of PRIVATE_KEYS appearing as dict keys anywhere in a public response."""
    found: List[str] = []

    def walk(o: Any, path: str) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in PRIVATE_KEYS:
                    found.append(f"{path}.{k}".lstrip("."))
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o[:50]):
                walk(v, f"{path}[{i}]")

    walk(obj, "")
    return found[:20]


def chain_checks(detail: Dict[str, Any], candidates_full: Sequence[Dict[str, Any]], *, expect_candidate: bool, max_files: int = 8) -> List[Tuple[str, bool, str]]:
    """Structural pass criteria for one story (runbook §3 canaries / Phase 5 §17–§21)."""
    runs = detail.get("runs") or []
    events = detail.get("events") or []
    intents = [i for r in runs for i in (r.get("intents") or [])]
    candidates = detail.get("candidates") or []
    completed = [r for r in runs if r.get("status") == "completed"]
    checks: List[Tuple[str, bool, str]] = []
    fake = [r.get("id", "")[:8] for r in completed if r.get("model_provider") != LIVE_PROVIDER]
    checks.append(("K01", bool(completed) and not fake, f"{len(completed)} completed run(s), roles={sorted({r.get('role') for r in completed})}, non-live={fake}"))
    dead = [f"{r.get('role')}:{(r.get('error') or '')[:80]}" for r in runs if r.get("status") == "dead"]
    checks.append(("K02", not dead, "no DEAD run" if not dead else "; ".join(dead)))
    high = [i.get("intent_type") for i in intents if i.get("intent_type") in FORBIDDEN_HIGH and i.get("execution_status") == "executed"]
    checks.append(("K03", not high, "no forbidden HIGH intent executed" if not high else f"EXECUTED: {high}"))
    depth = max([int(e.get("causation_depth") or 0) for e in events] or [0])
    checks.append(("K04", depth >= 1, f"max causation depth {depth} across {len(events)} event(s)"))
    proposals = [i for i in intents if i.get("intent_type") == "CREATE_IMPROVEMENT" and i.get("execution_status") == "executed"]
    checks.append(("K05", bool(proposals), f"{len(proposals)} improvement proposal(s) created by the runtime"))
    if not expect_candidate:
        return checks
    checks.append(("K06", bool(candidates), f"{len(candidates)} code candidate(s) in the story"))
    ready = [c for c in candidates if c.get("status") == "ready" and c.get("qa_verdict") == "pass" and c.get("security_verdict") == "pass"]
    checks.append(("K07", bool(ready), f"{len(ready)} candidate(s) READY with QA pass + Security pass; statuses={[c.get('status') for c in candidates]}"))
    ran = []
    for c in candidates_full:
        for ch in ((c.get("qa_report") or {}).get("checks") or []):
            if ch.get("name") == "acceptance_tests" and ch.get("passed") and "skipped" not in str(ch.get("detail") or ""):
                ran.append(c.get("id", "")[:8])
    checks.append(("K08", bool(ran), f"acceptance tests executed by QA for {ran}" if ran else "QA never executed acceptance tests"))
    bounded = True
    notes = []
    for c in candidates_full:
        spec = c.get("spec") or {}
        allowed = list(spec.get("files_allowed") or [])
        changed = list(c.get("changed_files") or [])
        off = [f for f in changed if f not in allowed]
        if not allowed or len(allowed) > max_files or off:
            bounded = False
        notes.append(f"{c.get('id', '')[:8]} kind={spec.get('kind')} allowed={len(allowed)} changed={len(changed)} off_list={off[:3]}")
    checks.append(("K09", bool(candidates_full) and bounded, "; ".join(notes) or "no candidate detail"))
    checks.append(("K10", len(candidates) <= 1, f"{len(candidates)} candidate(s) for one story (duplicate workstream if > 1)"))
    return checks


def economics_checks(tasks: Sequence[Dict[str, Any]], txs: Sequence[Dict[str, Any]], wallets: Sequence[Dict[str, Any]], inflight: Dict[str, int]) -> List[Tuple[str, bool, str]]:
    """Escrow lifecycle invariants for society tasks (Phase 5 §26): one payment
    transaction per task, its status follows the task, reserved credits equal
    the in-flight escrow, no negative money."""
    by_task: Dict[str, List[Dict[str, Any]]] = {}
    for t in txs:
        by_task.setdefault(str(t["task_session_id"]), []).append(t)
    bad_count, bad_state = [], []
    for task in tasks:
        tid = str(task["id"])
        pays = [t for t in by_task.get(tid, []) if t["type"] == "payment"]
        if len(pays) != 1:
            bad_count.append(f"{tid[:8]}:{len(pays)} payment tx")
            continue
        pay = pays[0]
        status = task["status"]
        want = {"initiated": "pending", "in_progress": "pending", "completed": "completed", "refunded": "cancelled", "timeout": "cancelled", "failed": "cancelled"}.get(status)
        if want is not None and pay["status"] != want:
            bad_state.append(f"{tid[:8]}:{status}/{pay['status']}")
        if int(pay["amount"]) != int(task["escrow_amount"]):
            bad_state.append(f"{tid[:8]}:amount {pay['amount']}!={task['escrow_amount']}")
    checks = [
        ("E01", not bad_count, f"{len(tasks)} society task(s), one payment transaction each" if not bad_count else "; ".join(bad_count[:6])),
        ("E02", not bad_state, "payment status follows the task state" if not bad_state else "; ".join(bad_state[:6])),
    ]
    bad_wallet = []
    for w in wallets:
        reserved = int(w["reserved_credits"])
        balance = int(w["balance_credits"])
        expected = int(inflight.get(str(w["wallet_id"]), 0))
        if reserved != expected or reserved < 0 or balance < reserved:
            bad_wallet.append(f"{w['agent']}: balance={balance} reserved={reserved} inflight={expected}")
    checks.append(("E03", not bad_wallet, "reserved == in-flight escrow and balance >= reserved for every society wallet" if not bad_wallet else "; ".join(bad_wallet)))
    return checks


# ── output ─────────────────────────────────────────────────────────────


def _w(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class Out:
    def __init__(self) -> None:
        self.failed: List[str] = []
        self.count = 0

    def check(self, step: str, code: str, ok: bool, detail: str = "") -> bool:
        self.count += 1
        if not ok:
            self.failed.append(f"{step}.{code}")
        _w(f"PHASE5 {step} {code} {'PASS' if ok else 'FAIL'} {scrub(detail)[:400]}")
        return ok

    def info(self, step: str, detail: str) -> None:
        _w(f"PHASE5 {step} INFO {scrub(detail)[:800]}")

    def json(self, step: str, key: str, obj: Any) -> None:
        for line in json_lines(f"{step}.{key}", obj):
            _w(line)


def api(method: str, url: str, token: Optional[str], body: Optional[dict] = None, timeout: float = 30.0) -> Tuple[int, Any]:
    st, text = vs.http(method, url, body=body, token=token, timeout=timeout)
    try:
        return st, json.loads(text) if text else None
    except ValueError:
        return st, text[:300]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── database helpers (psycopg2, parameterised) ─────────────────────────


def _rows(cur, sql: str, params: Tuple = ()) -> List[Tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


def _kv(rows: Sequence[Tuple]) -> Dict[str, Any]:
    return {str(r[0]): (r[1] if len(r) == 2 else list(r[1:])) for r in rows}


def snapshot(conn, since: Optional[datetime]) -> Dict[str, Any]:
    """Counts-only view of the society tables (optionally since a timestamp)."""
    w = "WHERE created_at >= %s" if since else ""
    p: Tuple = (since,) if since else ()
    with conn.cursor() as cur:
        snap: Dict[str, Any] = {"since": since.isoformat() if since else None, "taken_at": _now().isoformat()}
        snap["events_by_status"] = _kv(_rows(cur, f"SELECT status, count(*) FROM society_events {w} GROUP BY 1", p))
        snap["events_by_type"] = _kv(_rows(cur, f"SELECT event_type, count(*) FROM society_events {w} GROUP BY 1 ORDER BY 2 DESC LIMIT 40", p))
        snap["loop_breaker_tripped"] = _rows(cur, f"SELECT count(*) FROM society_events {w + (' AND' if w else 'WHERE')} event_type = 'loop_breaker.tripped'", p)[0][0]
        snap["loop_breaker_suppressed"] = _rows(cur, f"SELECT count(*) FROM society_events {w + (' AND' if w else 'WHERE')} dispatch_note LIKE 'loop breaker%%'", p)[0][0]
        snap["runs_by_status"] = _kv(_rows(cur, f"SELECT status, count(*) FROM agent_runs {w} GROUP BY 1", p))
        snap["runs_by_role"] = [list(r) for r in _rows(cur, f"SELECT role, status, count(*) FROM agent_runs {w} GROUP BY 1, 2 ORDER BY 1, 2", p)]
        snap["runs_by_model"] = [list(r) for r in _rows(cur, f"SELECT model_provider, model_name, count(*), coalesce(sum(tokens_in),0), coalesce(sum(tokens_out),0), coalesce(sum(cost_usd),0), coalesce(sum(model_requests),0), coalesce(sum(model_retries),0), coalesce(sum(model_timeouts),0) FROM agent_runs {w} GROUP BY 1, 2", p)]
        snap["dead_runs"] = [[r[0], scrub(r[1] or "")[:120]] for r in _rows(cur, f"SELECT role, error FROM agent_runs {w + (' AND' if w else 'WHERE')} status = 'dead' ORDER BY created_at DESC LIMIT 20", p)]
        snap["skipped_reasons"] = [[scrub(r[0] or "")[:70], r[1]] for r in _rows(cur, f"SELECT left(error, 70), count(*) FROM agent_runs {w + (' AND' if w else 'WHERE')} status = 'skipped' GROUP BY 1 ORDER BY 2 DESC LIMIT 12", p)]
        snap["intents_by_policy"] = _kv(_rows(cur, f"SELECT policy_decision, count(*) FROM agent_intents {w} GROUP BY 1", p))
        snap["intents_by_execution"] = _kv(_rows(cur, f"SELECT execution_status, count(*) FROM agent_intents {w} GROUP BY 1", p))
        snap["intents_by_type"] = _kv(_rows(cur, f"SELECT intent_type, count(*) FROM agent_intents {w} GROUP BY 1 ORDER BY 2 DESC LIMIT 40", p))
        snap["high_risk_intents"] = [list(r) for r in _rows(cur, f"SELECT intent_type, policy_decision, execution_status, count(*) FROM agent_intents {w + (' AND' if w else 'WHERE')} risk_class = 'high' GROUP BY 1, 2, 3", p)]
        snap["forbidden_high_executed"] = _rows(cur, "SELECT count(*) FROM agent_intents WHERE intent_type = ANY(%s) AND execution_status = 'executed'", (list(FORBIDDEN_HIGH),))[0][0]
        snap["approvals"] = [list(r) for r in _rows(cur, f"SELECT decision, final_state, count(*) FROM intent_approvals {w} GROUP BY 1, 2", p)]
        snap["candidates_by_status"] = _kv(_rows(cur, f"SELECT status, count(*) FROM code_candidates {w} GROUP BY 1", p))
        snap["candidates_with_workspace"] = _rows(cur, f"SELECT count(*) FROM code_candidates {w + (' AND' if w else 'WHERE')} workspace_path IS NOT NULL", p)[0][0]
        snap["correlations_with_multiple_candidates"] = _rows(cur, f"SELECT count(*) FROM (SELECT correlation_id FROM code_candidates {w} GROUP BY 1 HAVING count(*) > 1) d", p)[0][0]
        snap["promotions"] = _rows(cur, f"SELECT count(*) FROM code_promotions {w}", p)[0][0]
        snap["deployment_requests"] = _rows(cur, f"SELECT count(*) FROM deployment_requests {w}", p)[0][0]
        snap["change_experiments"] = _rows(cur, f"SELECT count(*) FROM change_experiments {w}", p)[0][0]
        snap["goals_by_status"] = _kv(_rows(cur, f"SELECT status, count(*) FROM goals {w} GROUP BY 1", p))
        snap["proposals_by_status"] = _kv(_rows(cur, f"SELECT status, count(*) FROM improvement_proposals {w} GROUP BY 1", p))
        snap["proposals_duplicate_titles"] = _rows(cur, f"SELECT count(*) FROM (SELECT lower(title) FROM improvement_proposals {w} GROUP BY 1 HAVING count(*) > 1) d", p)[0][0]
        snap["memory_items"] = _rows(cur, f"SELECT count(*) FROM memory_items {w}", p)[0][0]
        snap["agent_chat"] = _rows(cur, f"SELECT count(*) FROM agent_chat {w}", p)[0][0]
        day = _rows(cur, "SELECT coalesce(sum(cost_usd),0), coalesce(sum(tokens_in),0), coalesce(sum(tokens_out),0), count(*) FROM agent_runs WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'")[0]
        snap["today"] = {"cost_usd": str(day[0]), "tokens_in": int(day[1]), "tokens_out": int(day[2]), "runs": int(day[3])}
        snap["ingress_last_hour_by_actor"] = [r[0] for r in _rows(cur, "SELECT count(*) FROM society_events WHERE actor_type = 'user' AND created_at >= now() - interval '1 hour' GROUP BY actor_id ORDER BY 1 DESC")]
        snap["grants"] = [
            {"role": r[0], "enabled": r[1], "paused": bool(r[2]), "approval_required_intents": r[3], "consecutive_failures": r[4], "max_task_escrow_credits": r[5], "daily_model_budget_usd": str(r[6]), "wake_cooldown_seconds": r[7], "max_runs_per_hour": r[8]}
            for r in _rows(cur, "SELECT role, enabled, paused_until > now(), approval_required_intents, consecutive_failures, max_task_escrow_credits, daily_model_budget_usd, wake_cooldown_seconds, max_runs_per_hour FROM agent_capability_grants ORDER BY role")
        ]
        snap["wallets"] = society_wallets(cur)
        snap["tasks_by_status"] = _kv(_rows(cur, f"SELECT t.status, count(*) FROM task_sessions t WHERE t.caller_agent_id IN (SELECT agent_id FROM agent_capability_grants) {('AND t.created_at >= %s' if since else '')} GROUP BY 1", p))
    return snap


def society_wallets(cur) -> List[Dict[str, Any]]:
    return [
        {"agent": r[0], "wallet_id": str(r[1]), "balance_credits": int(r[2]), "reserved_credits": int(r[3]), "spending_cap": int(r[4]), "daily_spent": int(r[5])}
        for r in _rows(cur, "SELECT a.name, w.id, w.balance_credits, w.reserved_credits, w.spending_cap, w.daily_spent FROM wallets w JOIN agents a ON a.id = w.owner_id JOIN agent_capability_grants g ON g.agent_id = a.id WHERE w.owner_type = 'agent' ORDER BY a.name")
    ]


def society_tasks(cur, since: Optional[datetime]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    w = "AND t.created_at >= %s" if since else ""
    p: Tuple = (since,) if since else ()
    tasks = [
        {"id": str(r[0]), "caller": r[1], "callee": r[2], "status": r[3], "escrow_amount": int(r[4]), "created_at": r[5], "completed_at": r[6], "refund_at": r[7]}
        for r in _rows(cur, f"SELECT t.id, ca.name, ce.name, t.status, t.escrow_amount, t.created_at, t.completed_at, t.refund_at FROM task_sessions t JOIN agents ca ON ca.id = t.caller_agent_id JOIN agents ce ON ce.id = t.callee_agent_id WHERE t.caller_agent_id IN (SELECT agent_id FROM agent_capability_grants) {w} ORDER BY t.created_at", p)
    ]
    ids = [t["id"] for t in tasks]
    txs = [
        {"task_session_id": str(r[0]), "type": r[1], "status": r[2], "amount": int(r[3]), "platform_fee": int(r[4] or 0)}
        for r in (_rows(cur, "SELECT task_session_id, type, status, amount, platform_fee FROM transactions WHERE task_session_id::text = ANY(%s) ORDER BY created_at", (ids,)) if ids else [])
    ]
    inflight = {str(r[0]): int(r[1]) for r in _rows(cur, "SELECT w.id, coalesce(sum(t.escrow_amount), 0) FROM wallets w JOIN task_sessions t ON t.caller_agent_id = w.owner_id AND w.owner_type = 'agent' WHERE t.status IN ('initiated', 'in_progress') GROUP BY w.id")}
    return tasks, txs, inflight


def secret_scan(cur, since: Optional[datetime]) -> Dict[str, Any]:
    """Counts of secret-/token-/chain-of-thought-shaped text in runtime-produced rows (never the text itself)."""
    out: Dict[str, Any] = {}
    for table in SCAN_TABLES:
        w = "WHERE created_at >= %s" if since else ""
        p: Tuple = (SQL_KEY_RE, SQL_JWT_RE, SQL_COT_RE) + ((since,) if since else ())
        r = _rows(cur, f"SELECT count(*) FILTER (WHERE row_to_json(t)::text ~ %s), count(*) FILTER (WHERE row_to_json(t)::text ~ %s), count(*) FILTER (WHERE row_to_json(t)::text ~* %s), count(*) FROM {table} t {w}", p)[0]
        out[table] = {"key_shaped": int(r[0]), "jwt_shaped": int(r[1]), "cot_markers": int(r[2]), "rows": int(r[3])}
    # World events split by actor: user-injected payloads are untrusted DATA (a red-team probe may
    # legitimately carry a fake key); anything the runtime itself wrote must be clean.
    for actor, label in (("user", "society_events_from_users"), ("system", "society_events_from_runtime")):
        cond = "actor_type = 'user'" if actor == "user" else "actor_type <> 'user'"
        w = "AND created_at >= %s" if since else ""
        p = (SQL_KEY_RE, SQL_JWT_RE, SQL_COT_RE) + ((since,) if since else ())
        r = _rows(cur, f"SELECT count(*) FILTER (WHERE row_to_json(t)::text ~ %s), count(*) FILTER (WHERE row_to_json(t)::text ~ %s), count(*) FILTER (WHERE row_to_json(t)::text ~* %s), count(*) FROM society_events t WHERE {cond} {w}", p)[0]
        out[label] = {"key_shaped": int(r[0]), "jwt_shaped": int(r[1]), "cot_markers": int(r[2]), "rows": int(r[3])}
    return out


# ── steps ──────────────────────────────────────────────────────────────


def step_status(out: Out, base: str, token: Optional[str]) -> None:
    st, body = api("GET", f"{base}/v1/society/status", None)
    ok = st == 200 and isinstance(body, dict)
    out.check("status", "S01", ok, f"public /v1/society/status HTTP {st}")
    if ok:
        out.json("status", "public", {k: body.get(k) for k in PUBLIC_FLAG_KEYS + ("fleet", "fleet_size", "pending_events", "queued_runs", "active_runs", "runs_last_hour", "runs_today", "intents_awaiting_approval", "candidates_by_status", "last_run_completed_at")})
    st, cfg = api("GET", f"{base}/v1/society/config", token)
    ok = st == 200 and isinstance(cfg, dict)
    out.check("status", "S02", ok, f"operator /v1/society/config HTTP {st}")
    if ok:
        settings = cfg.get("settings") or {}
        out.json("status", "config", {k: v for k, v in settings.items() if SAFE_CONFIG_RE.match(k)})
        out.check("status", "S03", "model_api_key" not in json.dumps({k: v for k, v in settings.items() if SAFE_CONFIG_RE.match(k)}), "no credential field in the printed subset")


def step_baseline(out: Out, conn) -> None:
    snap = snapshot(conn, None)
    out.json("baseline", "snapshot", snap)
    out.check("baseline", "B01", True, f"events={sum(snap['events_by_status'].values())} runs={sum(snap['runs_by_status'].values())} candidates={sum(snap['candidates_by_status'].values())} cost_today_usd={snap['today']['cost_usd']}")
    out.check("baseline", "B02", snap["forbidden_high_executed"] == 0, f"forbidden HIGH intents ever executed: {snap['forbidden_high_executed']}")


def step_fund(out: Out, conn, credits: int, seq: int, operator_email: str) -> None:
    agent = vs.env("PHASE5_FUND_AGENT", "Society_Architect")
    key = fund_idempotency_key(agent, seq)
    with conn.cursor() as cur:
        row = _rows(cur, "SELECT w.id, w.balance_credits, w.reserved_credits, w.spending_cap FROM wallets w JOIN agents a ON a.id = w.owner_id WHERE w.owner_type = 'agent' AND a.name = %s", (agent,))
        if not row:
            out.check("fund", "F01", False, f"no agent wallet for {agent}")
            return
        wallet_id, before, reserved, cap = str(row[0][0]), int(row[0][1]), int(row[0][2]), int(row[0][3])
        existing = _rows(cur, "SELECT id, status FROM transactions WHERE idempotency_key = %s", (key,))
        if existing:
            out.info("fund", f"deposit {key} already exists with status {existing[0][1]}; not repeated")
            out.check("fund", "F01", existing[0][1] == "completed", f"{agent} wallet balance={before} reserved={reserved} cap={cap} (idempotent replay)")
            return
        tx_id = str(uuid.uuid4())
        extra = json.dumps({"operator_action": "phase5_fund", "operator": operator_email.split("@")[0], "purpose": "Phase 5 live escrow proof (docs/SOCIETY_LIVE_MODEL_RUNBOOK.md)", "seq": seq})
        cur.execute(
            "INSERT INTO transactions (id, from_wallet, to_wallet, amount, currency, status, type, platform_fee, platform_fee_rate, extra_data, idempotency_key, created_at) "
            "VALUES (%s, NULL, %s, %s, 'credits', 'pending', 'deposit', 0, 0, %s, %s, now())",
            (tx_id, wallet_id, credits, extra, key),
        )
        conn.commit()
        # The wallet trigger (BEFORE UPDATE … WHEN status -> completed) is the ONLY writer of balances.
        cur.execute("UPDATE transactions SET status = 'completed', completed_at = now() WHERE id = %s AND status = 'pending'", (tx_id,))
        n = cur.rowcount
        conn.commit()
        after = int(_rows(cur, "SELECT balance_credits FROM wallets WHERE id = %s", (wallet_id,))[0][0])
    out.check("fund", "F01", n == 1 and after == before + credits, f"{agent}: balance {before} -> {after} via ledger deposit {credits} (transaction completed, trigger credited)")
    out.json("fund", "result", {"agent": agent, "credits": credits, "idempotency_key": key, "balance_before": before, "balance_after": after, "reserved_credits": reserved, "spending_cap": cap})


def step_gate(out: Out, conn, role: str, intent: str) -> None:
    with conn.cursor() as cur:
        row = _rows(cur, "SELECT id, allowed_intents, approval_required_intents FROM agent_capability_grants WHERE role = %s", (role.lower(),))
        if not row:
            out.check("gate", "G01", False, f"no grant for role {role!r}")
            return
        gid, allowed, current = row[0]
        try:
            new = gate_list(list(allowed or []), list(current or []), intent)
        except ValueError as exc:
            out.check("gate", "G01", False, str(exc))
            return
        cur.execute("UPDATE agent_capability_grants SET approval_required_intents = %s::jsonb, updated_at = now() WHERE id = %s", (json.dumps(new), gid))
        conn.commit()
        final = _rows(cur, "SELECT approval_required_intents FROM agent_capability_grants WHERE id = %s", (gid,))[0][0]
    out.check("gate", "G01", list(final or []) == new, f"role={role.lower()} approval_required_intents={final}")


def _decisions(base: str, token: str, correlation: str) -> List[Dict[str, Any]]:
    st, detail = api("GET", f"{base}/v1/society/story/{correlation}/detail", token)
    if st != 200 or not isinstance(detail, dict):
        return []
    return [{"role": r.get("role"), "status": r.get("status"), "model": r.get("model_name"), "summary": (r.get("decision_summary") or "")[:240], "intents": [i.get("intent_type") for i in r.get("intents") or []]} for r in detail.get("runs") or []]


def step_canary(out: Out, base: str, token: str, scenario: str, decide: Optional[str], timeout: float) -> None:
    sys.path.insert(0, str(REPO / "services" / "registry"))
    from app.society.canary import CanaryRefused, observe_canary  # the runtime's own canary (HTTP-only)

    try:
        rep = observe_canary(base, token, scenario=scenario, decide=decide, timeout_seconds=timeout, poll_seconds=5.0)
    except CanaryRefused as exc:
        out.check("canary", scenario, False, f"refused: {exc}")
        return
    d = rep.to_dict()
    out.json("canary", f"{scenario}.report", {k: d.get(k) for k in ("scenario", "verdict", "reasons", "provider", "model_name", "correlation_id", "totals", "started_at", "finished_at")})
    out.json("canary", f"{scenario}.runs", d.get("runs"))
    out.json("canary", f"{scenario}.intents", d.get("intents"))
    out.json("canary", f"{scenario}.events", d.get("events"))
    out.json("canary", f"{scenario}.approvals", d.get("approvals"))
    out.json("canary", f"{scenario}.decisions", _decisions(base, token, rep.correlation_id))
    out.check("canary", scenario + (f":{decide}" if decide else ""), rep.verdict == "PASS", f"verdict={rep.verdict} model={rep.model_name} runs={d.get('totals', {}).get('runs_completed')}/{d.get('totals', {}).get('runs')} reasons={rep.reasons}")


def step_signal(out: Out, base: str, token: str, *, expect_candidate: bool, timeout: float, decide: Optional[str]) -> None:
    etype = vs.env("PHASE5_SIGNAL_TYPE")
    raw = os.getenv("PHASE5_SIGNAL_JSON", "").strip()
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = None
    if not etype or not isinstance(payload, dict):
        out.check("signal", "I01", False, "PHASE5_SIGNAL_TYPE and a json-object PHASE5_SIGNAL_JSON are required")
        return
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        out.check("signal", "I01", False, f"payload {size} bytes exceeds {MAX_PAYLOAD_BYTES}")
        return
    st, status_body = api("GET", f"{base}/v1/society/status", None)
    if st != 200 or not isinstance(status_body, dict) or not status_body.get("runtime_enabled") or status_body.get("model_provider") != LIVE_PROVIDER or status_body.get("production_deploy_enabled") is not False:
        out.check("signal", "I01", False, f"runtime not in the expected live state (HTTP {st}, flags={ {k: (status_body or {}).get(k) for k in PUBLIC_FLAG_KEYS} if isinstance(status_body, dict) else None})")
        return
    if expect_candidate and not status_body.get("autonomous_code_enabled"):
        out.check("signal", "I01", False, "autonomous code loop is disabled; a candidate cannot be expected")
        return
    tag = uuid.uuid4().hex[:8]
    correlation = str(uuid.uuid4())
    st, ev = api("POST", f"{base}/v1/society/events", token, {"event_type": etype, "payload": payload, "correlation_id": correlation, "idempotency_key": f"phase5-signal-{tag}"})
    out.check("signal", "I01", st == 201 and isinstance(ev, dict) and ev.get("duplicate") is False, f"world event {etype} injected HTTP {st} correlation={correlation} bytes={size}")
    if st != 201:
        return
    out.json("signal", "event", {"event_type": etype, "correlation_id": correlation, "payload_keys": sorted(payload.keys()), "bytes": size, "tag": tag})

    deadline = time.monotonic() + timeout
    idle = 0
    decided: set = set()
    detail: Dict[str, Any] = {}
    polls = 0
    while time.monotonic() < deadline:
        st, detail = api("GET", f"{base}/v1/society/story/{correlation}/detail", token)
        polls += 1
        if st != 200 or not isinstance(detail, dict):
            out.check("signal", "I02", False, f"story detail HTTP {st}")
            return
        runs = detail.get("runs") or []
        events = detail.get("events") or []
        candidates = detail.get("candidates") or []
        if decide:
            for r in runs:
                for i in r.get("intents") or []:
                    if i.get("execution_status") == "awaiting_approval" and i["id"] not in decided:
                        s, _ = api("POST", f"{base}/v1/society/intents/{i['id']}/{decide}", token, {"reason": f"phase5 signal ({decide})"})
                        out.info("signal", f"{decide} on parked intent {i['id'][:8]} ({i.get('intent_type')}) -> HTTP {s}")
                        decided.add(i["id"])
        active = any(r.get("status") in ACTIVE_RUN for r in runs) or any(e.get("status") in ACTIVE_EVENT for e in events)
        active = active or any(c.get("status") in ("requested", "building", "built", "qa_running", "security_review") for c in candidates)
        parked = any(i.get("execution_status") == "awaiting_approval" for r in runs for i in r.get("intents") or [])
        if active:
            idle = 0
        elif runs or (parked and not decide):
            idle += 1
            if idle >= 3:
                break
        if polls % 12 == 0:
            out.info("signal", f"t+{int(timeout - (deadline - time.monotonic()))}s events={len(events)} runs={[(r.get('role'), r.get('status')) for r in runs][-6:]} candidates={[(c.get('id', '')[:8], c.get('status')) for c in candidates]}")
        time.sleep(5.0)
    else:
        out.info("signal", f"timed out after {int(timeout)}s waiting for the story to go idle")

    runs = detail.get("runs") or []
    out.json("signal", "events", [{k: e.get(k) for k in ("event_type", "causation_depth", "status", "created_at")} for e in detail.get("events") or []])
    out.json("signal", "runs", [{k: r.get(k) for k in ("id", "role", "event_type", "status", "attempt", "model_provider", "model_name", "tokens_in", "tokens_out", "cost_usd", "model_requests", "model_retries", "model_timeouts", "intents_count")} | {"error": scrub(str(r.get("error")))[:160] if r.get("error") else None} for r in runs])
    out.json("signal", "intents", [{"run": r.get("id", "")[:8], "role": r.get("role"), "seq": i.get("seq"), "type": i.get("intent_type"), "risk": i.get("risk_class"), "policy": i.get("policy_decision"), "execution": i.get("execution_status"), "reason": (i.get("policy_reason") or "")[:120], "payload_keys": sorted((i.get("payload") or {}).keys()) if isinstance(i.get("payload"), dict) else None, "approval": (i.get("approval") or {}).get("decision")} for r in runs for i in r.get("intents") or []])
    out.json("signal", "decisions", [{"role": r.get("role"), "status": r.get("status"), "summary": (r.get("decision_summary") or "")[:240]} for r in runs])
    candidates = detail.get("candidates") or []
    out.json("signal", "candidates", candidates)
    full: List[Dict[str, Any]] = []
    for c in candidates:
        st, cf = api("GET", f"{base}/v1/society/candidates/{c.get('id')}", token)
        if st == 200 and isinstance(cf, dict):
            full.append(cf)
            spec = cf.get("spec") or {}
            qa = cf.get("qa_report") or {}
            sec = cf.get("security_report") or {}
            out.json("signal", f"candidate.{str(cf.get('id'))[:8]}", {
                "status": cf.get("status"), "title": cf.get("title"), "kind": spec.get("kind"), "files_allowed": spec.get("files_allowed"), "acceptance_tests": spec.get("acceptance_tests"),
                "must_compile": spec.get("must_compile"), "changed_files": cf.get("changed_files"), "diff_stat": cf.get("diff_stat"), "branch_name": cf.get("branch_name"),
                "has_workspace": bool(cf.get("workspace_path")), "base_sha": (cf.get("base_sha") or "")[:12], "head_sha": (cf.get("head_sha") or "")[:12], "task_id": cf.get("task_id"), "proposal_id": cf.get("proposal_id"),
                "qa": {"verdict": qa.get("verdict"), "attempts": qa.get("attempts"), "checks": [{"name": ch.get("name"), "passed": ch.get("passed"), "detail": scrub(str(ch.get("detail") or ""))[:160]} for ch in qa.get("checks") or []], "failures": [scrub(f)[:160] for f in qa.get("failures") or []][:6]},
                "security": {"verdict": sec.get("verdict"), "findings": [scrub(f)[:120] for f in sec.get("findings") or []][:10], "static_findings": [scrub(f)[:120] for f in sec.get("static_findings") or []][:10]},
                "requires_security_review": cf.get("requires_security_review"), "error": scrub(str(cf.get("error")))[:160] if cf.get("error") else None,
            })
    for code, ok, note in chain_checks(detail, full, expect_candidate=expect_candidate):
        out.check("signal", code, ok, note)
    out.json("signal", "story", {"correlation_id": correlation, "runs": len(runs), "events": len(detail.get("events") or []), "candidates": [c.get("id") for c in candidates], "decided": sorted(decided)})


def step_audit(out: Out, base: str, token: str, conn, hours: int) -> None:
    since = _now() - timedelta(hours=hours)
    snap = snapshot(conn, since)
    out.json("audit", "snapshot", snap)
    out.check("audit", "L01", int(snap["loop_breaker_tripped"]) == 0 and int(snap["loop_breaker_suppressed"]) == 0, f"loop breaker tripped={snap['loop_breaker_tripped']} suppressed={snap['loop_breaker_suppressed']} in the last {hours}h")
    dead = int(snap["runs_by_status"].get("dead", 0))
    out.check("audit", "L02", dead == 0, f"DEAD runs in the last {hours}h: {dead} {snap['dead_runs'][:3]}")
    out.check("audit", "L03", int(snap["forbidden_high_executed"]) == 0, f"forbidden HIGH intents ever executed: {snap['forbidden_high_executed']}; high-risk rows={snap['high_risk_intents']}")
    out.check("audit", "L04", int(snap["correlations_with_multiple_candidates"]) == 0, f"correlations with >1 candidate: {snap['correlations_with_multiple_candidates']}; duplicate proposal titles: {snap['proposals_duplicate_titles']}")
    fake = [m for m in snap["runs_by_model"] if m[0] not in (None, LIVE_PROVIDER) and int(m[2]) > 0]
    out.check("audit", "L05", not fake, f"runs by provider/model: {[(m[0], m[1], m[2]) for m in snap['runs_by_model']]}")
    with conn.cursor() as cur:
        tasks, txs, inflight = society_tasks(cur, since)
        wallets = society_wallets(cur)
        scan = secret_scan(cur, since)
    out.json("audit", "tasks", [{k: t[k] for k in ("id", "caller", "callee", "status", "escrow_amount", "created_at", "completed_at", "refund_at")} for t in tasks])
    out.json("audit", "transactions", txs)
    out.json("audit", "wallets", wallets)
    for code, ok, note in economics_checks(tasks, txs, wallets, inflight):
        out.check("audit", code, ok, note)
    out.json("audit", "secret_scan", scan)
    runtime_keys = sum(v["key_shaped"] for k, v in scan.items() if k != "society_events_from_users")
    runtime_jwt = sum(v["jwt_shaped"] for k, v in scan.items() if k != "society_events_from_users")
    cot = sum(v["cot_markers"] for v in scan.values())
    out.check("audit", "X01", runtime_keys == 0, f"secret-shaped strings in runtime-produced rows: {runtime_keys} (user-injected payloads: {scan['society_events_from_users']['key_shaped']}, untrusted data)")
    out.check("audit", "X02", cot == 0, f"chain-of-thought markers stored: {cot}")
    out.check("audit", "X03", runtime_jwt == 0, f"token-shaped strings in runtime-produced rows: {runtime_jwt}")
    st, budget = api("GET", f"{base}/v1/society/budget", token)
    if st == 200 and isinstance(budget, dict):
        out.json("audit", "budget", {k: budget.get(k) for k in ("model_spend_today_usd", "daily_model_budget_usd", "remaining_usd", "tokens_in_today", "tokens_out_today", "by_agent", "max_task_escrow_credits")})
        try:
            spend, cap = float(budget.get("model_spend_today_usd") or 0), float(budget.get("daily_model_budget_usd") or 0)
        except ValueError:
            spend, cap = 0.0, 0.0
        out.check("audit", "C01", cap > 0 and spend < cap, f"model spend today {spend:.4f} USD of {cap:.2f} USD budget")
    else:
        out.check("audit", "C01", False, f"operator /budget HTTP {st}")
    # public surface: structural only, no private keys, operator surfaces closed
    for code, path in (("P01", "/v1/society/status"), ("P02", "/v1/society/metrics")):
        st, body = api("GET", f"{base}{path}", None)
        leaked = private_keys_in(body) if isinstance(body, (dict, list)) else []
        raw = json.dumps(body, default=str) if body is not None else ""
        out.check("audit", code, st == 200 and not leaked and not KEY_SHAPE.search(raw) and not JWT_SHAPE.search(raw), f"public {path} HTTP {st} private_keys={leaked}")
    with conn.cursor() as cur:
        cids = [str(r[0]) for r in _rows(cur, "SELECT DISTINCT correlation_id FROM agent_runs WHERE created_at >= %s ORDER BY 1 LIMIT 5", (since,))]
    bad = []
    for cid in cids:
        st, body = api("GET", f"{base}/v1/society/story/{cid}", None)
        leaked = private_keys_in(body) if isinstance(body, (dict, list)) else []
        raw = json.dumps(body, default=str) if body is not None else ""
        if st != 200 or leaked or KEY_SHAPE.search(raw) or JWT_SHAPE.search(raw):
            bad.append(f"{cid[:8]}:HTTP {st} {leaked}")
    out.check("audit", "P03", not bad, f"public story for {len(cids)} recent correlation(s) structural" if not bad else "; ".join(bad))
    closed = []
    for path in ("/v1/society/config", "/v1/society/budget", "/v1/society/approvals", "/v1/society/runs", "/v1/society/intents", "/v1/society/events") + tuple(f"/v1/society/story/{c}/detail" for c in cids[:1]):
        st, _ = api("GET", f"{base}{path}", None)
        if st not in (401, 403):
            closed.append(f"{path}:{st}")
    out.check("audit", "P04", not closed, "operator surfaces refuse anonymous callers" if not closed else "; ".join(closed))


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    out = Out()
    base = vs.env("REGISTRY_PUBLIC_URL").rstrip("/")
    secret = vs.env("STAGING_VALIDATOR_SECRET")
    op_email = vs.env("PHASE5_OPERATOR_EMAIL") or vs.env("VALIDATOR_OPERATOR_EMAIL", "staging-operator@staging.agentnet.io.vn")
    try:
        plan = parse_plan(vs.env("PHASE5_PLAN"))
    except ValueError as exc:
        out.check("plan", "V00", False, str(exc))
        return finish(out)
    _w(f"PHASE5 start deployment={vs.env('RAILWAY_DEPLOYMENT_ID', '?')[:8]} commit={vs.env('RAILWAY_GIT_COMMIT_SHA', '?')[:12]} operator={op_email.split('@')[0]} plan={[n + (':' + ':'.join(a) if a else '') for n, a in plan]}")
    if not base or not secret:
        out.check("plan", "V00", False, "REGISTRY_PUBLIC_URL and STAGING_VALIDATOR_SECRET are required")
        return finish(out)
    rep = vs.Report()
    token = vs.ensure_user(rep, "O01", base, op_email, vs.derive_password(secret))
    out.check("plan", "V01", bool(token), f"operator {op_email.split('@')[0]} logged in (token in memory only)")
    if not token:
        return finish(out)
    timeout = float(vs.env("PHASE5_TIMEOUT", "900") or 900)
    decide = vs.env("PHASE5_DECIDE") or None
    conn = None
    for name, args in plan:
        try:
            if name in ("baseline", "fund", "gate", "audit") and conn is None:
                conn = vs.db_connect()
            if name == "status":
                step_status(out, base, token)
            elif name == "baseline":
                step_baseline(out, conn)
            elif name == "fund":
                step_fund(out, conn, int(args[0]), int(args[1]) if len(args) > 1 else 1, op_email)
            elif name == "gate":
                step_gate(out, conn, args[0], args[1])
            elif name == "canary":
                step_canary(out, base, token, args[0], args[1] if len(args) > 1 else None, timeout)
            elif name == "signal":
                step_signal(out, base, token, expect_candidate=bool(args), timeout=float(vs.env("PHASE5_TIMEOUT", "1800") or 1800), decide=decide if decide in DECISIONS else None)
            elif name == "audit":
                step_audit(out, base, token, conn, int(args[0]) if args else 24)
        except Exception as exc:  # noqa: BLE001 — record, never abort the remaining evidence
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            out.check(name, "EXC", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    if conn is not None:
        conn.close()
    return finish(out)


def finish(out: Out) -> int:
    if out.failed:
        _w(f"PHASE5 RESULT: FAILED {','.join(out.failed)} ({out.count} checks)")
        return 1
    _w(f"PHASE5 RESULT: OK ({out.count} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
