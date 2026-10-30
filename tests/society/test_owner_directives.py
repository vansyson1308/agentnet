"""The owner's directive ("Kim chỉ nam", society/directives.py): owner input every role sees, a plan
weight for its objectives, append-only, written only by an operator -- and it grants nothing."""

from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from services.registry.app.models import Agent, AgentCapabilityGrant, User
from services.registry.app.society import charter, directives, tickets
from services.registry.app.society.context import build_context
from services.registry.app.society.events import emit_event, utcnow
from services.registry.app.society.intents import PAYLOAD_MODELS, IntentType

from .conftest import auth
from .test_company_tickets import _activate, company as company  # noqa: F401 -- fixture
from .test_north_star import _ticket

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_a_new_directive_supersedes_the_active_one_and_history_is_append_only(db):
    first = directives.set_directive(db, text_="grow signups", objective_ids=["O5"], user_id=None)
    second = directives.set_directive(db, text_="  first paid USDC task on staging this week; focus on O4 ", objective_ids=["O4", "O4"], user_id=None)
    assert directives.active(db)["id"] == second["id"] != first["id"]
    assert second["text"] == "first paid USDC task on staging this week; focus on O4" and second["objective_ids"] == ["O4"]
    old = [h for h in directives.history(db) if h["id"] == first["id"]][0]
    assert old["text"] == "grow signups" and old["superseded_at"], "the old direction is kept, superseded"
    for sql in ("UPDATE society_owner_directives SET text = 'rewritten' WHERE id = :i", "DELETE FROM society_owner_directives WHERE id = :i",
                "UPDATE society_owner_directives SET superseded_at = NULL WHERE id = :i"):
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(sql), {"i": uuid.UUID(first["id"])})
        db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("INSERT INTO society_owner_directives (id, text) VALUES (:i, 'a second active one')"), {"i": uuid.uuid4()})
    db.rollback()


@pytest.mark.parametrize("body,ids,match", [("", [], "1..2000"), ("x" * 2001, [], "1..2000"), ("ok", ["O9"], "unknown objective")])
def test_a_bad_directive_is_refused(db, body, ids, match):
    with pytest.raises(ValueError, match=match):
        directives.set_directive(db, text_=body, objective_ids=ids, user_id=None)
    assert directives.active(db) is None


def test_every_role_sees_it_as_owner_input_and_it_grants_nothing(db, company):
    report, _ = company
    ev = emit_event(db, event_type="platform.metric.anomaly", payload={"metric": "x"})
    db.commit()
    contexts = {}
    for role in ("scout", "governor", "builder"):
        agent = db.query(Agent).filter(Agent.id == report.agents[role]).one()
        grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == agent.id).first()
        contexts[role] = (agent, grant)
    before = {r: build_context(db, agent=a, grant=g, event=ev, run=None, settings=company[1]) for r, (a, g) in contexts.items()}
    assert all(c.owner_directive == {} for c in before.values())
    directives.set_directive(db, text_="first paid USDC task on staging this week", objective_ids=["O4"], user_id=None)
    for role, (agent, grant) in contexts.items():
        ctx = build_context(db, agent=agent, grant=grant, event=ev, run=None, settings=company[1])
        assert ctx.owner_directive["text"] == "first paid USDC task on staging this week" and ctx.owner_directive["objective_ids"] == ["O4"]
        assert ctx.owner_directive["label"].startswith("OWNER INPUT") and "never permission" in ctx.owner_directive["label"]
        b = before[role]
        assert (ctx.permissions, ctx.restrictions, ctx.budget) == (b.permissions, b.restrictions, b.budget), f"{role}: the directive grants nothing"


def test_no_intent_can_write_the_directive():
    assert not [t for t in IntentType if "DIRECTIVE" in t.value]
    assert not [m for m in PAYLOAD_MODELS.values() if "directive" in m.__name__.lower()]
    writers = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "services/registry/app").rglob("*.py")
                     if "society_owner_directives" in p.read_text(encoding="utf-8") and p.name not in ("schema_sql.py",))
    assert writers == ["services/registry/app/society/directives.py"], writers
    routes = (ROOT / "services/registry/app/api/routes/society.py").read_text(encoding="utf-8")
    assert "directives.set_directive(" in routes and routes.count("directives.set_directive(") == 1
    block = routes[routes.index('@router.post("/company/directive"'):].split("@router.", 2)[1]
    assert "Depends(require_operator)" in block


def test_the_plan_weighs_the_directive_objectives(db, company):
    report, _ = company
    _activate(db, "O2")
    _activate(db, "O5")
    signups = _ticket(db, report, "O5", "verified_signups_7d", 15)
    escrow = _ticket(db, report, "O2", "escrow_tasks_completed_7d", 25)
    obj = {o["id"]: o for o in charter.objectives(db)}
    t_sign = tickets._ticket_ref(db, signups)
    d = directives.set_directive(db, text_="signups first", objective_ids=["O5"], user_id=None)
    assert tickets.score(t_sign, obj["O5"], directive=d) == 2 * tickets.score(t_sign, obj["O5"]) == 4.0
    plan = tickets.build_plan(db, None, utcnow())
    assert [r["ticket_id"] for r in plan["ranking"]] == [signups, escrow]
    assert plan["ranking"][0]["owner_directive"] == d["id"] and "owner_directive" not in plan["ranking"][1]
    follow = directives.following(db, d)
    assert [f["ticket_id"] for f in follow] == [], "tickets made BEFORE the directive did not come from it"
    later = _ticket(db, report, "O5", "verified_signups_7d", 5)
    _ticket(db, report, "O2", "escrow_tasks_completed_7d", 5)
    assert [f["ticket_id"] for f in directives.following(db, d)] == [later], "only its objectives, only since it was set"


def test_only_an_operator_sets_or_reads_it(db, api_client, user_token):
    _, plain = user_token(None)
    op_user, op = user_token("operator")
    body = {"text": "first paid USDC task on staging this week", "objective_ids": ["O4"]}
    assert api_client.post("/v1/society/company/directive", headers=auth(plain), json=body).status_code == 403
    assert api_client.get("/v1/society/company/directive", headers=auth(plain)).status_code == 403
    assert api_client.post("/v1/society/company/directive", json=body).status_code == 401
    r = api_client.post("/v1/society/company/directive", headers=auth(op), json=body)
    assert r.status_code == 201 and r.json()["created_by"] == str(op_user.id)
    assert api_client.post("/v1/society/company/directive", headers=auth(op), json={**body, "objective_ids": ["O9"]}).status_code == 422
    view = api_client.get("/v1/society/company/directive", headers=auth(op)).json()
    assert view["active"]["text"] == body["text"] and {o["id"] for o in view["objectives"]} >= {"O4", "O5"} and view["following"] == []


# ── the validator's owner:grant:<email> ────────────────────────────────────


def _p5():
    spec = importlib.util.spec_from_file_location("phase5_live_owner", ROOT / "deploy/railway/phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Out:
    def __init__(self):
        self.checks, self.blobs = [], {}

    def check(self, group, code, ok, detail):
        self.checks.append((code, bool(ok), detail))

    def json(self, group, name, value):
        self.blobs[name] = value


def _grant(db, engine, monkeypatch, email, **env):
    """Run the step against the test database; its operator call goes to the real role assignment."""
    from services.registry.app.society.operator_auth import assign_role

    p5 = _p5()
    for k in ("ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "OWNER_GRANT_CONFIRM"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    calls = []

    def api(method, url, token, body=None, timeout=30.0):
        calls.append((method, url, body))
        user = assign_role(db, email=body["email"], role=body["role"])
        return 200, {"email": user.email, "role": user.society_role}

    monkeypatch.setattr(p5, "api", api)
    out = _Out()
    conn = engine.raw_connection()
    try:
        p5.step_owner_grant(out, "https://api.test", "operator-jwt", conn, email)
    finally:
        conn.close()
    return out, calls


def test_owner_grant_needs_an_existing_account_and_never_touches_a_password(db, engine, make_user, monkeypatch):
    out, calls = _grant(db, engine, monkeypatch, "Owner.Person@Example.com")
    assert [c[:2] for c in out.checks] == [("G01", False)] and not calls
    assert out.blobs["grant"] == {"email": "Owner.Person", "existed": False, "verified": False, "role": None}
    assert db.query(User).filter(User.email.ilike("owner.person@example.com")).count() == 0, "never creates the account"

    user = make_user("owner.person@example.com")
    user.is_email_verified, user.password_hash = False, "the-owners-own-hash"
    db.commit()
    out, calls = _grant(db, engine, monkeypatch, "Owner.Person@Example.com")
    assert [c[:2] for c in out.checks] == [("G01", True), ("G02", True)]
    assert calls == [("POST", "https://api.test/v1/society/operators", {"email": "owner.person@example.com", "role": "operator"})]
    assert out.blobs["grant"] == {"email": "Owner.Person", "existed": True, "verified": True, "role": "operator"}
    db.expire_all()
    user = db.query(User).filter(User.id == user.id).one()
    assert (user.is_email_verified, user.society_role, user.password_hash) == (True, "operator", "the-owners-own-hash")
    assert "example.com" not in " ".join(d for _, _, d in out.checks), "only the email prefix is printed"


def test_owner_grant_never_runs_on_production_without_an_explicit_confirmation(db, engine, make_user, monkeypatch):
    make_user("boss@example.com")
    out, calls = _grant(db, engine, monkeypatch, "boss@example.com", ENVIRONMENT="production")
    assert [c[:2] for c in out.checks] == [("G00", False)] and not calls
    out, calls = _grant(db, engine, monkeypatch, "boss@example.com", RAILWAY_ENVIRONMENT_NAME="production", OWNER_GRANT_CONFIRM="boss@example.com")
    assert [c[:2] for c in out.checks] == [("G01", True), ("G02", True)]
