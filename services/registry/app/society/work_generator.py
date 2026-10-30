"""The standing work generator: the company never runs out of work (owner 2026-10-29, G1v).

At full throttle the company sat idle: the newest plan was EMPTY ("backlog empty") while the product
had pages, docs and an SDK to improve. Supply was the bottleneck, so supply is deterministic plumbing
run at every company cycle (``company.start_cycle``) -- the Scout's standing duty done by code, no
model, no event, no new authority. It tops up the PROPOSED tickets of every ACTIVE objective to
``SOCIETY_WORK_GENERATOR_MIN_BACKLOG`` (5), at most ``SOCIETY_WORK_GENERATOR_MAX_PER_CYCLE`` (10) new
tickets per cycle, each with a proof, from (``origin``):

* ``surface`` -- a public page of ``public_surface_contract.json`` whose latest trusted observation
  failed (a minor finding the monitor never escalates, or a browser rule) and that no OPEN maintenance
  incident covers (maintenance incidents never enter the innovation portfolio);
* ``directive`` -- the active directive's themes (fixed keyword map: UI, traffic, signup, agents) ->
  one small ticket per public page of the theme, for the directive's objectives. The owner's text is
  matched, never copied: titles are system-written;
* ``kr_lever`` -- every off-target key result of an active objective, every cycle (not weekly): one
  ticket per product idea of ``work_packet.METRIC_LEVERS``;
* ``docs_sdk`` -- read-only repository intelligence: SDK client methods the SDK README does not
  mention, public SDK methods without a docstring, example agents without a README;
* ``tests`` -- a pytest node id named by a promotion whose CI failed in the last 7 days (still on base);
* ``todo`` -- TODO/FIXME in owned product code (``repo_intel.search``, bounded; trusted base excluded);
* ``rescope`` -- every candidate REJECTED in the last 7 days whose ticket is closed: ONE follow-up ticket
  (source ``rescope``) on the same key result, with the rejection reason (never the same spec again).

Duplicates are refused: a ticket with the same title or the same proof that is open or was created in
the last 7 days. Every ticket still goes through the plan, the board/owner, the design review, the spec
gate, files_allowed, QA/Security and the RED/merge law: the generator only fills the queue."""

from __future__ import annotations

import json
import logging
import pathlib
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from . import charter, directives, metrics, rejections, tickets, work_packet
from .events import EventType

logger = logging.getLogger("agentnet.society.work_generator")

ORIGINS = ("surface", "directive", "kr_lever", "docs_sdk", "tests", "todo", "rescope")
DEDUP_WINDOW = timedelta(days=7)
RESCOPE_WINDOW = timedelta(days=7)
OBSERVATION_WINDOW = timedelta(hours=24)
#: rejection reasons that never get a follow-up: the owner closed it, it was already done, or busywork
NO_RESCOPE = ("closed_by_owner", "already_satisfied", "busywork")
#: the product pages the directive themes and surface tickets work on (contract items: ui, public, a 200 page)
TEMPLATES = "services/dashboard/app/templates"
#: owned product code the TODO scan reads (trusted base -- society/, maintenance/ -- is excluded)
TODO_GLOBS = ("services/dashboard/app/*.py", "sdk/python/agentnet/*.py", "examples/*.py", "services/registry/app/api/*.py")
_TODO = r"\b(TODO|FIXME)\b"
_NODE = re.compile(r"\btests/[A-Za-z0-9_/]+\.py(?:::[A-Za-z0-9_\[\]-]+)?")
#: which key results an origin's work moves, in preference order (the objective must own one)
PRODUCT_METRICS = ("verified_signups_7d", "outside_agents_joined_7d", "escrow_tasks_completed_7d", "a2a_agents_live_card", "paying_callers_30d",
                   "revenue_usd_30d", "core_journey_success_rate")
METRICS_BY_ORIGIN = {
    "surface": PRODUCT_METRICS,
    "directive": PRODUCT_METRICS,
    "docs_sdk": ("outside_agents_joined_7d", "a2a_agents_live_card", "escrow_tasks_completed_7d", "paying_callers_30d", "verified_signups_7d"),
    "tests": ("core_journey_success_rate", "usd_per_objective_pr_30d", "bench_holdout_pass_at_1"),
    "todo": ("core_journey_success_rate", "usd_per_objective_pr_30d", "outside_agents_joined_7d"),
}
DEPARTMENT_BY_ORIGIN = {"surface": "strategy_product", "directive": "strategy_product", "kr_lever": "strategy_product", "docs_sdk": "engineering",
                        "tests": "quality", "todo": "engineering"}
#: directive themes: (id, label, the change asked of each page, keywords matched in the directive, pages)
THEMES: Tuple[Tuple[str, str, str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("ui", "UI", "clearer layout and hierarchy, one obvious call to action",
     (r"\bui\b", r"\bux\b", "giao diện", "giao dien", "design", "thiết kế", "thiet ke", "layout", "đẹp", "nâng cấp"),
     ("landing", "metaverse", "marketplace", "network", "register", "login")),
    ("traffic", "Traffic", "a search title and description, a shareable preview and links to the other public pages",
     ("traffic", r"\bseo\b", "lưu lượng", "luu luong", "truy cập", "truy cap", "visitor", "marketing", "quảng bá", "quang ba", "người xem"),
     ("landing", "metaverse", "marketplace", "network")),
    ("signup", "Signup", "fewer steps to a verified account and the value of an account before the form",
     ("signup", "sign up", "sign-up", "đăng ký", "dang ky", "register", "onboarding", "người dùng mới"),
     ("register", "login", "landing")),
    ("agents", "Agents", "a short path for an outside agent to list itself and be hired",
     (r"\bagents?\b", r"\ba2a\b", "đối tác", "doi tac", "partner"),
     ("network", "marketplace")),
)
#: a directive that names no theme still gets its pages looked at
DEFAULT_THEME = "ui"


# ── evidence -> items (no DB writes) ─────────────────────────────────────


def _pages(contract: Any) -> Dict[str, Any]:
    return {it.name: it for it in contract.items if it.origin == "ui" and it.auth == "public" and it.monitor and tuple(it.initial_status) == (200,)}


def _template(repo_root: Optional[str], name: str) -> Optional[str]:
    rel = f"{TEMPLATES}/{name}.html"
    return rel if repo_root and (pathlib.Path(repo_root) / rel).is_file() else None


def _item(origin: str, title: str, proof: List[str], *, problem: str, change: str, key: str, objective_id: Optional[str] = None,
          metric_id: Optional[str] = None, levers: Iterable[Optional[str]] = (), **extra: Any) -> Dict[str, Any]:
    return {"origin": origin, "title": title[:255], "proof": proof, "problem": problem[:1000], "change": change[:1000], "key": key,
            "objective_id": objective_id, "metric_id": metric_id, "levers": [lv for lv in levers if lv], **extra}


def surface_items(db: Session, contract: Any, repo_root: Optional[str], now: datetime) -> List[Dict[str, Any]]:
    """Pages whose latest trusted observation (24 h) failed and no OPEN maintenance incident covers."""
    pages = _pages(contract)
    if not pages:
        return []
    out = []
    rows = db.execute(text(
        "SELECT DISTINCT ON (ref) ref, ok, payload, source FROM ("
        " SELECT COALESCE(payload ->> 'ref', payload ->> 'page') AS ref, ok, payload, source, observed_at FROM maintenance_observations"
        " WHERE observed_at >= :t AND (payload ->> 'ref' IN :names OR payload ->> 'page' IN :names)) o ORDER BY ref, observed_at DESC")
        .bindparams(bindparam("names", expanding=True)), {"t": now - OBSERVATION_WINDOW, "names": list(pages)}).mappings().all()
    open_refs = {str(r[0]) for r in db.execute(text("SELECT desired_state_ref FROM maintenance_incidents WHERE status = 'open'")).all()}
    for r in rows:
        name = r["ref"]
        if r["ok"] or name not in pages:
            continue
        p = r["payload"] or {}
        rules = [str(x) for x in (p.get("rules") or [])][:3]
        what = ", ".join(rules) if rules else str(p.get("failure") or "failed check")
        if name in open_refs or any(f"{name}:{rule}" in open_refs for rule in rules):
            continue  # a maintenance incident owns it (never the innovation portfolio)
        page = pages[name]
        out.append(_item("surface", f"Fix {page.path}: {what}", [f"surface:{name}"], key=f"surface:{name}:{what}",
                         problem=f"the trusted monitor's latest check of {page.path} failed ({what}, severity {p.get('severity') or 'minor'}); no incident covers it",
                         change=f"make {page.path} pass its contract check: {page.intent}", levers=[_template(repo_root, name)]))
    return out


def directive_themes(text_: str) -> List[str]:
    low = (text_ or "").lower()
    found = [tid for tid, _, _, words, _ in THEMES if any(re.search(w, low) if w.startswith(r"\b") else w in low for w in words)]
    return found or [DEFAULT_THEME]


def directive_items(db: Session, contract: Any, repo_root: Optional[str]) -> List[Dict[str, Any]]:
    """The active directive's themes -> one small ticket per page of the theme, for its objectives."""
    d = directives.active(db)
    if not d or not d.get("objective_ids"):
        return []
    pages = _pages(contract)
    short = d["id"][:8]
    out = []
    for tid in directive_themes(d["text"]):
        _, label, change, _, names = next(t for t in THEMES if t[0] == tid)
        for name in names:
            page = pages.get(name)
            if page is None:
                continue
            out.append(_item("directive", f"{label}: {page.path} -- {change} (directive {short})", [f"surface:{name}", f"directive:{short}:{tid}"],
                             key=f"directive:{short}:{tid}:{name}", objectives=list(d["objective_ids"]),
                             problem=f"the owner's directive {short} asks for {label} work; {page.path} is a public page ({page.intent})",
                             change=f"on {page.path}: {change}", levers=[_template(repo_root, name)]))
    return out


def kr_lever_items(db: Session, objs: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Every off-target KR of an active objective with product levers: one ticket per idea, every cycle."""
    out = []
    for obj in objs:
        for kr in obj["key_results"]:
            lever = work_packet.METRIC_LEVERS.get(kr["metric_id"])
            if lever is None or _gap(db, kr, now) <= 0:
                continue
            for n, idea in enumerate(lever.get("ideas") or []):
                out.append(_item("kr_lever", f"{obj['id']} {kr['metric_id']}: {idea}", [f"probe:{kr['metric_id']}", f"idea:{kr['metric_id']}:{n}"],
                                 key=f"kr_lever:{kr['metric_id']}:{n}", objective_id=obj["id"], metric_id=kr["metric_id"],
                                 problem=f"{kr['metric_id']} is off target ({lever['means']})", change=idea, levers=lever.get("levers") or []))
    return out


def docs_sdk_items(repo_root: Optional[str]) -> List[Dict[str, Any]]:
    """Read-only: SDK methods the SDK README never mentions, public SDK methods with no docstring,
    example agents without a README. Bounded; a missing file is no item, never a crash."""
    if not repo_root:
        return []
    root = pathlib.Path(repo_root)
    out: List[Dict[str, Any]] = []
    client, readme = root / "sdk/python/agentnet/client.py", root / "sdk/python/README.md"
    try:
        import ast  # noqa: PLC0415

        tree = ast.parse(client.read_text(encoding="utf-8"))
        methods = [(f.name, bool(ast.get_docstring(f))) for c in tree.body if isinstance(c, ast.ClassDef)
                   for f in c.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and not f.name.startswith("_")]
        doc = readme.read_text(encoding="utf-8") if readme.is_file() else ""
        missing = sorted(n for n, _ in methods if n not in doc)
        for i in range(0, min(len(missing), 15), 5):  # five methods per ticket: small, reviewable work
            chunk = missing[i:i + 5]
            out.append(_item("docs_sdk", f"SDK README: document {', '.join(chunk)}", [f"repo:sdk_readme:{chunk[0]}"], key=f"docs_sdk:readme:{','.join(chunk)}",
                             problem=f"{len(missing)} public SDK client method(s) are not in sdk/python/README.md",
                             change=f"a short section and example per method: {', '.join(chunk)}", levers=["sdk/python/README.md"]))
        bare = sorted(n for n, has in methods if not has)[:5]
        if bare:
            out.append(_item("docs_sdk", f"SDK: docstrings for {', '.join(bare)}", [f"repo:sdk_docstrings:{bare[0]}"], key=f"docs_sdk:doc:{','.join(bare)}",
                             problem=f"public SDK method(s) without a docstring: {', '.join(bare)}", change="a one-paragraph docstring (args, returns, errors) each",
                             levers=["sdk/python/agentnet/client.py"]))
    except (OSError, SyntaxError, ValueError):
        pass
    ex = root / "examples"
    if ex.is_dir():
        for d in sorted(p for p in ex.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))[:10]:
            if not (d / "README.md").is_file():
                out.append(_item("docs_sdk", f"Example {d.name}: a README to run it in five minutes", [f"repo:example_readme:{d.name}"],
                                 key=f"docs_sdk:example:{d.name}", problem=f"examples/{d.name} has no README", levers=[f"examples/{d.name}/README.md"],
                                 change="what it does, how to run it against staging, what a successful run prints"))
    return out


def test_items(db: Session, repo_root: Optional[str], now: datetime) -> List[Dict[str, Any]]:
    """Test node ids a promotion's failed CI named in the last 7 days, still present on base."""
    rows = db.execute(text("(SELECT failure_reason FROM code_promotions WHERE status = 'ci_failed' AND updated_at >= :t ORDER BY updated_at DESC LIMIT 20) "
                           "UNION ALL (SELECT payload::text FROM society_events WHERE event_type = :e AND created_at >= :t ORDER BY created_at DESC LIMIT 20)"),
                      {"t": now - DEDUP_WINDOW, "e": EventType.PROMOTION_CI_FAILED}).all()
    nodes: List[str] = []
    for (reason,) in rows:
        for node in _NODE.findall(reason or ""):
            if node not in nodes:
                nodes.append(node)
    out = []
    for node in nodes[:5]:
        path = node.split("::", 1)[0]
        if repo_root and not (pathlib.Path(repo_root) / path).is_file():
            continue
        out.append(_item("tests", f"Fix failing test {node}"[:255], [node], key=f"tests:{node}", problem=f"{node} failed in a promotion's CI",
                         change="make it pass on main (fix the code or the flaky setup; never skip or weaken it)", levers=[path]))
    return out


def todo_items(repo_root: Optional[str]) -> List[Dict[str, Any]]:
    if not repo_root:
        return []
    from . import repo_intel  # noqa: PLC0415

    out, seen = [], set()
    for glob in TODO_GLOBS:
        try:
            hits = repo_intel.search(pathlib.Path(repo_root), _TODO, glob=glob, regex=True, max_results=10).data.get("hits") or []
        except repo_intel.RepoReadError:
            continue
        for h in hits:
            if h["path"] in seen or len(out) >= 5:
                continue
            seen.add(h["path"])
            out.append(_item("todo", f"Resolve TODO/FIXME in {h['path']}", [f"repo:todo:{h['path']}"], key=f"todo:{h['path']}",
                             problem=f"{h['path']} carries an open TODO/FIXME (line {h['line']})", change="do it or remove it with the reason",
                             levers=[h["path"]]))
    return out


def rescope_items(db: Session, now: datetime) -> List[Dict[str, Any]]:
    """A candidate REJECTED in the last 7 days whose ticket is closed: ONE follow-up on the same KR."""
    rows = db.execute(text(
        "SELECT t.*, c.id AS cand_id, c.qa_report -> 'failures' AS qa, c.security_report ->> 'verdict' AS sec, c.error AS cerr, "
        "       (SELECT e.payload FROM society_events e WHERE e.subject_id = c.id AND e.event_type = 'code_candidate.rejected' ORDER BY e.created_at DESC LIMIT 1) AS ev "
        "FROM code_candidates c JOIN society_tickets t ON t.candidate_id = c.id "
        "WHERE c.status = 'rejected' AND c.updated_at >= :t AND t.status NOT IN :open AND t.source <> 'rescope' ORDER BY c.updated_at DESC LIMIT 20")
        .bindparams(bindparam("open", expanding=True)), {"t": now - RESCOPE_WINDOW, "open": list(tickets.OPEN_TICKET)}).mappings().all()
    out = []
    for r in rows:
        reason = rejections.classify(r["ev"], r["qa"], r["sec"], r["cerr"])
        if reason in NO_RESCOPE:
            continue
        proof = [p for p in (r["proof"] or []) if isinstance(p, str)] + [f"rescope:{str(r['cand_id'])[:8]}"]
        out.append(_item("rescope", f"Rescope: {r['title']}", proof, key=f"rescope:{r['cand_id']}", objective_id=r["objective_id"], metric_id=r["metric_id"],
                         problem=f"candidate {str(r['cand_id'])[:8]} for ticket {str(r['id'])[:8]} was rejected ({reason}); its key result still needs the work",
                         change=f"a smaller design that avoids {reason}: re-read the rejection, narrow files_allowed, keep the same proof",
                         department=r["department"], direction=r["direction"], expected_effect=float(r["expected_effect"] or 0), source="rescope"))
    return out


# ── items -> tickets ─────────────────────────────────────────────────────


def _gap(db: Session, kr: Dict[str, Any], now: datetime) -> float:
    try:
        current = metrics.read(db, kr["metric_id"], now)
    except Exception:  # noqa: BLE001 -- an unreadable metric is a gap we cannot size: no ticket
        return 0.0
    start = current if current is not None else float(kr.get("baseline") or 0)
    gap = float(kr["target"]) - float(start)
    return gap if kr["direction"] == "up" else -gap


def _fit(db: Session, item: Dict[str, Any], obj: Dict[str, Any], now: datetime) -> Optional[Tuple[Dict[str, Any], float]]:
    """(key result, expected effect) when ``obj`` can own the item, else None."""
    if item.get("objective_id") and item["objective_id"] != obj["id"]:
        return None
    if item.get("objectives") and obj["id"] not in item["objectives"]:
        return None
    krs = {k["metric_id"]: k for k in obj["key_results"]}
    if item.get("metric_id"):
        kr = krs.get(item["metric_id"])
        prefs = [kr] if kr else []
    else:
        prefs = [krs[m] for m in METRICS_BY_ORIGIN.get(item["origin"], ()) if m in krs]
    for kr in prefs:
        gap = _gap(db, kr, now)
        if gap > 0:
            effect = float(item["expected_effect"]) if item.get("expected_effect") else round(abs(gap) / 10.0, 4) or 0.0001
            return kr, effect
    return None


def _duplicate(db: Session, title: str, proof: List[str], now: datetime) -> bool:
    return db.execute(text(
        "SELECT 1 FROM society_tickets WHERE (LOWER(title) = LOWER(:ti) OR proof = CAST(:p AS JSONB)) AND (status IN :open OR created_at >= :t) LIMIT 1")
        .bindparams(bindparam("open", expanding=True)), {"ti": title, "p": json.dumps(proof), "open": list(tickets.OPEN_TICKET), "t": now - DEDUP_WINDOW}).first() is not None


def backlog_by_objective(db: Session) -> Dict[str, int]:
    return {str(r[0]): int(r[1]) for r in db.execute(text("SELECT objective_id, COUNT(*) FROM society_tickets WHERE status = 'proposed' GROUP BY 1")).all()}


def _insert(db: Session, item: Dict[str, Any], obj: Dict[str, Any], kr: Dict[str, Any], effect: float, now: datetime) -> str:
    from ..models import ImprovementProposal  # noqa: PLC0415

    key = f"{item['key']}:{now.date().isoformat()}"
    tid, pid = uuid.uuid5(tickets._NS, f"gen-ticket:{key}"), uuid.uuid5(tickets._NS, f"gen-proposal:{key}")
    levers = item.get("levers") or []
    db.add(ImprovementProposal(id=pid, source="audit", title=item["title"], problem=f"[{item['origin']}] {item['problem']}",
                               proposed_change=f"{item['change']}" + (f" -- where: {', '.join(levers[:4])}" if levers else ""),
                               status="PROPOSED", target_scope="platform", importance=60))
    db.flush()
    db.execute(text("INSERT INTO society_tickets (id, proposal_id, title, objective_id, metric_id, expected_effect, direction, proof, source, priority, department, origin) "
                    "VALUES (:id, :p, :t, :o, :m, :e, :d, CAST(:proof AS JSONB), :s, :pr, :dep, :origin)"),
               {"id": tid, "p": pid, "t": item["title"], "o": obj["id"], "m": kr["metric_id"], "e": effect, "d": item.get("direction") or kr["direction"],
                "proof": json.dumps(item["proof"]), "s": item.get("source") or "generator", "pr": 6 - int(obj["owner_priority"]),
                "dep": item.get("department") or DEPARTMENT_BY_ORIGIN.get(item["origin"], "strategy_product"), "origin": item["origin"]})
    return str(tid)


def collect(db: Session, settings: Any, now: datetime, objs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every source's items, in priority order: follow-ups of rejected work and the owner's direction first."""
    from . import surface  # noqa: PLC0415

    repo_root = getattr(settings, "repo_root", None)
    contract = surface.load_contract()
    out: List[Dict[str, Any]] = []
    for name, fn in (("rescope", lambda: rescope_items(db, now)), ("directive", lambda: directive_items(db, contract, repo_root)),
                     ("surface", lambda: surface_items(db, contract, repo_root, now)), ("tests", lambda: test_items(db, repo_root, now)),
                     ("kr_lever", lambda: kr_lever_items(db, objs, now)), ("docs_sdk", lambda: docs_sdk_items(repo_root)), ("todo", lambda: todo_items(repo_root))):
        try:
            with db.begin_nested():
                out += fn()
        except Exception:  # noqa: BLE001 -- one source failing never empties the others
            logger.exception("work generator: source %s failed", name)
    return out


def generate(db: Session, settings: Any, now: datetime) -> Dict[str, Any]:
    """Top up every ACTIVE objective's proposed tickets to MIN_BACKLOG; at most MAX_PER_CYCLE new
    tickets; duplicates refused. Returns structural counts. The caller commits."""
    objs = [o for o in charter.objectives(db) if o["status"] == "active"]
    floor, cap = int(settings.work_generator_min_backlog), int(settings.work_generator_max_per_cycle)
    have = backlog_by_objective(db)
    need = {o["id"]: max(0, floor - have.get(o["id"], 0)) for o in objs}
    report: Dict[str, Any] = {"active_objectives": len(objs), "backlog_before": {o["id"]: have.get(o["id"], 0) for o in objs}, "created": [],
                              "by_origin": {}, "duplicates": 0, "unfit": 0}
    if not objs or cap <= 0 or not any(need.values()):
        return report
    for item in collect(db, settings, now, objs):
        if len(report["created"]) >= cap or not any(need.values()):
            break
        fit = next(((o, f) for o in sorted(objs, key=lambda o: -need[o["id"]]) if need[o["id"]] > 0 for f in [_fit(db, item, o, now)] if f), None)
        if fit is None:
            report["unfit"] += 1
            continue
        obj, (kr, effect) = fit
        if _duplicate(db, item["title"], item["proof"], now):
            report["duplicates"] += 1
            continue
        report["created"].append(_insert(db, item, obj, kr, effect, now))
        report["by_origin"][item["origin"]] = report["by_origin"].get(item["origin"], 0) + 1
        need[obj["id"]] -= 1
    if report["created"]:
        logger.info("work generator: %d ticket(s) %s; backlog before %s", len(report["created"]), report["by_origin"], report["backlog_before"])
    return report


__all__ = ["ORIGINS", "THEMES", "backlog_by_objective", "collect", "directive_items", "directive_themes", "docs_sdk_items", "generate", "kr_lever_items",
           "rescope_items", "surface_items", "test_items", "todo_items"]
