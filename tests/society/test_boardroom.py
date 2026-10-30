"""The Telegram boardroom, step 1: a read-only daily digest to ONE operator chat (society/boardroom.py).

Owner 2026-10-27 (G2): the daily report, the plan, the owner actions and READY/PR links; operator-only
chat; structural text only; no model-authored text forwarded without the intent-row redaction; owner
commands only through the existing operator API (the digest decides nothing)."""

from __future__ import annotations

import json
import pathlib
import re
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text

from services.registry.app.society import boardroom, boardroom_telegram, owner_actions
from services.registry.app.society.config import SocietyConfigError, SocietySettings
from services.registry.app.society.seed import seed_society

from .conftest import auth
from .test_promotion_queue import _promote, _ready
from .test_redelivery import _settings

KEY = "s" + "k-" + "boardroom" + "Q" * 20  # built at runtime: no key-shaped literal in git
SOCIETY = pathlib.Path(__file__).resolve().parents[2] / "services/registry/app/society"
NOON = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def _world(db, report):
    shipped = _ready(db, report, "O5 register landing")
    promo = _promote(db, report, shipped)
    db.execute(text("UPDATE code_promotions SET external_pr_url = 'https://github.com/o/r/pull/115' WHERE id = :p"), {"p": promo.id})
    waiting = _ready(db, report, f"O4 quote\nand accept {KEY} " + "x" * 200)
    waiting.qa_report = {"summary": "QA-RATIONALE-SENTINEL"}
    waiting.spec = {**waiting.spec, "description": "SPEC-TEXT-SENTINEL"}
    pid = uuid.uuid4()
    db.execute(text("INSERT INTO society_daily_plans (id, plan_date, status, ranking) VALUES (:i, :d, 'awaiting_owner', CAST(:r AS JSONB))"),
               {"i": pid, "d": NOON.date(), "r": json.dumps([{"ticket_id": str(uuid.uuid4()), "title": "Quote a paid USDC task", "objective_id": "O4",
                                                             "department": "product", "risk": "judged_from_diff"}])})
    owner_actions._insert(db, key="catalog:launch-post", item=owner_actions.CATALOG["O5"][1], objective_id="O5", source="catalog")
    owner_actions._insert(db, key="agent:x", item={"title": f"Email partners with {KEY}", "reason": "REASON-SENTINEL", "expected_effect": "e"},
                          objective_id="O4", source="agent")
    db.commit()
    return shipped, waiting, pid


def test_the_digest_is_structural_and_redacts_what_a_model_wrote(db, society_settings, monkeypatch):
    report = seed_society(db)
    settings = _settings(monkeypatch, SOCIETY_PROMOTION_PROVIDER="fake")
    shipped, waiting, pid = _world(db, report)
    out = boardroom.digest(db, settings, NOON)
    assert out.startswith("AgentNet boardroom 2026-10-27 (UTC)")
    assert f"Plan {NOON.date()}: awaiting_owner, 1 ticket(s)" in out and f"/v1/society/company/plans/{pid}/approve|reject" in out
    assert f"{str(shipped.id)[:8]} O5 register landing -- PR https://github.com/o/r/pull/115 (pr_open)" in out
    assert f"{str(waiting.id)[:8]} O4 quote and accept [redacted-key]" in out and "no promotion; waiting: awaiting_request" in out
    assert "Owner actions open: 2" in out and "Post the AgentNet launch" in out and "Email partners with [redacted-key] (agent," in out
    assert KEY not in out and not re.search(r"x{90}", out), "redacted and bounded"
    for sentinel in ("QA-RATIONALE-SENTINEL", "SPEC-TEXT-SENTINEL", "REASON-SENTINEL"):
        assert sentinel not in out, "only titles a model wrote are forwarded, never rationales"
    assert all(len(line) < 300 for line in out.splitlines()) and len(out) <= boardroom.DIGEST_MAX


def test_the_digest_is_sent_once_a_day_after_its_hour_and_a_failure_waits_an_hour(db, society_settings, monkeypatch):
    seed_society(db)
    sent = []

    def send(body):
        sent.append(body)
        return {"ok": len(sent) > 1, "status": 200 if len(sent) > 1 else 502}

    off = _settings(monkeypatch)
    assert boardroom.maybe_send_digest(db, off, send, NOON) is None and not sent, "off by default"
    on = _settings(monkeypatch, SOCIETY_TELEGRAM_ENABLED="true", SOCIETY_TELEGRAM_CHAT_ID="123456", SOCIETY_TELEGRAM_DIGEST_HOUR_UTC=13)
    assert boardroom.maybe_send_digest(db, on, send, NOON) is None, "before the digest hour"
    one = NOON.replace(hour=13)
    assert boardroom.maybe_send_digest(db, on, send, one)["ok"] is False
    assert boardroom.maybe_send_digest(db, on, send, one.replace(minute=30)) is None and len(sent) == 1, "a failure waits an hour"
    assert boardroom.maybe_send_digest(db, on, send, one.replace(hour=14))["ok"] is True
    assert boardroom.maybe_send_digest(db, on, send, one.replace(hour=20)) is None and len(sent) == 2, "once a day"
    rows = db.execute(text("SELECT event_type, payload FROM society_events WHERE event_type LIKE 'boardroom.%' ORDER BY created_at")).all()
    assert [r[0] for r in rows] == ["boardroom.digest_failed", "boardroom.digest_sent"] and set(rows[1][1]) == {"chars", "status", "forced"}


def test_the_bot_token_is_read_in_one_module_and_never_leaves_it(monkeypatch):
    hits = [f.name for f in SOCIETY.rglob("*.py") if "SOCIETY_TELEGRAM_BOT_TOKEN" in f.read_text(encoding="utf-8")]
    assert hits == ["boardroom_telegram.py"], hits
    token = "123456:" + "T" * 30
    monkeypatch.setenv("SOCIETY_TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("SOCIETY_TELEGRAM_CHAT_ID", "-100777")
    settings = SocietySettings()
    calls = []

    def ok(url, json, timeout):
        calls.append((url, json))
        return httpx.Response(200, json={"ok": True, "result": {"echo": token}})

    monkeypatch.setattr(boardroom_telegram.httpx, "post", ok)
    assert boardroom_telegram.send(settings, "hello") == {"ok": True, "status": 200}
    assert calls == [(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": "-100777", "text": "hello", "disable_web_page_preview": True})]

    def boom(url, json, timeout):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(boardroom_telegram.httpx, "post", boom)
    res = boardroom_telegram.send(settings, "hello")
    assert res == {"ok": False, "status": "transport error: ConnectError"} and token not in str(res)
    monkeypatch.delenv("SOCIETY_TELEGRAM_BOT_TOKEN")
    assert boardroom_telegram.send(settings, "x") == {"ok": False, "status": "no bot token configured"}


def test_the_chat_must_be_named_and_the_operator_api_is_operator_only(db, api_client, user_token, monkeypatch, society_settings):
    with pytest.raises(SocietyConfigError, match="SOCIETY_TELEGRAM_CHAT_ID"):
        _settings(monkeypatch, SOCIETY_TELEGRAM_ENABLED="true", SOCIETY_TELEGRAM_CHAT_ID="")
    monkeypatch.setenv("SOCIETY_TELEGRAM_ENABLED", "false")
    seed_society(db)
    _, member = user_token(None)
    _, op = user_token("operator")
    assert api_client.get("/v1/society/company/boardroom/digest", headers=auth(member)).status_code == 403
    _settings(monkeypatch)
    got = api_client.get("/v1/society/company/boardroom/digest", headers=auth(op))
    assert got.status_code == 200 and got.json()["text"].startswith("AgentNet boardroom") and got.json()["enabled"] is False
    assert api_client.post("/v1/society/company/boardroom/digest", headers=auth(member)).status_code == 403
    assert api_client.post("/v1/society/company/boardroom/digest", headers=auth(op)).status_code == 409, "refused while the boardroom is off"
    _settings(monkeypatch, SOCIETY_TELEGRAM_ENABLED="true", SOCIETY_TELEGRAM_CHAT_ID="42")
    monkeypatch.setattr(boardroom_telegram, "send", lambda settings, body: {"ok": True, "status": 200})
    sent = api_client.post("/v1/society/company/boardroom/digest", headers=auth(op))
    assert sent.status_code == 200 and sent.json()["ok"] is True
