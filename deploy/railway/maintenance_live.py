#!/usr/bin/env python3
"""Maintenance OS live observer -- runs INSIDE Railway staging.

The ``staging-validator`` service's program for the Maintenance OS live
graduation (``VALIDATOR_SCRIPT=maintenance_live.py``, docs/MAINTENANCE_LIVE_PROOF.md).
It is READ-ONLY: it never writes a row, never flips a switch and never calls
the model. It reads the staging database (the kernel's durable state) and,
when the validator secret is present, the operator maintenance API.

Everything printed is structural: ids, states, classes, fingerprints, digests,
counts and timestamps. No page text, no model output text, no token, no
password. Free-text columns (activity errors) are scrubbed and truncated.

    MAINT <code> PASS|FAIL <detail>
    MAINT <step> INFO <detail>
    MAINT-JSON <key> <compact json>
    MAINT RESULT: OK | FAILED <codes> (<n> checks)

Steps (MAINT_PLAN, comma-separated, in order):

    schema                 alembic head, the 15 tables, liveness/uniqueness constraints, immutability triggers
    heartbeat              kernel/release/watchdog heartbeats (component, age, cycles, errors)
    incidents              every incident (class, priority, status, fingerprint, ref, counts)
    cases                  every case (state, risk, next action, deadline, lease) + activities (+ turn_log) + transitions
    turns                  where activity turns go, per kind: reads, read bytes, first patch turn, refusals (turn_log)
    invariants             one active case per incident, one open incident per fingerprint, stranded = 0
    model                  activity model providers (no scripted provider may count as live)
    secrets                release/model credential shapes across every maintenance table
    releases               releases, known-good records, open release freezes
    api                    operator GET /v1/maintenance/status + public summary
    watch:<min>[:<sec>]    bounded synchronous poll: cases + stranded every <sec> (default 60) for <min>

Environment (never printed): POSTGRES_*, REGISTRY_PUBLIC_URL, STAGING_VALIDATOR_SECRET,
VALIDATOR_OPERATOR_EMAIL, EXPECTED_ALEMBIC_HEAD (default 0018_daily_plans), MAINT_PLAN.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import validate_staging as vs  # noqa: E402  (stdlib + psycopg2 only)
from phase5_live import scrub  # noqa: E402

TABLES = (
    "maintenance_incidents", "maintenance_observations", "repair_cases", "repair_plan_revisions",
    "repair_attempts", "repair_activities", "repair_artifacts", "repair_evidence", "repair_transitions",
    "maintenance_releases", "maintenance_known_good", "maintenance_release_freezes",
    "maintenance_toil_events", "maintenance_heartbeats", "maintenance_knowledge",
)
CONSTRAINTS = ("repair_cases_liveness", "repair_cases_terminal_stamped", "repair_cases_state_valid",
               "maintenance_incidents_status_valid", "maintenance_releases_status_valid")
UNIQUE_INDEXES = ("uq_maintenance_incidents_open_fingerprint", "uq_repair_cases_one_active_per_incident",
                  "uq_maintenance_releases_one_in_flight", "uq_repair_activities_idempotency",
                  "uq_repair_plan_revisions_case_revision")
TRIGGERS = ("trg_repair_plan_revisions_immutable", "trg_repair_artifacts_immutable", "trg_repair_transitions_append_only")
TERMINAL = ("AUTO_REPAIRED", "AUTO_ROLLED_BACK", "SAFELY_ESCALATED", "CANNOT_REPRODUCE", "DUPLICATE_RESOLVED", "POLICY_REFUSED")
TERMINAL_SQL = ", ".join(f"'{s}'" for s in TERMINAL)
LIVE_PROVIDERS = ("openai_compatible",)
STALL_SECONDS = 900
MAX_LINE = 12000
# Credential shapes that must never appear in maintenance state (release boundary, ADR-0010 D13).
SECRET_SHAPES = {
    "pem_private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(ghs|ghp|gho|ghu|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "bearer_header": re.compile(r"(?i)authorization\s*[:=]\s*(bearer|token)\s+[A-Za-z0-9._-]{12,}"),
    "provider_key": re.compile(r"\b(sk|rk|re)_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9]{20,}"),
    "postgres_url_password": re.compile(r"postgres(ql)?://[^:/\s]+:[^@\s]{6,}@"),
}
SECRET_ENV_NAMES = ("MAINTENANCE_ATTESTATION_KEY", "MAINTENANCE_RAILWAY_TOKEN", "SOCIETY_MODEL_API_KEY",
                    "SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM", "JWT_SECRET_KEY", "POSTGRES_PASSWORD", "REDIS_PASSWORD")


class Rep:
    def __init__(self) -> None:
        self.count = 0
        self.failed: List[str] = []

    def record(self, code: str, ok: bool, detail: str) -> None:
        self.count += 1
        if not ok:
            self.failed.append(code)
        print(f"MAINT {code} {'PASS' if ok else 'FAIL'} {scrub(detail)[:MAX_LINE]}", flush=True)


def info(step: str, detail: str) -> None:
    print(f"MAINT {step} INFO {scrub(detail)[:MAX_LINE]}", flush=True)


def emit(key: str, obj: Any) -> None:
    print(f"MAINT-JSON {key} " + scrub(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")))[:MAX_LINE], flush=True)


def rows(cur, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def parse_plan(text: str) -> List[Tuple[str, List[str]]]:
    known = ("schema", "heartbeat", "incidents", "cases", "turns", "invariants", "model", "secrets", "releases", "api", "watch")
    out: List[Tuple[str, List[str]]] = []
    for raw in text.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(":")
        if parts[0] not in known:
            raise ValueError(f"unknown step {parts[0]!r} (known: {', '.join(known)})")
        if parts[0] == "watch" and (len(parts) < 2 or not all(p.isdigit() for p in parts[1:])):
            raise ValueError("watch:<minutes>[:<seconds>]")
        out.append((parts[0], parts[1:]))
    return out


# ── steps ──────────────────────────────────────────────────────────────


def step_schema(rep: Rep, cur) -> None:
    want = vs.env("EXPECTED_ALEMBIC_HEAD", "0018_daily_plans")
    heads = [r["version_num"] for r in rows(cur, "SELECT version_num FROM alembic_version")]
    rep.record("S01", want in heads, f"alembic head {heads} (want {want})")
    present = {r["table_name"] for r in rows(cur, "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name = ANY(%s)", (list(TABLES),))}
    missing = sorted(set(TABLES) - present)
    rep.record("S02", not missing, f"{len(present)}/{len(TABLES)} maintenance tables present; missing={missing}")
    cons = {r["conname"] for r in rows(cur, "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)", (list(CONSTRAINTS),))}
    rep.record("S03", cons == set(CONSTRAINTS), f"constraints present={sorted(cons)} missing={sorted(set(CONSTRAINTS) - cons)}")
    idx = {r["indexname"] for r in rows(cur, "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname = ANY(%s)", (list(UNIQUE_INDEXES),))}
    rep.record("S04", idx == set(UNIQUE_INDEXES), f"unique indexes present={sorted(idx)} missing={sorted(set(UNIQUE_INDEXES) - idx)}")
    trg = {r["tgname"] for r in rows(cur, "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND NOT tgisinternal", (list(TRIGGERS),))}
    rep.record("S05", trg == set(TRIGGERS), f"immutability triggers present={sorted(trg)}")
    counts = {t: rows(cur, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in sorted(present)}  # noqa: S608 (fixed allow-list)
    emit("schema.counts", counts)


def step_heartbeat(rep: Rep, cur) -> None:
    now = datetime.now(timezone.utc)
    hb = rows(cur, "SELECT component, worker_id, beat_at, cycles, errors, last_error_class, last_error_at FROM maintenance_heartbeats ORDER BY component")
    for h in hb:
        h["age_seconds"] = int((now - h["beat_at"]).total_seconds())
        h["worker_id"] = (h["worker_id"] or "")[:40]
    emit("heartbeats", [{k: iso(v) for k, v in h.items()} for h in hb])
    kernel = [h for h in hb if h["component"] == "kernel"]
    if kernel:
        rep.record("H01", kernel[0]["age_seconds"] < STALL_SECONDS, f"kernel heartbeat age {kernel[0]['age_seconds']}s cycles={kernel[0]['cycles']} errors={kernel[0]['errors']}")
    else:
        info("heartbeat", "no kernel heartbeat row yet (kernel has not run a cycle)")


def step_incidents(rep: Rep, cur) -> List[Dict[str, Any]]:
    inc = rows(cur, """SELECT id, incident_class, priority, severity, status, fingerprint, desired_state_ref, source, target,
                              observation_count, healthy_streak, case_count, recurrence_count,
                              first_observed_at, last_observed_at, resolved_at, closed_reason
                         FROM maintenance_incidents ORDER BY first_observed_at""")
    emit("incidents.count", {"total": len(inc), "open": sum(1 for i in inc if i["status"] == "open")})
    for i in inc:
        emit(f"incident {i['id']}", {k: iso(v) for k, v in i.items() if k != "id"})
    return inc


def _cases(cur) -> List[Dict[str, Any]]:
    return rows(cur, """SELECT id, incident_id, state, priority, risk_class, repair_class, current_plan_revision, attempt_count,
                               rescope_count, state_tries, model_calls, model_cost_usd, state_entered_at, next_action_at,
                               deadline_at, case_deadline_at, lease_owner, lease_expires_at, resumable, terminal_reason,
                               terminal_at, candidate_id, promotion_id, release_id, base_sha, head_sha, merged_sha, version
                          FROM repair_cases ORDER BY created_at""")


def step_cases(rep: Rep, cur) -> None:
    cs = _cases(cur)
    emit("cases.count", {"total": len(cs), "by_state": _by(cs, "state")})
    for c in cs:
        c["lease_owner"] = (c["lease_owner"] or "")[:40] or None
        emit(f"case {c['id']}", {k: iso(v) for k, v in c.items() if k != "id"})
        tl = ", turn_log" if _has_col(cur, "repair_activities", "turn_log") else ""
        acts = rows(cur, f"""SELECT kind, role, plan_revision, attempt, try_number, status, error_class, model_provider, model_name,
                                   tokens_in, tokens_out, cost_usd, turns, started_at, finished_at, output_digest{tl}
                              FROM repair_activities WHERE case_id=%s ORDER BY started_at""", (c["id"],))
        emit(f"case {c['id']} activities", [{k: iso(v) for k, v in a.items()} for a in acts])
        tr = rows(cur, """SELECT id, from_state, to_state, actor_type, reason_code, evidence_digest, created_at
                            FROM repair_transitions WHERE case_id=%s ORDER BY id""", (c["id"],))
        emit(f"case {c['id']} transitions", [{k: iso(v) for k, v in t.items()} for t in tr])
        plans = rows(cur, "SELECT revision, digest, created_at FROM repair_plan_revisions WHERE case_id=%s ORDER BY revision", (c["id"],))
        if plans:
            emit(f"case {c['id']} plans", [{k: iso(v) for k, v in p.items()} for p in plans])
        atts = rows(cur, """SELECT attempt, plan_revision, outcome, base_sha, head_sha, patch_digest, turns, test_runs, started_at, finished_at
                             FROM repair_attempts WHERE case_id=%s ORDER BY attempt""", (c["id"],))
        if atts:
            emit(f"case {c['id']} attempts", [{k: iso(v) for k, v in a.items()} for a in atts])


def turn_summary(acts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate turn_log per activity kind: structural numbers only."""
    out: Dict[str, Dict[str, Any]] = {}
    for a in acts:
        log = a.get("turn_log") or []
        k = out.setdefault(f"{a['kind']}:{a['status']}:{a.get('error_class') or '-'}", {"tries": 0, "turns": 0, "reads": 0, "read_bytes": 0, "patch_turns": [], "refused": {}, "last": {}})
        k["tries"] += 1
        k["turns"] += len(log)
        k["reads"] += sum(1 for t in log if t.get("read"))
        k["read_bytes"] += sum(int(t.get("bytes") or 0) for t in log if t.get("read"))
        first = next((t["turn"] for t in log if t.get("action") == "apply_patch"), None)
        if first is not None:
            k["patch_turns"].append(first)
        for t in log:
            if t.get("refused"):
                k["refused"][t["refused"]] = k["refused"].get(t["refused"], 0) + 1
        if log:
            k["last"][str(log[-1].get("action"))] = k["last"].get(str(log[-1].get("action")), 0) + 1
    for k in out.values():
        n = max(1, k["tries"])
        pt = k.pop("patch_turns")
        k.update(avg_turns=round(k["turns"] / n, 1), avg_reads=round(k["reads"] / n, 1), avg_read_bytes=k["read_bytes"] // n,
                 tries_with_patch=len(pt), avg_first_patch_turn=round(sum(pt) / len(pt), 1) if pt else None)
    return out


def step_turns(rep: Rep, cur) -> None:
    if not _has_col(cur, "repair_activities", "turn_log"):
        info("turns", "repair_activities.turn_log is not present (migration 0015 not applied)")
        return
    acts = rows(cur, "SELECT kind, status, error_class, turn_log FROM repair_activities ORDER BY started_at DESC LIMIT 500")
    emit("turns.by_kind", turn_summary(acts))


def _has_col(cur, table: str, col: str) -> bool:
    return bool(rows(cur, "SELECT 1 AS x FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, col)))


def _by(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i in items:
        out[str(i.get(key))] = out.get(str(i.get(key)), 0) + 1
    return out


def stranded(cur) -> Tuple[int, int]:
    """(kernel formula, independent formula).

    kernel: non-terminal, no active lease, next_action_at null or stale beyond the stall bound
    independent: non-terminal, no active lease, no next_action_at, not awaiting explicit approval
    (awaiting approval is the terminal-but-resumable SAFELY_ESCALATED, so any non-terminal row counts)."""
    k = rows(cur, f"""SELECT COUNT(*) AS n FROM repair_cases WHERE state NOT IN ({TERMINAL_SQL})
                         AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
                         AND (next_action_at IS NULL OR next_action_at < NOW() - INTERVAL '{STALL_SECONDS} seconds')""")[0]["n"]  # noqa: S608
    i = rows(cur, f"""SELECT COUNT(*) AS n FROM repair_cases WHERE state NOT IN ({TERMINAL_SQL})
                         AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
                         AND next_action_at IS NULL""")[0]["n"]  # noqa: S608
    return int(k), int(i)


def step_invariants(rep: Rep, cur) -> None:
    dup_case = rows(cur, f"""SELECT incident_id, COUNT(*) AS n FROM repair_cases
                              WHERE state NOT IN ({TERMINAL_SQL}) OR (state='SAFELY_ESCALATED' AND resumable)
                              GROUP BY incident_id HAVING COUNT(*) > 1""")  # noqa: S608
    rep.record("I01", not dup_case, f"incidents with >1 active case: {len(dup_case)}")
    dup_fp = rows(cur, "SELECT fingerprint, COUNT(*) AS n FROM maintenance_incidents WHERE status='open' GROUP BY fingerprint HAVING COUNT(*) > 1")
    rep.record("I02", not dup_fp, f"fingerprints with >1 open incident: {len(dup_fp)}")
    storm = rows(cur, "SELECT incident_id, COUNT(*) AS n FROM repair_cases GROUP BY incident_id ORDER BY n DESC LIMIT 1")
    top = storm[0]["n"] if storm else 0
    rep.record("I03", top <= 3, f"max cases for one incident (all time): {top} (bound MAINTENANCE_MAX_CASES_PER_INCIDENT=3)")
    k, i = stranded(cur)
    rep.record("I04", k == 0, f"reconciler.stranded_count={k}")
    rep.record("I05", i == 0, f"independent stranded (non-terminal, no lease, no next_action_at, not awaiting approval)={i}")
    no_deadline = rows(cur, f"SELECT COUNT(*) AS n FROM repair_cases WHERE state NOT IN ({TERMINAL_SQL}) AND (next_action_at IS NULL OR deadline_at IS NULL)")[0]["n"]  # noqa: S608
    rep.record("I06", no_deadline == 0, f"non-terminal cases missing next_action_at/deadline_at: {no_deadline}")


def step_model(rep: Rep, cur) -> None:
    by = rows(cur, "SELECT COALESCE(model_provider,'none') AS provider, status, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost FROM repair_activities GROUP BY 1,2 ORDER BY 1,2")
    emit("model.providers", by)
    model_calls = [b for b in by if b["provider"] != "none"]
    fake = [b for b in model_calls if b["provider"] not in LIVE_PROVIDERS]
    rep.record("M01", not fake, f"non-live model providers on activities: {[(b['provider'], b['n']) for b in fake]}")
    info("model", f"live model activity rows: {sum(b['n'] for b in model_calls if b['provider'] in LIVE_PROVIDERS)}")


def step_secrets(rep: Rep, cur) -> None:
    hits: Dict[str, int] = {}
    env_values = [v for v in (os.getenv(n, "") for n in SECRET_ENV_NAMES) if len(v) >= 12]
    for t in TABLES:
        for r in rows(cur, f"SELECT row_to_json(x)::text AS j FROM (SELECT * FROM {t} ORDER BY 1 DESC LIMIT 2000) x"):  # noqa: S608
            text = r["j"] or ""
            for name, pat in SECRET_SHAPES.items():
                if pat.search(text):
                    hits[f"{t}:{name}"] = hits.get(f"{t}:{name}", 0) + 1
            for v in env_values:
                if v in text:
                    hits[f"{t}:env_value"] = hits.get(f"{t}:env_value", 0) + 1
    rep.record("X01", not hits, f"credential shapes in maintenance tables: {hits or 'none'}")


def step_releases(rep: Rep, cur) -> None:
    rel = rows(cur, """SELECT id, case_id, status, head_sha, created_at, updated_at FROM maintenance_releases ORDER BY created_at""") if _has_col(cur, "maintenance_releases", "updated_at") else rows(cur, "SELECT id, case_id, status, head_sha, created_at FROM maintenance_releases ORDER BY created_at")
    emit("releases", [{k: iso(v) for k, v in r.items()} for r in rel])
    kg = rows(cur, "SELECT id, environment, recorded_at FROM maintenance_known_good ORDER BY recorded_at DESC LIMIT 10")
    emit("known_good", [{k: iso(v) for k, v in r.items()} for r in kg])
    fr = rows(cur, "SELECT id, lifted_at FROM maintenance_release_freezes ORDER BY 1 DESC LIMIT 20")
    emit("release_freezes", {"total": len(fr), "open": sum(1 for f in fr if f["lifted_at"] is None)})


def step_api(rep: Rep) -> None:
    api = vs.env("REGISTRY_PUBLIC_URL").rstrip("/")
    if not api:
        info("api", "REGISTRY_PUBLIC_URL unset; skipped")
        return
    st, body = vs.http("GET", f"{api}/v1/maintenance/summary")
    rep.record("A01", st == 200, f"public maintenance summary HTTP {st}")
    emit("api.summary", json.loads(body) if st == 200 else {"status": st})
    secret = vs.env("STAGING_VALIDATOR_SECRET") or vs.env("VALIDATOR_SECRET")
    email = vs.env("VALIDATOR_OPERATOR_EMAIL")
    if len(secret) < 32 or not email:
        info("api", "no validator secret/operator email; operator view skipped")
        return
    vrep = vs.Report()
    token = vs.ensure_user(vrep, "A10", api, email, vs.derive_password(secret))
    if not token:
        rep.record("A02", False, "operator login failed")
        return
    st, body = vs.http("GET", f"{api}/v1/maintenance/status", token=token)
    rep.record("A02", st == 200, f"operator maintenance status HTTP {st}")
    if st == 200:
        emit("api.status", json.loads(body))


def step_watch(rep: Rep, conn, minutes: int, every: int) -> None:
    end = time.time() + minutes * 60
    last: Dict[str, str] = {}
    worst = 0
    while True:
        with conn.cursor() as cur:
            cs = _cases(cur)
            k, i = stranded(cur)
            inc = rows(cur, "SELECT status, COUNT(*) AS n FROM maintenance_incidents GROUP BY status")
        conn.rollback()  # fresh snapshot next time
        worst = max(worst, k, i)
        changes = []
        for c in cs:
            cid = str(c["id"])
            if last.get(cid) != c["state"]:
                changes.append({"case": cid, "from": last.get(cid), "to": c["state"], "risk": c["risk_class"], "attempts": c["attempt_count"],
                                "calls": c["model_calls"], "next": iso(c["next_action_at"])})
                last[cid] = c["state"]
        info("watch", f"{datetime.now(timezone.utc).isoformat()} stranded={k}/{i} cases={_by(cs, 'state')} incidents={ {r['status']: r['n'] for r in inc} }")
        if changes:
            emit("watch.transitions", changes)
        if time.time() + every > end:
            break
        time.sleep(every)
    rep.record("W01", worst == 0, f"stranded stayed 0 over the watch window (max seen {worst})")


def main() -> int:
    rep = Rep()
    try:
        plan = parse_plan(vs.env("MAINT_PLAN", "schema,heartbeat,incidents,cases,invariants,model,secrets,releases,api"))
    except ValueError as e:
        rep.record("P00", False, str(e))
        return finish(rep)
    conn = vs.db_connect()
    conn.set_session(readonly=True, autocommit=False)
    try:
        for name, args in plan:
            info(name, "begin")
            try:
                if name == "watch":
                    step_watch(rep, conn, int(args[0]), int(args[1]) if len(args) > 1 else 60)
                    continue
                if name == "api":
                    step_api(rep)
                    continue
                with conn.cursor() as cur:
                    {"schema": step_schema, "heartbeat": step_heartbeat, "incidents": step_incidents, "cases": step_cases,
                     "turns": step_turns, "invariants": step_invariants, "model": step_model, "secrets": step_secrets, "releases": step_releases}[name](rep, cur)
                conn.rollback()
            except Exception as e:  # noqa: BLE001  (a step failure never hides later evidence)
                conn.rollback()
                rep.record(f"E-{name}", False, f"{type(e).__name__}: {e}")
    finally:
        conn.close()
    return finish(rep)


def finish(rep: Rep) -> int:
    if rep.failed:
        print(f"MAINT RESULT: FAILED {','.join(rep.failed)} ({rep.count} checks)", flush=True)
        return 1
    print(f"MAINT RESULT: OK ({rep.count} checks)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
