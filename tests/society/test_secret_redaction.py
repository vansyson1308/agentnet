"""Key-shaped strings never reach a persisted intent row (secret_redaction.py), and the audit
says where one is without printing it (phase5_live X01).

Staging 2026-10-27: audit X01 counted 1 key-shaped string in agent_intents. A model writes free
text (a design, an SDK example) and a placeholder ``sk-…`` in it is indistinguishable from a leak
by shape alone; so the worker redacts every key-/JWT-shaped string in an intent row before it is
written, and tells a real credential (configured or on the compromised list) from a placeholder by
fingerprint -- the text itself is never logged, returned or stored."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import pathlib
import uuid

from services.registry.app.models import AgentIntent, AgentRun
from services.registry.app.society import canary, secret_redaction
from services.registry.app.society.events import emit_event
from services.registry.app.society.seed import seed_society

from .test_builder_decline import _intent_of, _run_as

# Built at runtime so no key-shaped literal is ever committed (tests/test_no_hardcoded_secrets.py).
PLACEHOLDER = "s" + "k-" + "live" + "X" * 24
REAL = "s" + "k-" + "ant-api03-" + "R" * 30
JWT = "ey" + "J" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12


def _phase5():
    spec = importlib.util.spec_from_file_location("phase5_live_secret", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    p5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p5)
    return p5


def test_redact_walks_the_payload_and_reports_only_structure():
    payload = {"description": f"client = Client(api_key='{PLACEHOLDER}')", "files": ["src/a.py", f"Bearer {JWT}"], "id": "task-0123456789abcdefghij", "n": 3}
    clean, found = secret_redaction.redact(payload, "payload")
    text = json.dumps(clean)
    assert PLACEHOLDER not in text and JWT not in text
    assert "[redacted-key]" in clean["description"] and clean["files"][1] == "Bearer [redacted-jwt]"
    assert clean["id"] == "task-0123456789abcdefghij" and clean["n"] == 3, "ids such as task-… are not keys"
    assert [f.to_dict() for f in found] == [
        {"field": "payload.description", "class": "key", "credential_match": False},
        {"field": "payload.files[1]", "class": "jwt", "credential_match": False},
    ]
    assert PLACEHOLDER not in repr(found)
    assert secret_redaction.redact(clean, "payload") == (clean, []), "idempotent: the marker is not a shape"


def test_a_configured_credential_or_a_compromised_one_is_told_from_a_placeholder():
    _, (f,) = secret_redaction.redact(f"key {REAL}", "x", credentials=(REAL,))
    assert f.credential_match
    _, (f,) = secret_redaction.redact(f"key {PLACEHOLDER}", "x", credentials=(REAL,))
    assert not f.credential_match
    # the denylist check is kept: every compromised fingerprint counts as a real credential
    assert canary.COMPROMISED_CREDENTIAL_FINGERPRINTS <= secret_redaction._known(())


def test_the_audit_copy_of_the_compromised_list_is_identical():
    assert _phase5().COMPROMISED_CREDENTIAL_FINGERPRINTS == canary.COMPROMISED_CREDENTIAL_FINGERPRINTS


def test_a_model_written_key_never_reaches_the_intent_row_and_a_real_one_says_rotate(db, SessionLocal, society_settings, grants_with_no_cooldown, caplog):
    seed_society(db)
    grants_with_no_cooldown()
    settings = dataclasses.replace(society_settings, model_api_key=REAL)
    content = f"SDK example: AgentNet(api_key='{PLACEHOLDER}'); leaked: {REAL}"
    with caplog.at_level(logging.WARNING):
        ev = _run_as(db, SessionLocal, settings, "scout", [{"type": "SEND_MESSAGE", "payload": {"title": "how to hire", "content": content}}])
    row = _intent_of(db, ev)
    stored = json.dumps({"payload": row.payload, "result": row.result, "reason": row.policy_reason, "error": row.error})
    assert PLACEHOLDER not in stored and REAL not in stored
    assert row.payload["content"] == "SDK example: AgentNet(api_key='[redacted-key]'); leaked: [redacted-key]"
    logs = [r for r in caplog.records if "secret.redacted" in r.getMessage()]
    assert all(PLACEHOLDER not in r.getMessage() and REAL not in r.getMessage() for r in caplog.records)
    assert {(r.levelno, "credential_match=True" in r.getMessage()) for r in logs} == {(logging.WARNING, False), (logging.ERROR, True)}
    assert all("type=SEND_MESSAGE role=scout field=payload.content class=key" in r.getMessage() for r in logs)


def test_an_invalid_intent_is_redacted_too(db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    ev = _run_as(db, SessionLocal, society_settings, "scout", [{"type": "SEND_MESSAGE", "payload": {"title": "t", "content": PLACEHOLDER, "bogus": PLACEHOLDER}}])
    row = _intent_of(db, ev)
    assert row.policy_decision.value == "invalid"
    assert PLACEHOLDER not in json.dumps({"payload": row.payload, "reason": row.policy_reason})


def test_the_audit_locates_a_key_shaped_string_without_printing_it(db, engine, make_agent):
    p5 = _phase5()
    agent = make_agent("x01-architect")
    ev = emit_event(db, event_type="t.x01", payload={})
    run = AgentRun(id=uuid.uuid4(), agent_id=agent.id, event_id=ev.id, role="architect", status="completed", correlation_id=uuid.uuid4())
    db.add(run)
    db.flush()
    db.add(AgentIntent(id=uuid.uuid4(), run_id=run.id, agent_id=agent.id, seq=0, intent_type="PROPOSE_DESIGN", idempotency_key=f"t-{uuid.uuid4()}",
                       payload={"approach": f"use {PLACEHOLDER}", "proof": f"and {REAL}"}, risk_class="low", policy_decision="allow", execution_status="executed", result={}))
    db.commit()
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            out = p5.secret_locations(cur, None, env={"SOCIETY_MODEL_API_KEY": REAL})
        conn.rollback()
    finally:
        conn.close()
    assert PLACEHOLDER not in json.dumps(out) and REAL not in json.dumps(out)
    assert out["credentials_checked"] == ["SOCIETY_MODEL_API_KEY", "compromised_list"]
    assert sorted((x["intent_type"], x["role"], x["field"], x["class"], x["credential_match"]) for x in out["locations"]) == [
        ("PROPOSE_DESIGN", "architect", "payload.approach", "key", "none"),
        ("PROPOSE_DESIGN", "architect", "payload.proof", "key", "SOCIETY_MODEL_API_KEY"),
    ]
