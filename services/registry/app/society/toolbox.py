"""The Society toolbox: agents develop reusable tools, not only one-off answers (owner 2026-11-02, "Hyra").

One tool-development effort should benefit many later tasks. A role that sees the same kind of
problem recur writes it down ONCE as a tool, and every later run of the roles it serves starts
with it. The loop is design -> experiment -> evaluate -> reuse -> upgrade, and only the FIRST step
is the model's:

* **design** -- ``PROPOSE_TOOL`` (any role, LOW): a ``workflow`` (ordered steps for a recurring
  kind of work), a ``prompt`` (role guidance), a ``checklist`` (what a reviewer verifies) or a
  ``toolkit`` (how to use code that is ALREADY on main: 1-5 repo paths that must exist). It must
  name >= ``MIN_OCCURRENCES`` earlier runs / tickets / candidates / proposals where the problem
  occurred -- a tool is for something that recurs. ``REVISE_TOOL`` writes the next immutable
  version of a tool ("develop & upgrade"); a version is never edited in place.
  A toolkit that does not exist yet is BUILT through the ordinary pipeline (CREATE_IMPROVEMENT ->
  plan -> design review -> spec gate -> build -> QA -> PR -> merge law) and registered afterwards;
  this module never files tickets, writes files or runs anything.
* **experiment** -- deterministic, no model: an ``experimental`` tool is shown to the runs of its
  roles in about half of the stories (``arm``: sha256 of tool id + correlation id) and withheld in
  the other half. A revision competes with the version it revises: the treatment arm sees the new
  version, the control arm the old one.
* **evaluate** -- deterministic, execution-grounded (as memory_grounding.py and Harness² are):
  a trial is one COMPLETED run with at least one side-effecting intent; it succeeded when every
  side-effecting intent EXECUTED. Nothing the model says about a tool counts -- there is no
  "I used it" field. With ``min_trials`` trials in each arm, a tool whose success rate beats the
  control by ``MARGIN`` (at no more than ``COST_RATIO`` x the cost) is ``adopted``; one that does
  worse by ``MARGIN`` is ``retired``; ``MAX_TRIALS`` without a verdict retires it as inconclusive.
* **reuse** -- an adopted tool is shown to every run of its roles except a ``HOLDOUT_PCT`` holdout,
  which keeps measuring it: an adopted tool that falls ``MARGIN`` behind its holdout is retired
  (``regressed``). Adopting a revision supersedes the version it revised. The Builder's coding
  harness reads the same tools as advisory text (build_engine.spec_input).

Boundaries: tool text is model-authored, advisory DATA. It is shown labelled
``society_toolbox`` after the laws, never changes a grant, a policy, files_allowed, a test, a
budget or the merge law, and is never executed. The evaluation rules here are trusted base
(owner-merge, risk.OWNER_MERGE_PATTERNS): a candidate cannot change how tools are judged. Off
unless ``SOCIETY_TOOLBOX_ENABLED``; adds no model call.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

KINDS = ("workflow", "prompt", "checklist", "toolkit")
STATUSES = ("experimental", "adopted", "retired", "superseded")
ROLES = ("governor", "scout", "architect", "builder", "qa", "security", "evaluator")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
BODY_MAX, PROBLEM_MAX = 2000, 300
MIN_OCCURRENCES = 2
#: tools shown in one run's context (and so measured by it)
MAX_SHOWN = 3
#: experimental versions alive at once (the experiment portfolio)
MAX_EXPERIMENTAL = 6
#: tool proposals/revisions per agent per UTC day
PER_AGENT_PER_DAY = 3
#: the evaluation law (trusted base: owner-merge)
MARGIN = 0.10
COST_RATIO = Decimal("1.5")
MAX_TRIALS = 60
HOLDOUT_PCT = 20
RECURRING_DAYS = 14

SIDE_EFFECT_FREE = frozenset({"WRITE_MEMORY", "SLEEP", "LIST_REPO_TREE", "SEARCH_REPO", "READ_REPO_FILE", "READ_REPO_RANGE",
                              "READ_DIFF", "READ_CANDIDATE_STATE"})


class ToolRefused(ValueError):
    """A tool proposal/revision the rules refuse (the intent FAILS with this reason)."""


def _norm(name: str) -> str:
    return "-".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())[:64]


def _bucket(tool_id: Any, correlation_id: Any) -> int:
    """0..99, deterministic per (tool, story): every run of one story is in the same arm."""
    return int(hashlib.sha256(f"{tool_id}:{correlation_id}".encode()).hexdigest()[:8], 16) % 100


def _existing_refs(db: Session, refs: Sequence[uuid.UUID]) -> int:
    ids = [str(r) for r in dict.fromkeys(refs)]
    if not ids:
        return 0
    q = ("SELECT COUNT(*) FROM (SELECT id FROM agent_runs WHERE id = ANY(CAST(:ids AS uuid[])) "
         "UNION SELECT id FROM society_tickets WHERE id = ANY(CAST(:ids AS uuid[])) "
         "UNION SELECT id FROM code_candidates WHERE id = ANY(CAST(:ids AS uuid[])) "
         "UNION SELECT id FROM improvement_proposals WHERE id = ANY(CAST(:ids AS uuid[]))) x")
    return int(db.execute(text(q), {"ids": ids}).scalar() or 0)


def _check_paths(repo_root: Optional[str], paths: Sequence[str]) -> List[str]:
    from . import repo_intel  # noqa: PLC0415

    if not paths:
        raise ToolRefused("a toolkit names 1-5 repo paths that already exist on main (build a new one through CREATE_IMPROVEMENT first)")
    root = pathlib.Path(repo_root or ".")
    out = []
    for p in paths:
        try:
            full = repo_intel.contained_read_path(root, p)
        except Exception as exc:  # noqa: BLE001 -- any refusal of the path is a refusal of the tool
            raise ToolRefused(f"toolkit path {p!r} refused: {exc}") from exc
        if not full.is_file():
            raise ToolRefused(f"toolkit path {p!r} is not a file on main")
        out.append(repo_intel.normalize_rel_path(p))
    return out


def propose(db: Session, *, agent_id: Any, kind: str, name: str, roles: Sequence[str], problem: str, body: str,
            occurrences: Sequence[uuid.UUID], toolkit_paths: Sequence[str] = (), revises: Optional[str] = None,
            repo_root: Optional[str] = None, credentials: Iterable[str] = (), now: datetime) -> Dict[str, Any]:
    """Store a new tool (version 1) or, with ``revises``, the next version of that tool -- always
    ``experimental``. Raises ToolRefused. The caller commits."""
    from .secret_redaction import redact  # noqa: PLC0415

    if kind not in KINDS:
        raise ToolRefused(f"kind must be one of {KINDS}")
    slug = _norm(revises or name)
    if not NAME_RE.match(slug):
        raise ToolRefused("a tool name is 3-64 letters, digits or dashes")
    roles = sorted(set(roles))
    if not roles or any(r not in ROLES for r in roles):
        raise ToolRefused(f"roles must be a non-empty subset of {ROLES}")
    if _existing_refs(db, occurrences) < MIN_OCCURRENCES:
        raise ToolRefused(f"a tool is for a recurring problem: name at least {MIN_OCCURRENCES} earlier runs, tickets, candidates or proposals where it occurred")
    paths = _check_paths(repo_root, toolkit_paths) if kind == "toolkit" else []
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = db.execute(text("SELECT COUNT(*) FROM society_tools WHERE proposed_by_agent_id = :a AND created_at >= :t"), {"a": agent_id, "t": day}).scalar() or 0
    if n >= PER_AGENT_PER_DAY:
        raise ToolRefused(f"{n} tool(s) proposed by this agent today (at most {PER_AGENT_PER_DAY})")
    live = db.execute(text("SELECT id, version, status FROM society_tools WHERE name = :n AND status IN ('experimental', 'adopted') ORDER BY version DESC"),
                      {"n": slug}).mappings().all()
    if revises is None and live:
        raise ToolRefused(f"tool {slug!r} exists: REVISE_TOOL writes its next version")
    if revises is not None:
        if not live:
            raise ToolRefused(f"no live tool named {slug!r} to revise")
        if any(r["status"] == "experimental" for r in live):
            raise ToolRefused(f"tool {slug!r} already has an experimental version on trial: wait for its verdict")
    if (db.execute(text("SELECT COUNT(*) FROM society_tools WHERE status = 'experimental'")).scalar() or 0) >= MAX_EXPERIMENTAL:
        raise ToolRefused(f"{MAX_EXPERIMENTAL} tools are already on trial (the experiment portfolio is full)")
    parent = live[0] if revises is not None else None
    version = int(db.execute(text("SELECT COALESCE(MAX(version), 0) FROM society_tools WHERE name = :n"), {"n": slug}).scalar() or 0) + 1
    creds = tuple(c for c in credentials if c)
    clean_body, _ = redact(body[:BODY_MAX], "body", credentials=creds)
    clean_problem, _ = redact(problem[:PROBLEM_MAX], "problem", credentials=creds)
    tool_id = uuid.uuid4()
    db.execute(text(
        "INSERT INTO society_tools (id, name, version, parent_id, kind, roles, problem, body, toolkit_paths, occurrences, proposed_by_agent_id) "
        "VALUES (:id, :n, :v, :p, :k, CAST(:r AS JSONB), :pr, :b, CAST(:tp AS JSONB), CAST(:o AS JSONB), :a)"),
        {"id": tool_id, "n": slug, "v": version, "p": parent["id"] if parent else None, "k": kind, "r": json.dumps(roles), "pr": clean_problem,
         "b": clean_body, "tp": json.dumps(paths), "o": json.dumps([str(o) for o in dict.fromkeys(occurrences)]), "a": agent_id})
    return {"tool_id": str(tool_id), "name": slug, "version": version, "parent_id": str(parent["id"]) if parent else None}


# ── experiment: who sees what ───────────────────────────────────────────


def assign(db: Session, *, role: str, correlation_id: Any) -> List[Dict[str, Any]]:
    """The tools a run of ``role`` in story ``correlation_id`` is measured on: ``[{id, name,
    version, status, arm}]`` (``treatment`` = shown). Deterministic for a given library state."""
    rows = db.execute(text(
        "SELECT t.id, t.name, t.version, t.status, t.parent_id, t.created_at, "
        "       (SELECT COUNT(*) FILTER (WHERE x.success) FROM society_tool_trials x WHERE x.tool_id = t.id AND x.arm = 'treatment') AS wins "
        "FROM society_tools t WHERE t.status IN ('experimental', 'adopted') AND t.roles ? :r "
        "ORDER BY (t.status = 'adopted') DESC, wins DESC, t.created_at, t.id"), {"r": role}).mappings().all()
    challenger = {r["parent_id"] for r in rows if r["status"] == "experimental" and r["parent_id"]}
    out: List[Dict[str, Any]] = []
    shown = 0
    for r in rows:
        b = _bucket(r["id"], correlation_id)
        if r["status"] == "experimental":
            arm = "treatment" if b < 50 else "control"
        elif r["id"] in challenger:
            continue  # its revision is on trial: the revision's arm decides which version this story sees
        else:
            arm = "control" if b < HOLDOUT_PCT else "treatment"
        if arm == "treatment":
            if shown >= MAX_SHOWN:
                continue  # not shown and not measured: the cap would confound it
            shown += 1
        out.append({"id": str(r["id"]), "name": r["name"], "version": int(r["version"]), "status": r["status"], "arm": arm,
                    "parent_id": str(r["parent_id"]) if r["parent_id"] else None})
    return out


def _shown_ids(assignment: Sequence[Dict[str, Any]]) -> List[str]:
    """Shown = treatment tools, plus the adopted parent of a revision that drew control."""
    ids = [a["id"] for a in assignment if a["arm"] == "treatment"]
    ids += [a["parent_id"] for a in assignment if a["arm"] == "control" and a["status"] == "experimental" and a.get("parent_id")]
    return list(dict.fromkeys(ids))


def tool_texts(db: Session, ids: Sequence[str]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    rows = db.execute(text("SELECT id, name, version, kind, status, problem, body, toolkit_paths FROM society_tools WHERE id = ANY(CAST(:ids AS uuid[])) "
                           "ORDER BY (status = 'adopted') DESC, name"), {"ids": list(ids)}).mappings().all()
    return [{"name": r["name"], "version": int(r["version"]), "kind": r["kind"], "status": r["status"], "problem": r["problem"], "steps": r["body"],
             **({"toolkit_paths": list(r["toolkit_paths"] or [])} if r["kind"] == "toolkit" else {})} for r in rows]


def library(db: Session, limit: int = 12) -> List[Dict[str, Any]]:
    """Structural view of the live library (no text): what exists, so a role revises instead of duplicating."""
    rows = db.execute(text(
        "SELECT t.name, t.version, t.kind, t.status, t.roles, "
        "       COUNT(x.id) FILTER (WHERE x.arm = 'treatment') AS uses, COUNT(x.id) FILTER (WHERE x.arm = 'treatment' AND x.success) AS wins "
        "FROM society_tools t LEFT JOIN society_tool_trials x ON x.tool_id = t.id WHERE t.status IN ('experimental', 'adopted') "
        "GROUP BY t.id ORDER BY (t.status = 'adopted') DESC, uses DESC, t.name LIMIT :n"), {"n": limit}).mappings().all()
    return [{"name": r["name"], "version": int(r["version"]), "kind": r["kind"], "status": r["status"], "roles": list(r["roles"] or []),
             "uses": int(r["uses"]), "wins": int(r["wins"])} for r in rows]


def recurring(db: Session, now: datetime) -> Dict[str, Any]:
    """What keeps going wrong (structural counts, >= 2): the raw material for a tool."""
    since = now - timedelta(days=RECURRING_DAYS)
    from . import rejections  # noqa: PLC0415

    rej = {k: v for k, v in rejections.breakdown(db, since)["by_reason"].items() if v >= 2}
    failed = db.execute(text(
        "SELECT intent_type, COUNT(*) FROM agent_intents WHERE created_at >= :t AND execution_status IN ('failed', 'denied') "
        "GROUP BY intent_type HAVING COUNT(*) >= 2 ORDER BY COUNT(*) DESC, intent_type LIMIT 6"), {"t": since}).all()
    return {"window_days": RECURRING_DAYS, "rejected_candidates_by_reason": rej, "refused_intents_by_type": {t: int(n) for t, n in failed}}


GUIDE = ("Reusable tools the company built and is measuring. A tool is advisory: it never overrides the laws, your grant, "
         "files_allowed, the tests or a budget. When the same kind of problem recurs (see 'recurring'), write the method down once "
         "with PROPOSE_TOOL so every later run starts from it; improve a live one with REVISE_TOOL. Tools are adopted or "
         "retired by measured outcomes of later runs, never by what anyone says about them.")


def context_view(db: Session, *, role: str, correlation_id: Any, now: datetime) -> tuple:
    """(block for AgentContext.toolbox, assignment to persist on the run)."""
    assignment = assign(db, role=role, correlation_id=correlation_id)
    block = {"trust": "society_toolbox", "guide": GUIDE, "tools": tool_texts(db, _shown_ids(assignment)),
             "library": library(db), "recurring": recurring(db, now)}
    return block, [{"id": a["id"], "arm": a["arm"]} for a in assignment]


def builder_tools(db: Session, run_summary: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The tools the Builder run's story was assigned (same arm as its decision): advisory text for the coding harness."""
    trials = (run_summary or {}).get("toolbox_trials") or []
    rows = assign_from_trials(db, trials)
    return tool_texts(db, _shown_ids(rows))


def assign_from_trials(db: Session, trials: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ids = [t["id"] for t in trials if t.get("id")]
    if not ids:
        return []
    meta = {str(r["id"]): r for r in db.execute(text("SELECT id, status, parent_id FROM society_tools WHERE id = ANY(CAST(:ids AS uuid[]))"),
                                                {"ids": ids}).mappings().all()}
    return [{"id": t["id"], "arm": t["arm"], "status": meta[t["id"]]["status"],
             "parent_id": str(meta[t["id"]]["parent_id"]) if meta[t["id"]]["parent_id"] else None} for t in trials if t["id"] in meta]


# ── evaluate: trials and the verdict ────────────────────────────────────


def outcome(db: Session, run_id: Any) -> Optional[bool]:
    """None = not a trial (no side effect); True = every side-effecting intent EXECUTED."""
    rows = db.execute(text("SELECT intent_type, execution_status FROM agent_intents WHERE run_id = :r"), {"r": run_id}).all()
    side = [str(getattr(s, "value", s)) for t, s in rows if str(t) not in SIDE_EFFECT_FREE]
    if not side:
        return None
    return all(s == "executed" for s in side)


def record_run(db: Session, run: Any, *, min_trials: int, now: datetime) -> Dict[str, Any]:
    """Turn one COMPLETED run into trials of the tools its context was assigned, then re-judge
    those tools. Idempotent per (tool, run). The caller commits."""
    trials = ((run.context_summary or {}).get("toolbox_trials")) or []
    res = outcome(db, run.id) if trials else None
    out = {"trials": 0, "verdicts": {}}
    if res is None:
        return out
    for t in trials:
        made = db.execute(text(
            "INSERT INTO society_tool_trials (id, tool_id, run_id, role, arm, success, cost_usd) "
            "SELECT :id, :tool, :run, :role, :arm, :ok, :cost WHERE EXISTS (SELECT 1 FROM society_tools WHERE id = :tool) "
            "ON CONFLICT (tool_id, run_id) DO NOTHING"),
            {"id": uuid.uuid4(), "tool": t["id"], "run": run.id, "role": run.role, "arm": t["arm"], "ok": res, "cost": Decimal(str(run.cost_usd or 0))})
        out["trials"] += made.rowcount or 0
    for t in trials:
        verdict = evaluate(db, t["id"], min_trials=min_trials, now=now)
        if verdict:
            out["verdicts"][t["id"]] = verdict
    return out


def _arm_stats(db: Session, tool_id: Any, since: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    rows = db.execute(text(
        "SELECT arm, COUNT(*), COUNT(*) FILTER (WHERE success), COALESCE(AVG(cost_usd), 0) FROM society_tool_trials "
        "WHERE tool_id = :t AND (CAST(:s AS timestamptz) IS NULL OR created_at >= CAST(:s AS timestamptz)) GROUP BY arm"),
        {"t": tool_id, "s": since}).all()
    stats = {"treatment": {"n": 0, "rate": 0.0, "cost": Decimal("0")}, "control": {"n": 0, "rate": 0.0, "cost": Decimal("0")}}
    for arm, n, wins, cost in rows:
        stats[arm] = {"n": int(n), "rate": (int(wins) / int(n)) if n else 0.0, "cost": Decimal(str(cost))}
    return stats


def judge(stats: Dict[str, Dict[str, Any]], *, status: str, min_trials: int) -> Optional[tuple]:
    """Pure verdict: (new_status, reason) or None (keep measuring)."""
    t, c = stats["treatment"], stats["control"]
    enough = t["n"] >= min_trials and c["n"] >= min_trials
    lift = t["rate"] - c["rate"]
    if status == "experimental":
        if enough and lift >= MARGIN and t["cost"] <= c["cost"] * COST_RATIO + Decimal("0.01"):
            return "adopted", f"success {t['rate']:.0%} vs {c['rate']:.0%} without it over {t['n']}+{c['n']} runs"
        if enough and lift <= -MARGIN:
            return "retired", f"worse than without it: {t['rate']:.0%} vs {c['rate']:.0%} over {t['n']}+{c['n']} runs"
        if t["n"] + c["n"] >= MAX_TRIALS:
            return "retired", f"inconclusive after {t['n'] + c['n']} runs ({t['rate']:.0%} vs {c['rate']:.0%})"
        return None
    if status == "adopted" and enough and lift <= -MARGIN:
        return "retired", f"regressed: {t['rate']:.0%} vs {c['rate']:.0%} holdout over {t['n']}+{c['n']} runs since adoption"
    return None


def evaluate(db: Session, tool_id: Any, *, min_trials: int, now: datetime) -> Optional[str]:
    row = db.execute(text("SELECT id, status, parent_id, decided_at FROM society_tools WHERE id = :t FOR UPDATE"), {"t": tool_id}).mappings().first()
    if row is None or row["status"] not in ("experimental", "adopted"):
        return None
    since = row["decided_at"] if row["status"] == "adopted" else None
    verdict = judge(_arm_stats(db, tool_id, since), status=row["status"], min_trials=min_trials)
    if verdict is None:
        return None
    new, reason = verdict
    db.execute(text("UPDATE society_tools SET status = :s, status_reason = :r, decided_at = :now WHERE id = :t"),
               {"s": new, "r": reason[:200], "now": now, "t": tool_id})
    if new == "adopted" and row["parent_id"]:
        db.execute(text("UPDATE society_tools SET status = 'superseded', status_reason = :r, decided_at = :now WHERE id = :p AND status = 'adopted'"),
                   {"r": f"superseded by {tool_id}"[:200], "now": now, "p": row["parent_id"]})
    return new


def retire(db: Session, tool_id: uuid.UUID, *, reason: str, now: datetime) -> Dict[str, Any]:
    """An operator retires a tool (a bad tool can always be switched off by hand)."""
    row = db.execute(text("UPDATE society_tools SET status = 'retired', status_reason = :r, decided_at = :now "
                          "WHERE id = :t AND status IN ('experimental', 'adopted') RETURNING id, name, version"),
                     {"r": ("operator: " + reason)[:200], "now": now, "t": tool_id}).mappings().first()
    if row is None:
        raise KeyError("no live tool with this id")
    return {"id": str(row["id"]), "name": row["name"], "version": int(row["version"]), "status": "retired"}


def operator_view(db: Session, limit: int = 100) -> Dict[str, Any]:
    """Every tool with its text and both arms' numbers (model-authored text: operator only)."""
    rows = db.execute(text("SELECT * FROM society_tools ORDER BY name, version DESC LIMIT :n"), {"n": limit}).mappings().all()
    tools = []
    for r in rows:
        s = _arm_stats(db, r["id"])
        tools.append({**{k: (str(v) if isinstance(v, (uuid.UUID, Decimal)) else v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()},
                      "trials": {arm: {"n": v["n"], "success_rate": round(v["rate"], 3), "avg_cost_usd": str(v["cost"])} for arm, v in s.items()}})
    counts = {s: sum(1 for t in tools if t["status"] == s) for s in STATUSES}
    reuse = int(db.execute(text("SELECT COUNT(*) FROM society_tool_trials WHERE arm = 'treatment'")).scalar() or 0)
    return {"tools": tools, "counts": counts, "reuses": reuse}


__all__ = ["KINDS", "MARGIN", "MAX_SHOWN", "ToolRefused", "assign", "builder_tools", "context_view", "evaluate", "judge", "library",
           "operator_view", "outcome", "propose", "record_run", "recurring", "retire", "tool_texts"]
