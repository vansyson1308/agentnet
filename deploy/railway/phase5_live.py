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
    intents:<correlation-id>      per-intent policy/validation reasons + decision summaries of one story (diagnosis)
    journey[:<hours>]             the core-journey probe's maintenance_observations (sli=core_journey): count, success, duration (default 24 h)
    autonomy[:<days>]             does the company run without the owner? plans decided by the board vs the owner and how long they
                                  waited, design reviews and their conversation, growth tickets, merged work, the federated network (default 7 d)
    costs[:<days>[:<budget>]]     model spend p50/p95 per role+model (decide runs), per build, per maintenance activity, per day;
                                  the caps it implies inside <budget> USD/day (default 7 d, 6.0) -- a recommendation, never applied
    budget:<role>:<usd>           operator command: that role's grant daily_model_budget_usd (a re-seed keeps the larger of it and the default)
    freezes:lift-recovered        lift (operator API) ONLY the open freezes that outlived their cause (maintenance incident recovered/closed,
                                  or public surface recovered >= 6 h ago and green now); others untouched

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

import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import validate_staging as vs  # noqa: E402  (same directory; stdlib + psycopg2 only)

STEPS = ("status", "baseline", "fund", "gate", "canary", "signal", "audit", "intents", "memory", "refute", "promotions", "candidates", "taskfail", "abandon", "company", "journey", "autonomy", "freezes", "costs", "budget", "routes", "owner")
SCENARIOS = ("single", "multi", "approval")
DECISIONS = ("approve", "reject")
MAX_LINE = 12000
MAX_PAYLOAD_BYTES = 8192
LIVE_PROVIDERS = ("openai_compatible", "anthropic")
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
        if name == "intents" and (len(args) != 1 or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", args[0])):
            raise ValueError("intents:<correlation-id>")
        if name == "memory" and (len(args) != 1 or not re.fullmatch(r"[a-z_]{2,32}", args[0])):
            raise ValueError("memory:<role>")
        if name == "refute" and (len(args) != 1 or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", args[0])):
            raise ValueError("refute takes exactly one memory item id")
        if name == "promotions" and (len(args) > 1 or (args and not args[0].isdigit())):
            raise ValueError("promotions[:<limit>]")
        if name == "candidates" and (len(args) > 1 or (args and not args[0].isdigit())):
            raise ValueError("candidates[:<limit>]")
        if name == "abandon" and (len(args) != 1 or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", args[0])):
            raise ValueError("abandon takes exactly one candidate id")
        if name == "taskfail" and (len(args) > 1 or (args and not args[0].isdigit())):
            raise ValueError("taskfail[:<escrow-credits>]")
        if name == "journey" and (len(args) > 1 or (args and not args[0].isdigit())):
            raise ValueError("journey[:<hours>]")
        if name == "autonomy" and (len(args) > 1 or (args and not args[0].isdigit())):
            raise ValueError("autonomy[:<days>]")
        if name == "costs" and (len(args) > 2 or any(not re.fullmatch(r"\d{1,3}(\.\d{1,2})?", a) for a in args) or (args and not args[0].isdigit())):
            raise ValueError("costs[:<days>[:<daily-budget-usd>]]")
        if name == "budget" and (len(args) != 2 or not re.fullmatch(r"[a-z_]{2,32}", args[0]) or not re.fullmatch(r"\d{1,2}(\.\d{1,2})?", args[1]) or float(args[1]) <= 0):
            raise ValueError("budget:<role>:<usd> (0 < usd < 100, two decimals)")
        if name == "routes" and (len(args) > 1 or (args and not re.fullmatch(r"\d{1,3}", args[0]))):
            raise ValueError("routes[:<limit>]")
        if name == "owner" and args != ["slo"] and (len(args) != 2 or args[0] != "grant" or not OWNER_EMAIL_RE.fullmatch(args[1])):
            raise ValueError("owner:grant:<email> (an account that already registered on the dashboard) | owner:slo")
        if name == "freezes" and args != ["lift-recovered"]:
            raise ValueError("freezes:lift-recovered")
        if name == "company" and not _company_args_ok(args):
            raise ValueError("company[:view] | company:cycle | company:<activate|pause|propose|done>:<O1..O5> | company:<approve|reject>:<latest|plan-id>")
        plan.append((name, args))
    if not plan:
        raise ValueError("PHASE5_PLAN is empty")
    return plan


COMPANY_OBJ_STATUS = {"activate": "active", "pause": "paused", "propose": "proposed", "done": "done"}


def _company_args_ok(args: List[str]) -> bool:
    if not args or args == ["view"] or args == ["cycle"]:
        return True
    if len(args) == 2 and args[0] in COMPANY_OBJ_STATUS:
        return bool(re.fullmatch(r"O[1-5]", args[1]))
    if len(args) == 2 and args[0] in ("approve", "reject"):
        return args[1] == "latest" or bool(re.fullmatch(r"[0-9a-fA-F-]{32,36}", args[1]))
    return False


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


def seconds_to_wait(last_completed_iso: Optional[str], cooldown_seconds: int, now: datetime, margin_seconds: int = 5) -> int:
    """Seconds until the fleet's wake cooldown has elapsed since the last
    completed run (0 when no run or already elapsed). Canaries injected inside
    the cooldown would be deferred by the runtime, which only slows the story."""
    if not last_completed_iso:
        return 0
    try:
        last = datetime.fromisoformat(str(last_completed_iso).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0, int(cooldown_seconds + margin_seconds - (now - last).total_seconds()))


def intent_rows(detail: Dict[str, Any], *, reason_chars: int = 300) -> List[Dict[str, Any]]:
    """Structural per-intent rows of a story detail: type, risk, policy and
    execution decisions, the TRUSTED policy/validation reason and executor
    error (scrubbed, bounded) and the payload's key names — never its values."""
    rows: List[Dict[str, Any]] = []
    for r in detail.get("runs") or []:
        for i in r.get("intents") or []:
            payload = i.get("payload")
            rows.append(
                {
                    "run": str(r.get("id") or "")[:8],
                    "role": r.get("role"),
                    "seq": i.get("seq"),
                    "type": i.get("intent_type"),
                    "risk": i.get("risk_class"),
                    "policy": i.get("policy_decision"),
                    "execution": i.get("execution_status"),
                    "reason": scrub(str(i.get("policy_reason") or ""))[:reason_chars],
                    "error": scrub(str(i.get("error") or ""))[:reason_chars] or None,
                    "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
                    "approval": (i.get("approval") or {}).get("decision"),
                }
            )
    return rows


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
    fake = [r.get("id", "")[:8] for r in completed if r.get("model_provider") not in LIVE_PROVIDERS]
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
        # L01 names each trip: which story, why (depth vs runs), what was swallowed, where the story began, re-delivered?
        lw = "AND lb.created_at >= %s" if since else ""
        snap["loop_breaker_trips"] = [
            {"correlation": str(r[0])[:8], "note": scrub(r[1] or "")[:90], "swallowed": r[2], "depth": r[3], "runs": r[4], "root": r[5],
             "subject": f"{r[6]}:{str(r[7] or '')[:8]}", "redelivered": bool(r[8])}
            for r in _rows(cur, "SELECT lb.correlation_id, coalesce(lb.payload ->> 'note', lb.payload ->> 'reason'), sw.event_type, sw.causation_depth, "
                                "(SELECT count(*) FROM agent_runs a WHERE a.correlation_id = lb.correlation_id), "
                                "(SELECT e0.event_type FROM society_events e0 WHERE e0.correlation_id = lb.correlation_id ORDER BY e0.created_at LIMIT 1), "
                                "sw.subject_type, sw.subject_id, EXISTS (SELECT 1 FROM society_events rd WHERE rd.idempotency_key = 'redeliver:' || sw.id::text) "
                                f"FROM society_events lb LEFT JOIN society_events sw ON sw.id::text = lb.payload ->> 'event_id' "
                                f"WHERE lb.event_type = 'loop_breaker.tripped' {lw} ORDER BY lb.created_at DESC LIMIT 10", p)]
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


#: Verbatim copy of services/registry/app/society/canary.py COMPROMISED_CREDENTIAL_FINGERPRINTS (sha256 of
#: keys that leaked in git history; tests/society/test_secret_redaction.py asserts the two are identical).
COMPROMISED_CREDENTIAL_FINGERPRINTS = frozenset(
    {
        "7557e924b55eba39ac318b87ae34f1fffc435bfc92d207830c6801613b1fdcae",
        "0da0e5d60ebfe5529eca6a5ef6bab689d5884b57f92e5e8009628e60bd2cc781",
        "1ec5a2b2179f6f63cbab18b3195f6957224989e316f9eda470deeba995b01449",
    }
)
#: sha256 fingerprints (hex, comma-separated) of the credentials to check X01 matches against, e.g.
#: ``printf %s "$KEY" | sha256sum``. The validator never reads a credential itself, only fingerprints.
CREDENTIAL_FINGERPRINTS_ENV = "X01_CREDENTIAL_SHA256"


def _shape_paths(value: Any, field: str) -> List[Tuple[str, str, str]]:
    """(field path, pattern class, sha256) of every key-/JWT-shaped substring -- the text never leaves here."""
    if isinstance(value, str):
        return [(field, cls, hashlib.sha256(m.group(0).encode("utf-8")).hexdigest()) for cls, shape in (("key", KEY_SHAPE), ("jwt", JWT_SHAPE)) for m in shape.finditer(value)]
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in _shape_paths(v, f"{field}.{k}" if field else str(k))]
    if isinstance(value, list):
        return [x for i, v in enumerate(value) for x in _shape_paths(v, f"{field}[{i}]")]
    return []


def secret_locations(cur, since: Optional[datetime], env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Where the key-/JWT-shaped strings in ``agent_intents`` are: intent type, role, field, pattern class and
    whether the fingerprint is a configured credential or on the compromised list. Never the string."""
    env = os.environ if env is None else env
    given = [f.strip().lower() for f in (env.get(CREDENTIAL_FINGERPRINTS_ENV) or "").split(",") if re.fullmatch(r"[0-9a-fA-F]{64}", f.strip())]
    configured = {fp: f"{CREDENTIAL_FINGERPRINTS_ENV}[{i}]" for i, fp in enumerate(given)}
    w = "AND i.created_at >= %s" if since else ""
    rows = _rows(cur, "SELECT i.id, i.intent_type, coalesce(r.role, ''), i.policy_decision, i.execution_status, i.payload, i.policy_reason, i.result, i.error, i.created_at "
                      f"FROM agent_intents i LEFT JOIN agent_runs r ON r.id = i.run_id WHERE (row_to_json(i)::text ~ %s OR row_to_json(i)::text ~ %s) {w} ORDER BY i.created_at",
                 (SQL_KEY_RE, SQL_JWT_RE) + ((since,) if since else ()))
    found = []
    for r in rows:
        cols = {"payload": r[5], "policy_reason": r[6], "result": r[7], "error": r[8]}
        for name, value in cols.items():
            for path, cls, fp in _shape_paths(value, name):
                match = configured.get(fp) or ("compromised_list" if fp in COMPROMISED_CREDENTIAL_FINGERPRINTS else "none")
                found.append({"intent": str(r[0])[:8], "intent_type": r[1], "role": r[2], "decision": str(r[3]), "status": str(r[4]), "field": path, "class": cls,
                              "credential_match": match, "created_at": r[9].isoformat() if r[9] else None})
    return {"credentials_checked": list(configured.values()) + ["compromised_list"], "locations": found}


_SCHEMA_ERR = re.compile(r"'type': '([^']+)', 'loc': \[([^\]]*)\]")


def refusal_class(reason: str) -> str:
    """A refused intent's reason as a groupable class: the schema errors' ``type@loc`` (structural, from
    intents.safe_error_summary), otherwise the reason with ids and numbers collapsed."""
    reason = reason or ""
    if reason.startswith("payload schema violation"):
        errs = [f"{t}@{'.'.join(x.strip().strip(chr(39)) for x in loc.split(',') if x.strip())}" for t, loc in _SCHEMA_ERR.findall(reason)]
        return "schema: " + (", ".join(errs) or "?")
    reason = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", reason)
    return scrub(re.sub(r"\d+(\.\d+)?", "<n>", reason))[:120]


def refused_intents(cur, since: Optional[datetime]) -> Dict[str, Any]:
    """Invalid and denied intents in the window, by intent type and reason class (never the payload)."""
    w = "WHERE created_at >= %s" if since else ""
    rows = _rows(cur, f"SELECT intent_type, policy_decision::text, coalesce(policy_reason, '') FROM agent_intents {w}", (since,) if since else ())
    groups: Dict[Tuple[str, str, str], int] = {}
    for itype, decision, reason in rows:
        if decision in ("invalid", "deny"):
            key = (decision, itype, refusal_class(reason))
            groups[key] = groups.get(key, 0) + 1
    by = sorted(({"decision": d, "intent_type": t, "reason": r, "count": n} for (d, t, r), n in groups.items()), key=lambda x: (-x["count"], x["intent_type"]))
    return {"intents": len(rows), "invalid": sum(x["count"] for x in by if x["decision"] == "invalid"),
            "denied": sum(x["count"] for x in by if x["decision"] == "deny"), "by_type_reason": by}


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


def pace_for_cooldown(out: Out, step: str, base: str, token: str) -> None:
    """Sleep (bounded) until every role's wake cooldown has elapsed since the
    fleet's last completed run, so back-to-back stories are not deferred."""
    cooldown = 30
    st, cfg = api("GET", f"{base}/v1/society/config", token)
    if st == 200 and isinstance(cfg, dict):
        roles = cfg.get("roles") or {}
        values = [int((r or {}).get("wake_cooldown_seconds") or 0) for r in roles.values()] if isinstance(roles, dict) else []
        cooldown = max(values) if values else cooldown
    st, status_body = api("GET", f"{base}/v1/society/status", None)
    last = status_body.get("last_run_completed_at") if isinstance(status_body, dict) else None
    wait = min(seconds_to_wait(last, cooldown, _now()), 180)
    if wait:
        out.info(step, f"pacing: sleeping {wait}s so the fleet's wake cooldown ({cooldown}s) has elapsed since the last completed run")
        time.sleep(wait)


def _story_detail(base: str, token: str, correlation: str) -> Dict[str, Any]:
    st, detail = api("GET", f"{base}/v1/society/story/{correlation}/detail", token)
    return detail if st == 200 and isinstance(detail, dict) else {}


def _decisions(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"role": r.get("role"), "status": r.get("status"), "model": r.get("model_name"), "summary": scrub(r.get("decision_summary") or "")[:400], "intents": [i.get("intent_type") for i in r.get("intents") or []]} for r in detail.get("runs") or []]


def memory_view(cur, agent_name: str, limit: int = 30) -> Dict[str, Any]:
    """Read-only view of the memory ONE role reads back as prior experience.

    Memory is the only runtime state a finished run leaves behind for the next
    one, so a rehearsal that writes "this was non-actionable" changes every
    later decision on a similar signal. Titles are model-authored text: they
    are scrubbed and bounded like any other untrusted string, and contents are
    never read."""
    rows = _rows(
        cur,
        """
        SELECT m.scope::text, m.title, m.created_at, m.expires_at, m.importance,
               m.confidence, m.validation_state, m.source_type, m.correlation_id,
               m.id
        FROM memory_items m
        LEFT JOIN agents a ON a.id = m.agent_id
        WHERE (a.name = %s OR m.scope::text = 'SOCIETY')
          AND (m.expires_at IS NULL OR m.expires_at > now())
        ORDER BY m.created_at DESC
        LIMIT %s
        """,
        (agent_name, limit),
    )
    live = [
        {
            "scope": r[0],
            "title": scrub(str(r[1]))[:160],
            "created_at": r[2].isoformat() if r[2] else None,
            "expires_at": r[3].isoformat() if r[3] else None,
            "importance": int(r[4] or 0),
            "confidence": int(r[5] or 0),
            "validation": r[6],
            "source": r[7],
            "correlation": str(r[8])[:8] if r[8] else None,
            "id": str(r[9]),
        }
        for r in rows
    ]
    totals = _rows(
        cur,
        """
        SELECT count(*),
               count(*) FILTER (WHERE m.expires_at IS NOT NULL),
               count(DISTINCT m.correlation_id)
        FROM memory_items m
        LEFT JOIN agents a ON a.id = m.agent_id
        WHERE (a.name = %s OR m.scope::text = 'SOCIETY')
          AND (m.expires_at IS NULL OR m.expires_at > now())
        """,
        (agent_name,),
    )[0]
    return {
        "agent": agent_name,
        "live_rows": int(totals[0]),
        "rows_with_expiry": int(totals[1]),
        "distinct_correlations": int(totals[2]),
        "newest": live,
    }


def memory_search(cur, agent_name: str, needle: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Read-only title search across ALL live rows a role reads back.

    memory_view shows the newest few, which is the wrong end of the list when
    the row you need to correct is old and the fleet has been busy since. The
    needle is matched as a LITERAL substring: ILIKE wildcards in operator input
    would silently widen the search, and a refutation must hit the row the
    operator meant."""
    literal = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = _rows(
        cur,
        """
        SELECT m.id, m.title, m.created_at, m.validation_state, m.correlation_id, m.scope::text
        FROM memory_items m
        LEFT JOIN agents a ON a.id = m.agent_id
        WHERE (a.name = %s OR m.scope::text = 'SOCIETY')
          AND (m.expires_at IS NULL OR m.expires_at > now())
          AND m.title ILIKE %s ESCAPE '\\'
        ORDER BY m.created_at DESC
        LIMIT %s
        """,
        (agent_name, f"%{literal}%", limit),
    )
    return [
        {
            "id": str(r[0]),
            "title": scrub(str(r[1]))[:200],
            "created_at": r[2].isoformat() if r[2] else None,
            "validation": r[3],
            # full, not the usual 8 chars: a refutation has to be justified from
            # that story's intents, and `intents:<correlation>` needs the whole id
            "correlation": str(r[4]) if r[4] else None,
            "scope": r[5],
        }
        for r in rows
    ]


def step_memory(out: Out, conn, role: str) -> None:
    agent_name = "Society_" + role.capitalize()
    needle = (os.getenv("PHASE5_MEMORY_MATCH", "") or "").strip()
    with conn.cursor() as cur:
        view = memory_view(cur, agent_name)
        hits = memory_search(cur, agent_name, needle) if needle else []
    out.check("memory", "M01", True, f"{agent_name}: {view['live_rows']} live memory row(s) from {view['distinct_correlations']} correlation(s), {view['rows_with_expiry']} with an expiry")
    if needle:
        out.check("memory", "M02", True, f"{len(hits)} live row(s) whose title contains the literal {needle!r}")
        out.json("memory", f"{role}.match", hits)
    else:
        out.json("memory", f"{role}.view", view)


def _memory_row(conn, memory_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        rows = _rows(
            cur,
            """
            SELECT m.title, m.validation_state, m.created_at, m.scope::text,
                   m.correlation_id, a.name, m.importance, m.confidence
            FROM memory_items m
            LEFT JOIN agents a ON a.id = m.agent_id
            WHERE m.id = %s
            """,
            (memory_id,),
        )
    if not rows:
        return None
    r = rows[0]
    return {
        "id": memory_id,
        "title": scrub(str(r[0]))[:200],
        "validation": r[1],
        "created_at": r[2].isoformat() if r[2] else None,
        "scope": r[3],
        "correlation": str(r[4])[:8] if r[4] else None,
        "agent": r[5],
        "importance": int(r[6] or 0),
        "confidence": int(r[7] or 0),
    }


def _refute_history(conn, memory_id: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        rows = _rows(
            cur,
            """
            SELECT e.from_state, e.to_state, e.actor_type, u.email, e.reason, e.created_at
            FROM memory_validation_events e
            LEFT JOIN users u ON u.id = e.actor_user_id
            WHERE e.memory_id = %s
            ORDER BY e.created_at
            """,
            (memory_id,),
        )
    return [
        {
            "from": r[0],
            "to": r[1],
            "actor_type": r[2],
            "actor": (r[3] or "").split("@")[0] or None,
            "reason": scrub(str(r[4] or ""))[:300],
            "at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


def step_refute(out: Out, base: str, token: str, conn, memory_id: str) -> None:
    """Correct a false belief through the operator API, never through SQL.

    The driver holds a database connection, so it COULD set validation_state
    directly in one statement. It must not, and the distinction is the whole
    point: an edit leaves no audit row, and a record that can be silently
    rewritten is not evidence. This calls the ordinary operator endpoint and
    then reads the append-only history back OUT of the database, so the claim
    "the refutation was recorded" is checked against the table rather than
    against the response that asserts it."""
    reason = (os.getenv("PHASE5_REFUTE_REASON", "") or "").strip()
    if not reason:
        out.check("refute", "R00", False, "PHASE5_REFUTE_REASON is required — a refutation must say why")
        return
    before = _memory_row(conn, memory_id)
    if before is None:
        out.check("refute", "R00", False, f"memory {memory_id[:8]} not found")
        return
    out.json("refute", "before", before)
    if before["validation"] == "refuted":
        out.info("refute", f"{memory_id[:8]} is already refuted; re-running to confirm idempotency")

    st, body = api("POST", f"{base}/v1/society/memory/{memory_id}/refute", token, {"reason": reason})
    ok = st == 200 and isinstance(body, dict)
    out.check("refute", "R01", ok, f"refute {memory_id[:8]} HTTP {st}{'' if ok else ' ' + str(body)[:160]}")
    if not ok:
        return
    mem = body.get("memory") or {}
    out.check("refute", "R02", mem.get("validation_state") == "refuted", f"validation_state={mem.get('validation_state')!r} (was {before['validation']!r})")

    after = _memory_row(conn, memory_id)
    preserved = bool(after) and scrub(str(after["title"]))[:200] == before["title"] and after["created_at"] == before["created_at"] and after["correlation"] == before["correlation"]
    out.check("refute", "R03", preserved, "title, created_at and correlation untouched — the row still records what was believed")

    st2, body2 = api("POST", f"{base}/v1/society/memory/{memory_id}/refute", token, {"reason": reason})
    out.check("refute", "R04", st2 == 200 and isinstance(body2, dict) and (body2 or {}).get("already_refuted") is True, f"repeat refute HTTP {st2} already_refuted={(body2 or {}).get('already_refuted')}")

    hist = _refute_history(conn, memory_id)
    out.check("refute", "R05", len(hist) == 1, f"{len(hist)} append-only audit row(s) for this memory after two calls (exactly 1 expected)")
    out.json("refute", "history", hist)
    out.json("refute", "after", after)


def _candidate_row(conn, candidate_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        rows = _rows(
            cur,
            """
            SELECT c.status::text, c.title, c.error, c.created_at, c.correlation_id,
                   c.task_id, t.status::text, t.escrow_amount,
                   w.balance_credits, w.reserved_credits
            FROM code_candidates c
            LEFT JOIN task_sessions t ON t.id = c.task_id
            LEFT JOIN wallets w ON w.owner_id = t.caller_agent_id AND w.owner_type = 'agent'
            WHERE c.id = %s
            """,
            (candidate_id,),
        )
    if not rows:
        return None
    r = rows[0]
    return {
        "id": candidate_id[:8],
        "status": r[0],
        "title": scrub(str(r[1] or ""))[:160],
        "error": scrub(str(r[2]))[:200] if r[2] else None,
        "created_at": r[3].isoformat() if r[3] else None,
        "correlation": str(r[4])[:8] if r[4] else None,
        "task": str(r[5])[:8] if r[5] else None,
        "task_status": r[6],
        "escrow": int(r[7] or 0) if r[7] is not None else None,
        "caller_balance": int(r[8]) if r[8] is not None else None,
        "caller_reserved": int(r[9]) if r[9] is not None else None,
    }


def _abandon_events(conn, candidate_id: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        rows = _rows(
            cur,
            """
            SELECT e.actor_type, e.payload->>'previous_status', e.payload->>'task_refunded',
                   e.idempotency_key, e.correlation_id, e.created_at
            FROM society_events e
            WHERE e.event_type = 'code_candidate.abandoned' AND e.subject_id = %s
            ORDER BY e.created_at
            """,
            (candidate_id,),
        )
    return [
        {"actor_type": r[0], "previous_status": r[1], "task_refunded": r[2],
         "idempotency_key": r[3], "correlation": str(r[4])[:8] if r[4] else None,
         "created_at": r[5].isoformat() if r[5] else None}
        for r in rows
    ]


def step_abandon(out: Out, base: str, token: str, conn, candidate_id: str) -> None:
    """Close a stranded candidate through the operator API, never through SQL.

    Same discipline as `refute`: the driver holds a database connection and
    COULD set status='abandoned' in one statement, which would leave no audit
    row and no refund. It calls the ordinary operator endpoint instead and then
    reads the row, the escrow and the append-only event back OUT of the
    database, so "the candidate was abandoned and the money was released" is
    checked against the tables rather than against the response asserting it."""
    reason = (os.getenv("PHASE5_ABANDON_REASON", "") or "").strip()
    if not reason:
        out.check("abandon", "A00", False, "PHASE5_ABANDON_REASON is required — abandoning must say why")
        return
    before = _candidate_row(conn, candidate_id)
    if before is None:
        out.check("abandon", "A00", False, f"candidate {candidate_id[:8]} not found")
        return
    out.json("abandon", "before", before)
    if before["status"] == "abandoned":
        out.info("abandon", f"{candidate_id[:8]} is already abandoned; re-running to confirm idempotency")

    st, body = api("POST", f"{base}/v1/society/candidates/{candidate_id}/abandon", token, {"reason": reason})
    ok = st == 200 and isinstance(body, dict)
    out.check("abandon", "A01", ok, f"abandon {candidate_id[:8]} HTTP {st}{'' if ok else ' ' + str(body)[:200]}")
    if not ok:
        return
    cand = (body or {}).get("candidate") or {}
    out.check("abandon", "A02", cand.get("status") == "abandoned", f"status={cand.get('status')!r} (was {before['status']!r})")

    after = _candidate_row(conn, candidate_id)
    out.json("abandon", "after", after)
    preserved = bool(after) and after["title"] == before["title"] and after["created_at"] == before["created_at"] and after["correlation"] == before["correlation"]
    out.check("abandon", "A03", preserved, "title, created_at and correlation untouched — the row still records what was attempted")

    # Economics: the escrow is released exactly once, through the ordinary path.
    if before["task"] is not None:
        # task_status enum: initiated | in_progress | completed | failed | timeout | refunded
        released = after["task_status"] in ("failed", "timeout", "refunded") if after else False
        out.check("abandon", "A04", released or before["task_status"] not in ("initiated", "in_progress"),
                  f"implementation task {before['task']} {before['task_status']} -> {after['task_status'] if after else '?'}")
        if before["caller_reserved"] is not None and after and after["caller_reserved"] is not None:
            freed = before["caller_reserved"] - after["caller_reserved"]
            out.check("abandon", "A05", after["caller_reserved"] <= before["caller_reserved"],
                      f"caller reserved {before['caller_reserved']} -> {after['caller_reserved']} (freed {freed}), balance {before['caller_balance']} -> {after['caller_balance']}")
    else:
        out.info("abandon", "candidate carried no implementation task; nothing to refund")

    st2, body2 = api("POST", f"{base}/v1/society/candidates/{candidate_id}/abandon", token, {"reason": reason + " (repeat)"})
    out.check("abandon", "A06", st2 == 200 and isinstance(body2, dict) and (body2 or {}).get("already_abandoned") is True,
              f"repeat abandon HTTP {st2} already_abandoned={(body2 or {}).get('already_abandoned')}")
    repeat = _candidate_row(conn, candidate_id)
    if repeat and after:
        out.check("abandon", "A07", repeat["caller_reserved"] == after["caller_reserved"] and repeat["caller_balance"] == after["caller_balance"],
                  f"repeat released nothing further (reserved {after['caller_reserved']} -> {repeat['caller_reserved']})")

    evs = _abandon_events(conn, candidate_id)
    out.check("abandon", "A08", len(evs) == 1, f"{len(evs)} append-only audit event(s) after two calls (exactly 1 expected)")
    out.check("abandon", "A09", all(e["actor_type"] == "operator" for e in evs), "the audit row names an operator, not an agent")
    out.json("abandon", "events", evs)


def step_company(out: Out, base: str, token: str, args: List[str]) -> None:
    """Owner actions on company mode through the operator API (token in memory only):
    view plans/tickets, start a cycle, set an objective status, approve/reject a plan."""
    act = args[0] if args else "view"
    if act == "cycle":
        st, body = api("POST", f"{base}/v1/society/company/cycles", token)
        out.check("company", "K01", st in (200, 201), f"start cycle HTTP {st}{'' if st in (200, 201) else ' ' + str(body)[:200]}")
        out.json("company", "cycle", body)
    elif act in COMPANY_OBJ_STATUS:
        st, body = api("POST", f"{base}/v1/society/company/objectives/{args[1]}/status", token, {"status": COMPANY_OBJ_STATUS[act]})
        out.check("company", "K02", st == 200, f"objective {args[1]} -> {COMPANY_OBJ_STATUS[act]} HTTP {st}{'' if st == 200 else ' ' + str(body)[:200]}")
        out.json("company", "objectives", body)
    elif act in ("approve", "reject"):
        plan_id = args[1]
        if plan_id == "latest":
            st, body = api("GET", f"{base}/v1/society/company/plans", token)
            waiting = [p for p in ((body or {}).get("plans") or []) if p.get("status") == "awaiting_owner"] if isinstance(body, dict) else []
            if not out.check("company", "K03", bool(waiting), f"{len(waiting)} plan(s) awaiting the owner (HTTP {st})"):
                return
            plan_id = str(waiting[0]["id"])
        st, body = api("POST", f"{base}/v1/society/company/plans/{plan_id}/{act}", token)
        out.check("company", "K04", st == 200, f"{act} plan {plan_id[:8]} HTTP {st}{'' if st == 200 else ' ' + str(body)[:200]}")
        out.json("company", "decision", body)
    st, plans = api("GET", f"{base}/v1/society/company/plans", token)
    out.check("company", "K05", st == 200, f"GET company/plans HTTP {st}")
    out.json("company", "plans", plans)
    st, tks = api("GET", f"{base}/v1/society/company/tickets?limit=20", token)
    out.check("company", "K06", st == 200, f"GET company/tickets HTTP {st}")
    out.json("company", "tickets", tks)
    st, appr = api("GET", f"{base}/v1/society/approvals", token)
    queue = ((appr or {}).get("candidates") or {}).get("owner_merge") if isinstance(appr, dict) else None
    out.check("company", "K07", st == 200, f"GET approvals HTTP {st}: {len(queue or [])} RED PR(s) in the owner merge queue")
    out.json("company", "owner_merge", queue or [])


#: The owner's decision of 2026-10-14 (project thread): a freeze whose maintenance incident recovered outlived it.
FREEZE_LIFT_REASON = "owner 2026-10-14: incident recovered, freeze outlived it"
FREEZE_LIFT_REASON_SURFACE = "owner 2026-10-14: public surface recovered >= 6h ago and green now, freeze outlived it"
#: Verbatim copy of services/registry/app/society/company.py OUTLIVED_FREEZES_SQL (this script imports no app code;
#: tests/society/test_company_board.py asserts the two are identical).
OUTLIVED_FREEZES_SQL = (
    "SELECT f.id FROM society_incident_freezes f LEFT JOIN maintenance_incidents i ON i.id::text = f.evidence ->> 'incident_id' "
    "WHERE f.lifted_at IS NULL AND (i.status IN ('recovered', 'closed') OR (f.source = 'public_surface' "
    "AND EXISTS (SELECT 1 FROM society_events r WHERE r.event_type = 'public.surface.recovered' AND r.created_at > f.opened_at "
    "AND r.created_at <= NOW() - INTERVAL '6 hours') "
    "AND (SELECT bool_and(o.ok) FROM maintenance_observations o WHERE o.source = 'public_surface_monitor' AND o.observed_at >= NOW() - INTERVAL '1 hour' "
    "AND o.observed_at = (SELECT MAX(observed_at) FROM maintenance_observations WHERE source = 'public_surface_monitor')))) ORDER BY f.opened_at")


def _open_freezes(conn) -> List[Tuple]:
    """(freeze id, source, status label), oldest first. The label is the linked maintenance incident's
    status, ``recovered>=6h+green`` for an outlived surface freeze, else ``-``; liftable = OUTLIVED_FREEZES_SQL."""
    with conn.cursor() as cur:
        outlived = {str(r[0]) for r in _rows(cur, OUTLIVED_FREEZES_SQL)}
        rows = _rows(cur, "SELECT f.id, f.source, i.status FROM society_incident_freezes f LEFT JOIN maintenance_incidents i "
                          "ON i.id::text = f.evidence ->> 'incident_id' WHERE f.lifted_at IS NULL ORDER BY f.opened_at")
    conn.rollback()
    return [(fid, src, st or ("recovered>=6h+green" if str(fid) in outlived else None), str(fid) in outlived) for fid, src, st in rows]


def _by_source(rows: Sequence[Tuple]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for _, src, st, _ in rows:
        k = f"{src}/{st or '-'}"
        out[k] = out.get(k, 0) + 1
    return out


OWNER_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")


def _production() -> bool:
    return "production" in (vs.env("ENVIRONMENT", "") + " " + vs.env("RAILWAY_ENVIRONMENT_NAME", "")).lower()


def step_owner_grant(out: Out, base: str, token: str, conn, email: str) -> None:
    """Make an EXISTING account an operator (staging has no SMTP, so the owner cannot verify by email).
    The owner registers on the dashboard with their own password first; this step never creates a user
    and never reads or writes a password. It marks the account verified on the database (like O01b) and
    assigns the durable ``operator`` role through the operator API (POST /v1/society/operators ->
    operator_auth.assign_role). Prints only {email prefix, existed, verified, role}. On production it runs
    only when OWNER_GRANT_CONFIRM names the same email (never automatically)."""
    prefix = email.split("@")[0]
    if _production() and vs.env("OWNER_GRANT_CONFIRM", "").strip().lower() != email.lower():
        out.check("owner", "G00", False, f"production: owner:grant:{prefix}@... runs only with OWNER_GRANT_CONFIRM set to the same email")
        return
    with conn.cursor() as cur:
        row = _rows(cur, "SELECT id, email, is_email_verified FROM users WHERE lower(email) = lower(%s)", (email,))
        if not row:
            conn.rollback()
            out.check("owner", "G01", False, f"no account for {prefix}@...: register on the dashboard first (this step never creates one)")
            out.json("owner", "grant", {"email": prefix, "existed": False, "verified": False, "role": None})
            return
        user_id, stored_email, verified_before = row[0]
        if not verified_before:
            cur.execute("UPDATE users SET is_email_verified = TRUE WHERE id = %s", (user_id,))
    conn.commit()
    out.check("owner", "G01", True, f"account {prefix}@... exists; email verified on the database ({'already' if verified_before else 'now'})")
    st, body = api("POST", f"{base}/v1/society/operators", token, {"email": stored_email, "role": "operator"})
    role = body.get("role") if st == 200 and isinstance(body, dict) else None
    out.check("owner", "G02", role == "operator", f"durable role via operator_auth.assign_role: HTTP {st}, role={role}")
    out.json("owner", "grant", {"email": prefix, "existed": True, "verified": True, "role": role})


OWNER_SLO_NAMES = (("first_run_start", "L01", "owner event -> first run start"), ("reply", "L02", "owner event -> Governor reply"),
                   ("directive_to_plan", "L03", "directive -> first ticket in a plan"))


def step_owner_slo(out: Out, base: str, token: str) -> None:
    """The Owner Console's latency SLOs, read from the operator API (owner_thread.slo): p95 under its
    target and no owner row left late. A metric with no sample in the window is reported, not failed."""
    st, body = api("GET", f"{base}/v1/society/company/owner-thread?limit=5", token)
    if not out.check("owner", "L00", st == 200 and isinstance(body, dict), f"GET company/owner-thread HTTP {st}"):
        return
    slo = body.get("slo") or {}
    for key, code, label in OWNER_SLO_NAMES:
        m = slo.get(key) or {}
        detail = f"{label}: p95 {m.get('p95_s')}s / target {m.get('target_s')}s over {m.get('n', 0)} sample(s), {m.get('late_unanswered', 0)} late"
        if m.get("ok") is None:
            out.info("owner", f"{code} no data yet -- {detail}")
        else:
            out.check("owner", code, bool(m.get("ok")), detail)
    out.json("owner", "slo", slo)
    out.json("owner", "thread", [{"kind": e.get("kind"), "status": e.get("status"), "created_at": e.get("created_at"), "replied": bool(e.get("reply")),
                                  "tickets": len(e.get("tickets") or []), "plans": len(e.get("plans") or [])} for e in body.get("entries") or []])


def step_freezes(out: Out, base: str, token: str, conn) -> None:
    """Lift, through the operator API, ONLY the open freezes that outlived their cause
    (``OUTLIVED_FREEZES_SQL``). Any other freeze is never touched. Idempotent: a second run
    finds nothing left to lift."""
    before = _open_freezes(conn)
    liftable = [r for r in before if r[3]]
    kept = [r for r in before if not r[3]]
    out.check("freezes", "F01", True, f"open before: {_by_source(before)}; liftable (outlived their cause): {len(liftable)}")
    failed = []
    for fid, src, _, _ in liftable:
        reason = FREEZE_LIFT_REASON_SURFACE if src == "public_surface" else FREEZE_LIFT_REASON
        st, body = api("POST", f"{base}/v1/society/incidents/{fid}/lift", token, {"reason": reason})
        if st != 200 or not isinstance(body, dict) or not body.get("liftedAt"):
            failed.append(f"{str(fid)[:8]}:HTTP {st}")
    out.check("freezes", "F02", not failed, f"lifted {len(liftable) - len(failed)}/{len(liftable)} through the operator API" + (f"; failed {failed}" if failed else ""))
    after = _open_freezes(conn)
    after_ids = {str(r[0]) for r in after}
    out.check("freezes", "F03", not any(r[3] for r in after), f"open after: {_by_source(after)}")
    out.check("freezes", "F04", all(str(r[0]) in after_ids for r in kept), f"untouched: {len(kept)} freeze(s) that did not outlive their cause stay open")
    with conn.cursor() as cur:  # why each kept freeze is open: structural reason, age, and for the surface its anomaly vs a later recovery
        why = _rows(cur, "SELECT f.id, f.source, f.reason, EXTRACT(EPOCH FROM (NOW() - f.opened_at)) / 3600.0, "
                         "(SELECT jsonb_agg(jsonb_build_object('name', x ->> 'name', 'failure', x ->> 'failure', 'status', x ->> 'final_status')) "
                         " FROM society_events a, jsonb_array_elements(COALESCE(a.payload -> 'failing', '[]'::jsonb)) x WHERE a.id::text = f.evidence ->> 'anomaly_event_id'), "
                         "(SELECT MAX(r.created_at) FROM society_events r WHERE r.event_type = 'public.surface.recovered' AND r.created_at > f.opened_at) "
                         "FROM society_incident_freezes f WHERE f.lifted_at IS NULL AND f.id::text = ANY(%s) ORDER BY f.opened_at", ([str(r[0]) for r in kept],))
    conn.rollback()
    for fid, src, reason, age, failing, recovered_at in why:
        out.info("freezes", f"kept {str(fid)[:8]} {src} {float(age):.1f}h: {scrub(str(reason or ''))[:160]}; failing={failing or []}; "
                            f"surface recovered since: {recovered_at.isoformat() if recovered_at else 'no'}")


# ── costs: model spend per role / build / activity, and the caps it implies ──

BUILD_INTENT = "BUILD_CODE_CANDIDATE"
COST_DECIDE_SQL = (
    "SELECT r.role, COALESCE(r.model_name, '?'), r.cost_usd, (r.created_at AT TIME ZONE 'UTC')::date, "
    "EXISTS (SELECT 1 FROM agent_intents i WHERE i.run_id = r.id AND i.intent_type = %s), "
    "EXISTS (SELECT 1 FROM agent_intents i WHERE i.run_id = r.id AND i.intent_type = %s AND i.execution_status = 'executed'), "
    "EXISTS (SELECT 1 FROM agent_intents i WHERE i.run_id = r.id AND i.intent_type = %s AND (i.error ILIKE '%%cost_budget%%' OR i.error ILIKE '%%cost cap%%')) "
    "FROM agent_runs r WHERE r.created_at >= NOW() - (%s * INTERVAL '1 day') AND r.model_provider = ANY(%s) AND r.cost_usd > 0")
COST_ACTIVITY_SQL = (
    "SELECT kind, COALESCE(model_name, '?'), cost_usd, (started_at AT TIME ZONE 'UTC')::date, status = 'succeeded', error_class = 'cost_budget' "
    "FROM repair_activities WHERE started_at >= NOW() - (%s * INTERVAL '1 day') AND model_provider = ANY(%s) AND cost_usd > 0")
#: what the recommendation plans for: builds/day, the minimum caps, the share of the daily budget one build may take
COST_PLAN = {"builds_per_day": 2, "min_case_cap": 0.50, "max_case_share": 0.25, "min_role_budget": 0.25, "headroom": 1.5, "story_decides": 6}


def _pct(values: Sequence[float], q: float) -> Optional[float]:
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 4) if v else None


def _ceil05(x: float) -> float:
    return round(-(-round(x * 100) // 5) * 0.05, 2)


def cost_summary(runs: Sequence[Tuple], activities: Sequence[Tuple]) -> Dict[str, Any]:
    """runs: (role, model, cost, day, is_build, delivered, cap_hit); activities: (kind, model, cost, day, ok, cap_hit).
    Per role+model: decide runs; per model: builds; per kind+model: maintenance activities; per day: totals."""
    def stats(costs: List[float]) -> Dict[str, Any]:
        return {"n": len(costs), "p50": _pct(costs, 0.5), "p95": _pct(costs, 0.95), "max": round(max(costs), 4) if costs else None, "sum": round(sum(costs), 4)}

    decide: Dict[str, List[float]] = {}
    role_day: Dict[str, Dict[Any, float]] = {}
    builds: Dict[str, List[float]] = {}
    build_meta: Dict[str, List[int]] = {}
    days: Dict[str, float] = {}
    for role, model, cost, day, is_build, delivered, cap_hit in runs:
        c = float(cost or 0)
        days[str(day)] = days.get(str(day), 0.0) + c
        if is_build:
            builds.setdefault(str(model), []).append(c)
            meta = build_meta.setdefault(str(model), [0, 0])
            meta[0] += int(bool(delivered))
            meta[1] += int(bool(cap_hit))
        else:
            decide.setdefault(f"{role}/{model}", []).append(c)
            role_day.setdefault(str(role), {}).setdefault(day, 0.0)
            role_day[str(role)][day] += c
    acts: Dict[str, List[float]] = {}
    act_caps: Dict[str, int] = {}
    for kind, model, cost, day, _ok, cap_hit in activities:
        acts.setdefault(f"{kind}/{model}", []).append(float(cost or 0))
        act_caps[f"{kind}/{model}"] = act_caps.get(f"{kind}/{model}", 0) + int(bool(cap_hit))
        days[str(day)] = days.get(str(day), 0.0) + float(cost or 0)
    return {
        "decide": {k: stats(v) for k, v in sorted(decide.items())},
        "role_daily": {r: {"days": len(d), "p50": _pct(list(d.values()), 0.5), "p95": _pct(list(d.values()), 0.95)} for r, d in sorted(role_day.items())},
        "builds": {m: {**stats(v), "delivered": build_meta[m][0], "cap_hits": build_meta[m][1],
                       "usd_per_delivered": round(sum(v) / build_meta[m][0], 4) if build_meta[m][0] else None} for m, v in sorted(builds.items())},
        "activities": {k: {**stats(v), "cap_hits": act_caps[k]} for k, v in sorted(acts.items())},
        "days": {d: round(v, 4) for d, v in sorted(days.items())},
    }


def recommend_costs(summary: Dict[str, Any], daily_budget: float, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deterministic caps from measured spend (every rule is printed with its inputs):
    case cap = p95 build x1.25 (x2 when builds hit the cap: the sample is censored), within
    [min_case_cap, max_case_share x budget]; a role's grant = its p95 daily spend x headroom
    (>= min_role_budget), the Builder's plus builds_per_day x case cap; the correlation cap
    = case cap + story_decides x the dearest decide p95; the strong-tier floor = 2x that
    p95 (an Opus decide costs ~2x a Sonnet one). role_daily is decide spend only (builds apart).
    Fits when the grants sum within the budget."""
    p = {**COST_PLAN, **(plan or {})}
    builds = [b for b in summary.get("builds", {}).values() if b["n"]]
    p95_build = max((b["p95"] for b in builds), default=0.0)
    censored = any(b["cap_hits"] for b in builds)
    case_cap = _ceil05(min(max(p95_build * (2.0 if censored else 1.25), p["min_case_cap"]), p["max_case_share"] * daily_budget))
    decide_p95 = max((d["p95"] for d in summary.get("decide", {}).values() if d["n"]), default=0.0)
    roles: Dict[str, float] = {}
    for role, d in summary.get("role_daily", {}).items():
        base = (d["p95"] or 0.0) * p["headroom"] + (p["builds_per_day"] * case_cap if role == "builder" else 0.0)
        roles[role] = _ceil05(max(base, p["min_role_budget"]))
    if builds and "builder" not in roles:
        roles["builder"] = _ceil05(max(p["builds_per_day"] * case_cap, p["min_role_budget"]))
    total = round(sum(roles.values()), 2)
    return {
        "MAINTENANCE_MAX_CASE_COST_USD": case_cap,
        "SOCIETY_MAX_CORRELATION_COST_USD": _ceil05(case_cap + p["story_decides"] * decide_p95),
        "SOCIETY_MODEL_STRONG_FLOOR_USD": _ceil05(max(2 * decide_p95, 0.05)),
        "grants": roles,
        "grants_total": total,
        "fits_daily_budget": total <= daily_budget,
        "inputs": {"p95_build": p95_build, "build_cap_hits": censored, "decide_p95_max": decide_p95, "daily_budget": daily_budget, "plan": p},
    }


def step_costs(out: Out, conn, days: int, daily_budget: float) -> None:
    with conn.cursor() as cur:
        runs = _rows(cur, COST_DECIDE_SQL, (BUILD_INTENT, BUILD_INTENT, BUILD_INTENT, days, list(LIVE_PROVIDERS)))
        acts = _rows(cur, COST_ACTIVITY_SQL, (days, list(LIVE_PROVIDERS)))
        grants = _rows(cur, "SELECT role, daily_model_budget_usd FROM agent_capability_grants ORDER BY role")
    conn.rollback()
    summary = cost_summary(runs, acts)
    for key in ("decide", "role_daily", "builds", "activities", "days"):
        out.json("costs", key, summary[key])
    out.json("costs", "grants_now", {str(r): float(b) for r, b in grants})
    rec = recommend_costs(summary, daily_budget)
    out.json("costs", "recommend", rec)
    out.check("costs", "C01", bool(runs), f"{len(runs)} priced live runs and {len(acts)} activities in {days}d")
    out.check("costs", "C02", rec["fits_daily_budget"], f"recommended grants sum {rec['grants_total']} within the daily budget {daily_budget}")


def step_budget(out: Out, conn, role: str, usd: str) -> None:
    """Operator command: one role's grant daily_model_budget_usd (a re-seed keeps the larger of it and the role default)."""
    with conn.cursor() as cur:
        row = _rows(cur, "SELECT id, daily_model_budget_usd FROM agent_capability_grants WHERE role = %s", (role.lower(),))
        if not row:
            out.check("budget", "B01", False, f"no grant for role {role!r}")
            return
        gid, before = row[0]
        cur.execute("UPDATE agent_capability_grants SET daily_model_budget_usd = %s, updated_at = now() WHERE id = %s", (usd, gid))
        conn.commit()
        after = _rows(cur, "SELECT daily_model_budget_usd FROM agent_capability_grants WHERE id = %s", (gid,))[0][0]
    out.check("budget", "B01", float(after) == float(usd), f"role={role.lower()} daily_model_budget_usd {float(before)} -> {float(after)}")


ROUTES_SQL = (
    "SELECT r.created_at, r.role, r.model_tier, COALESCE(r.model_name, '?'), r.route_reason, e.event_type, r.cost_usd, "
    "(SELECT i.execution_status FROM agent_intents i WHERE i.run_id = r.id AND i.intent_type = %s ORDER BY i.seq LIMIT 1) "
    "FROM agent_runs r JOIN society_events e ON e.id = r.event_id "
    "WHERE r.role IN ('builder', 'architect') AND r.model_provider = ANY(%s) AND r.route_reason IS NOT NULL ORDER BY r.created_at DESC LIMIT %s")


def step_routes(out: Out, conn, limit: int) -> None:
    """The router's recorded reason for the last Builder/Architect runs (structural: tier, model, reason, cost, build outcome)."""
    with conn.cursor() as cur:
        rows = _rows(cur, ROUTES_SQL, (BUILD_INTENT, list(LIVE_PROVIDERS), limit))
    conn.rollback()
    for at, role, tier, model, reason, event_type, cost, build in rows:
        out.json("routes", at.isoformat(timespec="seconds"), {"role": role, "tier": tier, "model": model, "event": event_type, "cost": float(cost or 0),
                                                              "build": build, "reason": reason})
    strong = sum(1 for r in rows if r[2] == "strong")
    out.check("routes", "R01", bool(rows), f"{len(rows)} builder/architect routes, {strong} strong")


def journey_summary(rows: Sequence[Tuple]) -> Dict[str, Any]:
    """(ok, duration_s, step, observed_at, target) rows, newest first -> counts and duration percentiles."""
    durs = sorted(float(r[1]) for r in rows if r[1] not in (None, ""))

    def pct(q: float) -> Optional[float]:
        return round(durs[min(len(durs) - 1, int(q * len(durs)))], 2) if durs else None

    ok = sum(1 for r in rows if r[0])
    return {"observations": len(rows), "ok": ok, "failed": len(rows) - ok, "success_rate": round(ok / len(rows), 4) if rows else None,
            "duration_s": {"p50": pct(0.5), "p95": pct(0.95), "max": durs[-1] if durs else None},
            "first_at": rows[-1][3].isoformat() if rows else None, "last_at": rows[0][3].isoformat() if rows else None,
            "failed_steps": sorted({str(r[2]) for r in rows if not r[0]}),
            "latest": [{"ok": bool(r[0]), "duration_s": r[1], "step": r[2], "at": r[3].isoformat(), "target": r[4]} for r in rows[:10]]}


def step_journey(out: Out, conn, hours: int) -> None:
    """The core-journey probe's evidence (maintenance/journey_probe.py): structural rows only."""
    with conn.cursor() as cur:
        rows = _rows(cur, "SELECT ok, payload ->> 'duration_s', payload ->> 'step', observed_at, target FROM maintenance_observations "
                          "WHERE sli = 'core_journey' AND observed_at >= %s ORDER BY observed_at DESC", (_now() - timedelta(hours=hours),))
    s = journey_summary(rows)
    out.check("journey", "J01", s["observations"] > 0, f"{s['observations']} core_journey observation(s) in the last {hours}h, {s['ok']} ok, p50 {s['duration_s']['p50']}s")
    out.json("journey", "summary", s)


#: An undecided plan older than this means the company is waiting for someone (board off or holding).
AUTONOMY_PLAN_WAIT_HOURS = 24
#: A design review still open this long means its sweep is not running (design_review.py windows are 2 h each).
AUTONOMY_REVIEW_STUCK_HOURS = 7
#: a real meeting: at least this many roles spoke in one review's thread (Architect + reviewers + Governor = 5)
AUTONOMY_MEETING_ROLES = 4
#: design_review.PROTOCOL: wakes that ask each role its question by name (G1j); older reviews are listed, not judged
REVIEW_PROTOCOL = 2


def step_autonomy(out: Out, conn, days: int) -> None:
    """Does the company run without the owner? Structural counts only (no model text)."""
    since = _now() - timedelta(days=days)
    with conn.cursor() as cur:
        plans = _rows(cur, "SELECT status, COALESCE(decided_by, CASE WHEN decided_at IS NULL THEN NULL ELSE 'owner' END), "
                           "EXTRACT(EPOCH FROM (COALESCE(decided_at, NOW()) - created_at)) / 3600.0, board_note FROM society_daily_plans WHERE created_at >= %s", (since,))
        # the protocol a review ran (its wakes carry it, design_review.PROTOCOL): only current-protocol reviews are judged
        reviews = _rows(cur, "SELECT r.status, r.round, EXTRACT(EPOCH FROM (NOW() - r.stage_at)) / 3600.0, r.id, COALESCE(r.decision ->> 'by', ''), "
                             "COALESCE((SELECT MAX((e.payload ->> 'protocol')::int) FROM society_events e WHERE e.event_type LIKE 'design_review.%%' "
                             "AND e.payload ->> 'review_id' = r.id::text), 1) "
                             "FROM society_design_reviews r WHERE r.created_at >= %s ORDER BY r.created_at DESC", (since,))
        legacy = sum(1 for r in reviews if int(r[5]) < REVIEW_PROTOCOL)
        reviews = [r[:5] for r in reviews if int(r[5]) >= REVIEW_PROTOCOL]
        # who was woken for each review (the run each stage wake created) and who spoke in its thread
        woken = _rows(cur, "SELECT e.payload ->> 'review_id', r.role, r.status::text, COUNT(*) FROM society_events e JOIN agent_runs r ON r.event_id = e.id "
                           "WHERE e.event_type LIKE 'design_review.%%' AND e.created_at >= %s GROUP BY 1, 2, 3", (since,))
        spoke = _rows(cur, "SELECT c.thread_id::text, g.role, COUNT(*) FROM agent_chat c JOIN agent_capability_grants g ON g.agent_id = c.from_agent_id "
                           "WHERE c.created_at >= %s AND c.thread_id IN (SELECT id FROM society_design_reviews) GROUP BY 1, 2", (since,))
        chat = _rows(cur, "SELECT COUNT(*) FROM agent_chat WHERE created_at >= %s AND thread_id IN (SELECT id FROM society_design_reviews)", (since,))[0][0]
        tks = _rows(cur, "SELECT CASE WHEN proof::text LIKE '%%bench:%%' THEN 'bench' WHEN proof::text LIKE '%%probe:escrow%%' OR proof::text LIKE '%%probe:a2a%%' "
                         "THEN 'growth' ELSE source END, status, COUNT(*) FROM society_tickets WHERE created_at >= %s GROUP BY 1, 2", (since,))
        merged = _rows(cur, "SELECT COUNT(*) FROM society_tickets WHERE merged_at >= %s", (since,))[0][0]
        live = _rows(cur, "SELECT COUNT(*) FROM a2a_remote_agents WHERE consecutive_failures = 0 AND last_validated_at >= %s", (_now() - timedelta(days=7),))[0][0]
        via_dir = _rows(cur, "SELECT COUNT(*) FROM a2a_remote_agents WHERE provenance ->> 'source' = 'directory'")[0][0]
        scans = _rows(cur, "SELECT COUNT(*) FROM society_events WHERE event_type = 'a2a.directory.scanned' AND idempotency_key LIKE '%%:done' AND created_at >= %s", (since,))[0][0]
        last_scan = _rows(cur, "SELECT payload FROM society_events WHERE event_type = 'a2a.directory.scanned' AND idempotency_key LIKE '%%:done' ORDER BY created_at DESC LIMIT 1")
        listings = _rows(cur, "SELECT payload ->> 'directory', payload ->> 'outcome', created_at FROM society_events WHERE event_type = 'a2a.directory.listed' "
                              "AND idempotency_key LIKE '%%:done' ORDER BY created_at DESC LIMIT 3")
        # the board holds while any freeze is open: which ones, and is their maintenance incident still open?
        freezes = _rows(cur, "SELECT f.source, COALESCE(i.status, '-'), COUNT(*), EXTRACT(EPOCH FROM (NOW() - MIN(f.opened_at))) / 3600.0 FROM society_incident_freezes f "
                             "LEFT JOIN maintenance_incidents i ON i.id::text = f.evidence ->> 'incident_id' WHERE f.lifted_at IS NULL GROUP BY 1, 2 ORDER BY 3 DESC")
        bench_open = _rows(cur, "SELECT id, status, proof, reason FROM society_tickets WHERE proof::text LIKE '%%bench:%%' AND (status IN ('proposed', 'planned', 'approved', 'building') "
                                "OR updated_at >= %s) ORDER BY updated_at DESC LIMIT 5", (since,))
        bench_view = []
        for tid, st, proof, reason in bench_open:
            task = next((str(p).split(":", 1)[1] for p in (proof or []) if str(p).startswith("bench:")), None)
            reps = _rows(cur, "SELECT LEFT(revision, 12), per_task -> %s ->> 'delivered', jsonb_array_length(per_task -> %s -> 'runs'), created_at FROM society_bench_reports "
                              "WHERE revision = judge_revision AND per_task ? %s ORDER BY created_at DESC LIMIT 3", (task, task, task)) if task else []
            bench_view.append({"ticket": str(tid)[:8], "status": st, "task": task, "reports": [f"{d}/{n}@{r}" for r, d, n, _ in reps],
                               "reason": scrub(str(reason or ""))[:160]})
    by_decider: Dict[str, int] = {}
    for st, who, _, _ in plans:
        k = who or st
        by_decider[k] = by_decider.get(k, 0) + 1
    waits = sorted(float(h) for st, who, h, _ in plans if who)
    waiting = [float(h) for st, who, h, _ in plans if st == "awaiting_owner"]
    holds = sorted({str(n) for st, _, _, n in plans if st == "awaiting_owner" and n})
    stuck = [r for r in reviews if r[0] in ("proposing", "reviewing", "deciding") and float(r[2]) > AUTONOMY_REVIEW_STUCK_HOURS]
    rev: Dict[str, int] = {}
    for st, *_ in reviews:
        rev[st] = rev.get(st, 0) + 1
    go_runtime = sum(1 for st, _, _, _, by in reviews if st == "go" and by == "runtime")
    go_governor = sum(1 for st, _, _, _, by in reviews if st == "go" and by != "runtime")
    runs_by: Dict[str, Dict[str, List[str]]] = {}
    for rid, role, st, n in woken:  # e.g. {"qa": ["completed"], "scout": ["skipped"]}: a role with no run was never woken
        runs_by.setdefault(str(rid), {}).setdefault(str(role), []).append(str(st) + (f"x{n}" if n > 1 else ""))
    said: Dict[str, Dict[str, int]] = {}
    for tid, role, n in spoke:
        said.setdefault(str(tid), {})[str(role)] = int(n)
    meetings = [{"review": str(rid)[:8], "status": st, "by": by or "-", "woken": runs_by.get(str(rid), {}), "spoke": said.get(str(rid), {})}
                for st, _, _, rid, by in reviews[:5]]
    real = [str(rid)[:8] for st, _, _, rid, _ in reviews if sum(said.get(str(rid), {}).values()) >= AUTONOMY_MEETING_ROLES and len(said.get(str(rid), {})) >= AUTONOMY_MEETING_ROLES]
    tickets_view: Dict[str, Dict[str, int]] = {}
    for kind, st, n in tks:
        tickets_view.setdefault(str(kind), {})[str(st)] = int(n)
    oldest = max(waiting) if waiting else 0.0
    out.check("autonomy", "A01", oldest < AUTONOMY_PLAN_WAIT_HOURS,
              f"plans in {days}d: {by_decider}; median wait {waits[len(waits) // 2] if waits else 'n/a'}h; oldest undecided {oldest:.1f}h"
              + (f"; board holds: {holds}" if holds else "")
              + (f"; open freezes (source, incident status, n, oldest h): {[(str(a), str(b), int(n), round(float(h), 1)) for a, b, n, h in freezes]}" if freezes else ""))
    # a timeout is never approval: a review only passes when the Governor said go
    out.check("autonomy", "A02", not stuck and not (go_runtime and not go_governor),
              f"design reviews in {days}d: {rev}; go by Governor {go_governor}, go by runtime {go_runtime}; stuck > {AUTONOMY_REVIEW_STUCK_HOURS}h: {len(stuck)}"
              + (f"; {legacy} legacy review(s) not judged (before protocol {REVIEW_PROTOCOL})" if legacy else ""))
    out.check("autonomy", "A03", not reviews or bool(real),
              f"{int(chat)} message(s) in design-review threads; meetings with >= {AUTONOMY_MEETING_ROLES} roles speaking: {real or 'none'}; latest: "
              + "; ".join(f"{m['review']} {m['status']}/{m['by']} woken {m['woken'] or '-'} spoke: {', '.join(sorted(m['spoke'])) or 'nobody'}" for m in meetings))
    out.check("autonomy", "A04", True, f"tickets in {days}d by kind/status: {tickets_view}; merged {int(merged)}")
    scan = (last_scan[0][0] if last_scan else None) or {}
    out.check("autonomy", "A05", True, f"federated network: {int(live)} live remote card(s) (7d), {int(via_dir)} found via directories, {int(scans)} directory scan(s); "
              f"last scan listed={scan.get('listed')} new={scan.get('new')} refreshed={scan.get('refreshed')} errors={scan.get('errors')}; "
              f"listing requests: {[(str(d), str(o)) for d, o, _ in listings] or 'none yet (needs A2A_SERVER_ENABLED + A2A_PUBLIC_BASE_URL on the society-worker)'}")
    out.check("autonomy", "A06", True, f"bench tickets vs their newest reports (newest first): {bench_view or 'none'}")
    out.json("autonomy", "summary", {"days": days, "plans_by_decider": by_decider, "plan_wait_hours": waits, "undecided_hours": waiting,
                                     "reviews": rev, "review_messages": int(chat), "go_by_governor": go_governor, "go_by_runtime": go_runtime, "meetings": meetings, "tickets": tickets_view, "merged_tickets": int(merged),
                                     "live_remote_cards": int(live), "directory_agents": int(via_dir), "directory_scans": int(scans),
                                     "last_scan": {k: scan.get(k) for k in ("listed", "new", "refreshed", "errors")}, "listings": [(str(d), str(o)) for d, o, _ in listings],
                                     "bench_tickets": bench_view, "open_freezes": [(str(a), str(b), int(n)) for a, b, n, _ in freezes]})


def step_candidates(out: Out, conn, limit: int) -> None:
    """Read recent candidates and, crucially, WHY each one ended where it did.

    A status alone cannot distinguish "the guard rejected busywork" (the system
    working) from "the pipeline broke" (a defect), and the difference decides
    whether anything needs repairing. Read-only; model-authored text is
    scrubbed and bounded like every other untrusted string."""
    with conn.cursor() as cur:
        rows = _rows(
            cur,
            """
            SELECT c.id, c.status::text, c.title, c.error, c.changed_files, c.diff_stat,
                   c.branch_name, c.requires_security_review, c.risk_tier,
                   c.qa_report, c.security_report, c.correlation_id, c.created_at, c.spec
            FROM code_candidates c
            ORDER BY c.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
    view = []
    for r in rows:
        qa = r[9] if isinstance(r[9], dict) else {}
        sec = r[10] if isinstance(r[10], dict) else {}
        spec = r[13] if isinstance(r[13], dict) else {}
        view.append(
            {
                "candidate": str(r[0])[:8],
                "status": r[1],
                "title": scrub(str(r[2] or ""))[:120],
                "error": scrub(str(r[3]))[:300] if r[3] else None,
                "changed_files": r[4],
                # the spec's rules (structural): what the Builder may touch and must turn green
                "files_allowed": list(spec.get("files_allowed") or [])[:12],
                "acceptance_tests": [scrub(str(t))[:160] for t in (spec.get("acceptance_tests") or [])][:12],
                "diff_stat": r[5],
                "branch": r[6],
                "requires_security_review": r[7],
                "risk_tier": r[8],
                "qa_verdict": qa.get("verdict"),
                "qa_failures": [scrub(str(f))[:160] for f in (qa.get("failures") or [])][:4],
                "security_verdict": sec.get("verdict"),
                "correlation": str(r[11]),
                "created_at": r[12].isoformat() if r[12] else None,
            }
        )
    out.check("candidates", "N01", True, f"{len(view)} candidate(s); statuses={sorted({v['status'] for v in view})}")
    out.json("candidates", "records", view)
    with conn.cursor() as cur:
        rej = rejection_breakdown(cur)
    out.check("candidates", "R01", True, f"rejected since {rej['since'][:16]}Z: {rej['rejected']} -- "
              + (", ".join(f"{k} {v}" for k, v in rej["by_reason"].items()) or "none") + f"; spec gate refused {rej['spec_gate_refusals']} design(s) before a build")
    out.json("candidates", "rejections", rej)


# ── rejections by structured reason: a VERBATIM copy of services/registry/app/society/rejections.py
# (classify + its tables); tests/society/test_rejections.py asserts the two agree. ──
SINCE_SPEC_GATE = datetime(2026, 10, 27, 4, 47, tzinfo=timezone.utc)
QA_CHECKS = {"no_self_judging": "no_self_judging", "allow_list": "outside_files_allowed", "test_first_red_on_base": "test_first_red",
             "test_first_green_on_head": "test_first_green", "ci_collects_new_tests": "ci_orphan", "test_first_intact": "test_first_tampered",
             "acceptance_tests": "acceptance_failed", "acceptance_tests_exist": "acceptance_failed"}


def classify_rejection(event: Optional[Dict[str, Any]], qa_failures: Optional[Sequence[str]], security_verdict: Optional[str], error: Optional[str]) -> str:
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
        return "test_first_red"
    if event.get("busywork"):
        return "busywork"
    if security_verdict == "fail":
        return "security"
    names = [str(f).split(":", 1)[0].strip() for f in (qa_failures or [])]
    for name in names:
        if name in QA_CHECKS:
            return QA_CHECKS[name]
    return "qa_other" if names else "other"


def rejection_breakdown(cur, since: datetime = SINCE_SPEC_GATE) -> Dict[str, Any]:
    rows = _rows(cur, """
        SELECT c.qa_report -> 'failures', c.security_report ->> 'verdict', c.error,
               (SELECT e.payload FROM society_events e WHERE e.subject_id = c.id AND e.event_type = 'code_candidate.rejected' ORDER BY e.created_at DESC LIMIT 1)
        FROM code_candidates c WHERE c.status::text = 'rejected' AND c.updated_at >= %s""", (since,))
    by: Dict[str, int] = {}
    for failures, verdict, error, payload in rows:
        reason = classify_rejection(payload, failures if isinstance(failures, list) else [], verdict, error)
        by[reason] = by.get(reason, 0) + 1
    gate = _rows(cur, """SELECT COUNT(*) FROM agent_intents WHERE created_at >= %s AND ((intent_type = 'PROPOSE_DESIGN' AND result ->> 'did' = 'gate_refused')
                         OR error LIKE 'spec gate:%%')""", (since,))[0][0]
    return {"since": since.isoformat(), "rejected": len(rows), "by_reason": dict(sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))), "spec_gate_refusals": int(gate or 0)}


def step_promotions(out: Out, conn, limit: int) -> None:
    """Read the durable promotion records the CONTROLLER wrote.

    A PR that is not merged is weak evidence: it could simply not have been
    tried yet. The eligibility record says WHY it is not merged -- which gate
    blocked it and whether auto-merge was even allowed -- and that is the claim
    "auto-merge is off" actually has to be checked against. Read-only."""
    with conn.cursor() as cur:
        rows = _rows(
            cur,
            """
            SELECT p.id, p.status::text, p.provider, p.risk_tier, p.external_branch,
                   p.external_pr_number, p.external_pr_url, p.ci_state, p.merge_state,
                   p.merged_sha, p.failure_reason, p.eligibility, p.created_at,
                   p.candidate_id, p.correlation_id, a.name
            FROM code_promotions p
            LEFT JOIN agents a ON a.id = p.requested_by_agent_id
            ORDER BY p.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
    view = []
    for r in rows:
        gates = r[11] if isinstance(r[11], dict) else {}
        view.append(
            {
                "promotion": str(r[0])[:8],
                "status": r[1],
                "provider": r[2],
                "risk_tier": r[3],
                "branch": r[4],
                "pr": r[5],
                "pr_url": r[6],
                "ci_state": r[7],
                "merge_state": r[8],
                "merged_sha": (r[9] or "")[:12] or None,
                "failure_reason": scrub(str(r[10]))[:200] if r[10] else None,
                "requested_by": r[15],
                "candidate": str(r[13])[:8],
                "correlation": str(r[14]),
                "created_at": r[12].isoformat() if r[12] else None,
                "gates": {
                    k: gates.get(k)
                    for k in (
                        "merge_eligible", "auto_merge_enabled", "auto_merge_allowed",
                        "human_approval_required", "human_approval_satisfied",
                        "human_approvals", "blocking", "ci_passed", "qa_pass",
                        "security_pass", "fitness_precheck", "trusted_risk_tier",
                    )
                    if k in gates
                },
            }
        )
    out.check("promotions", "P01", True, f"{len(view)} promotion record(s)")
    merged = [v for v in view if v["status"] == "merged" or v["merged_sha"]]
    out.check("promotions", "P02", not merged, f"nothing merged autonomously ({len(merged)} merged record(s) found)")
    auto = [v for v in view if v["gates"].get("auto_merge_enabled") or v["gates"].get("auto_merge_allowed")]
    out.check("promotions", "P03", not auto, f"no promotion was auto-merge eligible ({len(auto)} found)")
    out.json("promotions", "records", view)
    with conn.cursor() as cur:
        waiting = ready_without_promotion(cur)
    silent = [w for w in waiting if not w["requests"] and not w["ended_promotion"]]
    out.check("promotions", "P04", not silent, f"READY candidates without an active promotion: {len(waiting)}, never requested: {len(silent)}"
              + "".join(f"; [{w['candidate']}] requests={w['requests']} last={w['last_request']} {w['last_error'] or ''} ended={w['ended_promotion'] or '-'} wakes={w['ready_wakes']}"
                        for w in waiting[:5]))
    out.json("promotions", "ready_without_promotion", waiting)
    with conn.cursor() as cur:
        budget = change_budget(cur)
    out.check("promotions", "P05", budget["caps"] is not None,
              f"change budget in effect: {budget['caps']} (heartbeat {budget['at'] or '-'}); counted open PRs now {budget['open_prs']}; "
              f"ci_failed promotions {budget['ci_failed']}, READY rows whose promotion already ended {budget['ready_ended']}")
    out.json("promotions", "change_budget", budget)


#: promotion.OPEN_PR_STATUSES as text
OPEN_PR = ("pr_open", "ci_pending", "ci_passed", "ci_failed", "awaiting_approval", "merge_eligible")


def change_budget(cur) -> Dict[str, Any]:
    """The worker's change budget as its last heartbeat reported it (promotion_queue.caps), and what the rows count now."""
    hb = _rows(cur, "SELECT payload, created_at FROM society_events WHERE event_type IN ('society.heartbeat', 'society.heartbeat.idle') "
                    "AND payload -> 'change_budget' IS NOT NULL ORDER BY created_at DESC LIMIT 1")
    open_now = _rows(cur, "SELECT COUNT(*) FROM code_promotions WHERE status::text = ANY(%s)", (list(OPEN_PR),))[0][0]
    ci_failed = _rows(cur, "SELECT COUNT(*) FROM code_promotions WHERE status::text = 'ci_failed'")[0][0]
    ended = _rows(cur, """SELECT COUNT(*) FROM code_candidates c WHERE c.status::text = 'ready' AND EXISTS (
                              SELECT 1 FROM code_promotions p WHERE p.candidate_id = c.id AND p.status::text IN ('merged', 'rejected')
                              AND p.created_at = (SELECT max(q.created_at) FROM code_promotions q WHERE q.candidate_id = c.id))""")[0][0]
    return {"caps": (hb[0][0] or {}).get("change_budget") if hb else None, "at": hb[0][1].isoformat() if hb else None,
            "open_prs": int(open_now or 0), "ci_failed": int(ci_failed or 0), "ready_ended": int(ended or 0)}


#: promotion.ACTIVE_STATUSES as text (tests/society/test_promotion_queue.py asserts they are identical).
ACTIVE_PROMOTION = ("requested", "validating", "branch_ready", "pr_open", "ci_pending", "ci_passed", "awaiting_approval", "merge_eligible", "blocked_external")


def ready_without_promotion(cur) -> List[Dict[str, Any]]:
    """Every READY candidate with no active promotion, and why (promotion_queue.ready_backlog, from the rows):
    its REQUEST_PR_PROMOTION intents (count, last status, last refusal -- our words), an ended promotion, its READY wakes."""
    rows = _rows(cur, """
        SELECT c.id, c.updated_at,
               (SELECT count(*) FROM agent_intents i WHERE i.intent_type = 'REQUEST_PR_PROMOTION' AND i.payload ->> 'candidate_id' = c.id::text),
               (SELECT i.execution_status::text || '|' || coalesce(i.error, i.policy_reason, '') FROM agent_intents i WHERE i.intent_type = 'REQUEST_PR_PROMOTION'
                  AND i.payload ->> 'candidate_id' = c.id::text ORDER BY i.created_at DESC LIMIT 1),
               (SELECT p.status::text FROM code_promotions p WHERE p.candidate_id = c.id ORDER BY p.created_at DESC LIMIT 1),
               (SELECT count(*) FROM society_events e WHERE e.event_type = 'code_candidate.ready' AND e.subject_id = c.id)
        FROM code_candidates c
        WHERE c.status::text = 'ready' AND NOT EXISTS (SELECT 1 FROM code_promotions p WHERE p.candidate_id = c.id AND p.status::text = ANY(%s))
        ORDER BY c.updated_at""", (list(ACTIVE_PROMOTION),))
    out = []
    for r in rows:
        status, _, error = (r[3] or "").partition("|")
        out.append({"candidate": str(r[0])[:8], "ready_since": r[1].isoformat() if r[1] else None, "requests": int(r[2]), "last_request": status or None,
                    "last_error": scrub(error)[:200] or None, "ended_promotion": r[4], "ready_wakes": int(r[5])})
    return out


def step_intents(out: Out, base: str, token: str, correlation: str) -> None:
    detail = _story_detail(base, token, correlation)
    ok = bool(detail)
    out.check("intents", "D01", ok, f"story detail for {correlation[:8]} {'read' if ok else 'unavailable'}")
    if not ok:
        return
    out.json("intents", f"{correlation[:8]}.intents", intent_rows(detail))
    out.json("intents", f"{correlation[:8]}.decisions", _decisions(detail))
    out.json("intents", f"{correlation[:8]}.events", [{k: e.get(k) for k in ("event_type", "causation_depth", "status")} for e in detail.get("events") or []])


CANARY_CALLER = "Phase5_Canary_Caller"
CANARY_CALLEE = "Phase5_Canary_Callee"
CANARY_CAPABILITY = "staging.canary.echo"


def _ensure_canary_agent(out: Out, base: str, token: str, name: str) -> Optional[str]:
    """Register (or reuse) a staging-only agent owned by the operator user.

    Ordinary authenticated agent registration — the same call any integrator
    makes. Nothing here touches the database."""
    st, body = api("GET", f"{base}/v1/agents/?limit=100", token)
    if st == 200:
        rows = body if isinstance(body, list) else (body or {}).get("items") or []
        for a in rows:
            if isinstance(a, dict) and a.get("name") == name and a.get("id"):
                return str(a["id"])
    st, body = api(
        "POST", f"{base}/v1/agents/", token,
        {
            "name": name,
            "description": "Phase 5 closure: controlled staging task-failure canary (no external endpoint).",
            "capabilities": [{
                "name": CANARY_CAPABILITY, "version": "1.0.0",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                "price": 1.0,
            }],
            "endpoint": "https://staging.invalid/phase5-canary",
            "public_key": "phase5-closure-canary-not-a-key",
        },
    )
    if st in (200, 201) and isinstance(body, dict) and body.get("id"):
        return str(body["id"])
    out.check("taskfail", "T00", False, f"register {name}: HTTP {st} {str(body)[:120]}")
    return None


def step_taskfail(out: Out, base: str, token: str, conn, credits: int, timeout: float) -> None:
    """REAL domain signal: create a task through the ordinary API, fail it
    through the ordinary callee path, and let the runtime's own
    ``world.ingest_task_outcomes`` turn that platform fact into ``task.failed``.

    Nothing about the society is simulated: no event is injected, no TaskSession
    row is written by hand. This is the production path a real failing task
    takes, which is the only kind of evidence a Scout should act on."""
    caller = _ensure_canary_agent(out, base, token, CANARY_CALLER)
    callee = _ensure_canary_agent(out, base, token, CANARY_CALLEE)
    if not (caller and callee):
        return
    out.check("taskfail", "T00", True, f"canary agents ready (caller {caller[:8]}, callee {callee[:8]})")

    # Fund the caller's wallet through the ledger so escrow can lock.
    with conn.cursor() as cur:
        row = _rows(cur, "SELECT id, balance_credits FROM wallets WHERE owner_type = 'agent' AND owner_id = %s", (caller,))
        if not row:
            out.check("taskfail", "T01", False, "caller wallet missing")
            return
        wallet_id, balance = str(row[0][0]), int(row[0][1])
        if balance < credits:
            key = f"phase5-closure-fund-{caller[:8]}"
            if not _rows(cur, "SELECT id FROM transactions WHERE idempotency_key = %s", (key,)):
                tx = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO transactions (id, from_wallet, to_wallet, amount, currency, status, type, platform_fee, platform_fee_rate, extra_data, idempotency_key, created_at) "
                    "VALUES (%s, NULL, %s, %s, 'credits', 'pending', 'deposit', 0, 0, %s, %s, now())",
                    (tx, wallet_id, max(credits * 4, 40), json.dumps({"operator_action": "phase5_closure_canary"}), key),
                )
                conn.commit()
                cur.execute("UPDATE transactions SET status = 'completed', completed_at = now() WHERE id = %s AND status = 'pending'", (tx,))
                conn.commit()
        balance = int(_rows(cur, "SELECT balance_credits FROM wallets WHERE id = %s", (wallet_id,))[0][0])
    out.check("taskfail", "T01", balance >= credits, f"caller wallet funded: {balance} credits available for a {credits}-credit escrow")

    st, body = api(
        "POST", f"{base}/v1/tasks/", token,
        {
            "caller_agent_id": caller, "callee_agent_id": callee,
            "capability": CANARY_CAPABILITY,
            "input": {"text": "phase5 closure controlled failure canary"},
            "max_budget": credits, "currency": "credits", "timeout_seconds": 300,
        },
    )
    # POST /v1/tasks answers with the escrow trace, keyed task_session_id.
    task_id = str((body or {}).get("task_session_id") or (body or {}).get("id") or "") if isinstance(body, dict) else ""
    out.check("taskfail", "T02", st in (200, 201) and bool(task_id), f"task created through POST /v1/tasks: HTTP {st} id={task_id[:8] or '-'}")
    if not task_id:
        out.json("taskfail", "create_error", {"status": st, "body": scrub(str(body))[:300]})
        return

    err = "callee returned HTTP 502 from https://staging.invalid/phase5-canary (staging canary: upstream unreachable)"
    st, body = api("PUT", f"{base}/v1/tasks/{task_id}/fail?error_message={urllib.parse.quote(err)}&agent_id={callee}", token)
    out.check("taskfail", "T03", st == 200, f"task failed through the ordinary callee path PUT /v1/tasks/<id>/fail: HTTP {st}")
    if st != 200:
        out.json("taskfail", "fail_error", {"status": st, "body": scrub(str(body))[:300]})
        return

    # The runtime ingests task outcomes itself, before each dispatch cycle.
    deadline = time.time() + min(timeout, 300)
    correlation, event_id = "", ""
    while time.time() < deadline:
        with conn.cursor() as cur:
            rows = _rows(
                cur,
                "SELECT id, correlation_id, event_type, status FROM society_events "
                "WHERE subject_type = 'task' AND subject_id = %s ORDER BY created_at LIMIT 2",
                (task_id,),
            )
        if rows:
            event_id, correlation = str(rows[0][0]), str(rows[0][1])
            out.check("taskfail", "T04", len(rows) == 1, f"exactly one task-outcome event for this task: {[(r[2], r[3]) for r in rows]}")
            break
        time.sleep(5)
    if not correlation:
        out.check("taskfail", "T04", False, "no task.failed event was ingested within the window (world.ingest_task_outcomes)")
        return

    with conn.cursor() as cur:
        st_rows = _rows(cur, "SELECT status, error_message IS NOT NULL, escrow_amount FROM task_sessions WHERE id = %s", (task_id,))
    out.check("taskfail", "T05", bool(st_rows) and str(st_rows[0][0]).lower().endswith("failed"), f"task row status={st_rows[0][0] if st_rows else '?'} (set by the API, not by hand)")

    pace_for_cooldown(out, "taskfail", base, token)
    deadline = time.time() + min(timeout, 600)
    detail: Dict[str, Any] = {}
    while time.time() < deadline:
        detail = _story_detail(base, token, correlation)
        runs = detail.get("runs") or []
        if runs and all(r.get("status") in ("completed", "failed", "dead", "skipped") for r in runs):
            break
        time.sleep(10)

    runs = detail.get("runs") or []
    roles = sorted({r.get("role") for r in runs if r.get("status") == "completed"})
    dead = [r for r in runs if r.get("status") == "dead"]
    fake = [r for r in runs if r.get("status") == "completed" and r.get("model_provider") not in (None, *LIVE_PROVIDERS)]
    out.check("taskfail", "T06", bool(runs), f"the real task.failed event woke the fleet: {len(runs)} run(s), roles completed={roles}")
    out.check("taskfail", "T07", not dead, f"no DEAD run on the real-domain story ({len(dead)} dead)")
    out.check("taskfail", "T08", not fake, "every completed run used the live provider (no fake autonomy)")
    out.json("taskfail", "story", {"task_id": task_id, "event_id": event_id, "correlation": correlation, "roles_completed": roles})
    out.json("taskfail", "runs", [{k: r.get(k) for k in ("id", "role", "status", "attempt", "model_provider", "model_name", "tokens_in", "tokens_out", "cost_usd", "model_requests", "model_retries")} for r in runs])
    out.json("taskfail", "decisions", _decisions(detail))
    out.json("taskfail", "intents", intent_rows(detail))
    out.json("taskfail", "events", [{k: e.get(k) for k in ("event_type", "causation_depth", "status")} for e in detail.get("events") or []])


def step_canary(out: Out, base: str, token: str, scenario: str, decide: Optional[str], timeout: float) -> None:
    sys.path.insert(0, str(REPO / "services" / "registry"))
    from app.society.canary import CanaryRefused, observe_canary  # the runtime's own canary (HTTP-only)

    pace_for_cooldown(out, "canary", base, token)
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
    detail = _story_detail(base, token, rep.correlation_id)
    out.json("canary", f"{scenario}.decisions", _decisions(detail))
    out.json("canary", f"{scenario}.intent_reasons", intent_rows(detail))
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
    if st != 200 or not isinstance(status_body, dict) or not status_body.get("runtime_enabled") or status_body.get("model_provider") not in LIVE_PROVIDERS or status_body.get("production_deploy_enabled") is not False:
        out.check("signal", "I01", False, f"runtime not in the expected live state (HTTP {st}, flags={ {k: (status_body or {}).get(k) for k in PUBLIC_FLAG_KEYS} if isinstance(status_body, dict) else None})")
        return
    if expect_candidate and not status_body.get("autonomous_code_enabled"):
        out.check("signal", "I01", False, "autonomous code loop is disabled; a candidate cannot be expected")
        return
    pace_for_cooldown(out, "signal", base, token)
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
    out.check("audit", "L01", int(snap["loop_breaker_tripped"]) == 0 and int(snap["loop_breaker_suppressed"]) == 0, f"loop breaker tripped={snap['loop_breaker_tripped']} suppressed={snap['loop_breaker_suppressed']} in the last {hours}h"
              + "".join(f"; [{t['correlation']}] {t['note']} swallowed={t['swallowed']} depth={t['depth']} runs={t['runs']} root={t['root']} {t['subject']}"
                        f" redelivered={t['redelivered']}" for t in snap.get("loop_breaker_trips") or []))
    dead = int(snap["runs_by_status"].get("dead", 0))
    out.check("audit", "L02", dead == 0, f"DEAD runs in the last {hours}h: {dead} {snap['dead_runs'][:3]}")
    out.check("audit", "L03", int(snap["forbidden_high_executed"]) == 0, f"forbidden HIGH intents ever executed: {snap['forbidden_high_executed']}; high-risk rows={snap['high_risk_intents']}")
    out.check("audit", "L04", int(snap["correlations_with_multiple_candidates"]) == 0, f"correlations with >1 candidate: {snap['correlations_with_multiple_candidates']}; duplicate proposal titles: {snap['proposals_duplicate_titles']}")
    fake = [m for m in snap["runs_by_model"] if m[0] not in (None, *LIVE_PROVIDERS) and int(m[2]) > 0]
    out.check("audit", "L05", not fake, f"runs by provider/model: {[(m[0], m[1], m[2]) for m in snap['runs_by_model']]}")
    with conn.cursor() as cur:
        tasks, txs, inflight = society_tasks(cur, since)
        wallets = society_wallets(cur)
        scan = secret_scan(cur, since)
        located = secret_locations(cur, since)
        refused = refused_intents(cur, since)
    out.json("audit", "tasks", [{k: t[k] for k in ("id", "caller", "callee", "status", "escrow_amount", "created_at", "completed_at", "refund_at")} for t in tasks])
    out.json("audit", "transactions", txs)
    out.json("audit", "wallets", wallets)
    for code, ok, note in economics_checks(tasks, txs, wallets, inflight):
        out.check("audit", code, ok, note)
    out.json("audit", "secret_scan", scan)
    runtime_keys = sum(v["key_shaped"] for k, v in scan.items() if k != "society_events_from_users")
    runtime_jwt = sum(v["jwt_shaped"] for k, v in scan.items() if k != "society_events_from_users")
    cot = sum(v["cot_markers"] for v in scan.values())
    out.json("audit", "secret_locations", located)
    out.json("audit", "refused_intents", refused)
    out.check("audit", "I01", True, f"invalid intents {refused['invalid']}/{refused['intents']}, denied {refused['denied']} in the last {hours}h"
              + "".join(f"; {x['decision']} {x['intent_type']} x{x['count']}: {x['reason']}" for x in refused["by_type_reason"][:6]))
    real = [x for x in located["locations"] if x["credential_match"] != "none"]
    out.check("audit", "X01", runtime_keys == 0, f"secret-shaped strings in runtime-produced rows: {runtime_keys} (user-injected payloads: {scan['society_events_from_users']['key_shaped']}, untrusted data)"
              + "".join(f"; [{x['intent']}] {x['intent_type']} role={x['role']} {x['field']} class={x['class']} credential_match={x['credential_match']}" for x in located["locations"][:5])
              + (f"; REAL CREDENTIAL MATCHED ({len(real)}): rotate it" if real else f"; checked against {', '.join(located['credentials_checked'])}"))
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
            if conn is None and (name in ("baseline", "fund", "gate", "audit", "memory", "taskfail", "refute", "promotions", "candidates", "abandon", "journey", "autonomy", "freezes", "costs", "budget", "routes")
                                 or (name == "owner" and args[0] == "grant")):
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
            elif name == "intents":
                step_intents(out, base, token, args[0])
            elif name == "memory":
                step_memory(out, conn, args[0])
            elif name == "refute":
                step_refute(out, base, token, conn, args[0])
            elif name == "promotions":
                step_promotions(out, conn, int(args[0]) if args else 5)
            elif name == "candidates":
                step_candidates(out, conn, int(args[0]) if args else 5)
            elif name == "taskfail":
                step_taskfail(out, base, token, conn, int(args[0]) if args else 5, timeout)
            elif name == "abandon":
                step_abandon(out, base, token, conn, args[0])
            elif name == "company":
                step_company(out, base, token, args)
            elif name == "journey":
                step_journey(out, conn, int(args[0]) if args else 24)
            elif name == "autonomy":
                step_autonomy(out, conn, int(args[0]) if args else 7)
            elif name == "freezes":
                step_freezes(out, base, token, conn)
            elif name == "costs":
                step_costs(out, conn, int(args[0]) if args else 7, float(args[1]) if len(args) > 1 else 6.0)
            elif name == "budget":
                step_budget(out, conn, args[0], args[1])
            elif name == "routes":
                step_routes(out, conn, int(args[0]) if args else 20)
            elif name == "owner" and args == ["slo"]:
                step_owner_slo(out, base, token)
            elif name == "owner":
                step_owner_grant(out, base, token, conn, args[1])
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
