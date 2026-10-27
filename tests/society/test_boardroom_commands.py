"""The Telegram boardroom, step 2: a linked operator's commands from the ONE operator chat (society/boardroom_commands.py).

Owner 2026-10-27 (G2): owner commands (approve plan, close owner action) only through the existing
operator API. A Telegram account is linked to an operator with a one-time code from that API; every
command re-checks the operator role and calls the operator routes' own functions; other chats, bots,
channels, forwards, stale and repeated updates never decide anything; the message text is never kept."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from services.registry.app.society import boardroom, boardroom_commands, boardroom_telegram, owner_actions
from services.registry.app.society.config import SocietyConfigError
from services.registry.app.society.seed import seed_society

from .conftest import auth
from .test_redelivery import _settings

CHAT = "-100777"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
OWNER, STRANGER = 555, 777
NOTE = "posted-NOTE-SENTINEL"


def _on(monkeypatch):
    return _settings(monkeypatch, SOCIETY_TELEGRAM_ENABLED="true", SOCIETY_TELEGRAM_CHAT_ID=CHAT, SOCIETY_TELEGRAM_COMMANDS_ENABLED="true")


def _world(db):
    pid = uuid.uuid4()
    db.execute(text("INSERT INTO society_daily_plans (id, plan_date, status, ranking) VALUES (:i, :d, 'awaiting_owner', CAST(:r AS JSONB))"),
               {"i": pid, "d": NOW.date(), "r": json.dumps([{"ticket_id": str(uuid.uuid4()), "title": "Quote a paid USDC task", "objective_id": "O4",
                                                             "department": "product", "risk": "judged_from_diff"}])})
    owner_actions._insert(db, key="catalog:launch-post", item=owner_actions.CATALOG["O5"][1], objective_id="O5", source="catalog")
    db.commit()
    aid = db.execute(text("SELECT id FROM society_owner_actions")).scalar()
    return pid, aid


class Chat:
    """A fake Bot API: queued updates in, replies out."""

    def __init__(self):
        self.queue, self.replies, self.offsets, self.n = [], [], [], 100

    def say(self, body, *, chat=CHAT, sender=OWNER, at=NOW, **extra):
        self.n += 1
        self.queue.append({"update_id": self.n, "chat_id": int(chat), "from_id": sender, "from_is_bot": False, "sender_chat": False, "forwarded": False,
                           "date": int(at.timestamp()), "text": body, **extra})
        return self.n

    def fetch(self, offset):
        self.offsets.append(offset)
        out = [u for u in self.queue if offset is None or u["update_id"] >= offset]
        return {"ok": True, "status": 200, "updates": out}

    def send(self, body):
        self.replies.append(body)
        return {"ok": True, "status": 200}

    def poll(self, db, settings, offset=None, now=NOW):
        before = len(self.replies)
        offset = boardroom_commands.poll(db, settings, self.fetch, self.send, offset, now)
        return offset, self.replies[before:]


def test_a_linked_operator_approves_the_plan_and_closes_an_owner_action(db, api_client, user_token, monkeypatch, society_settings):
    seed_society(db)
    settings = _on(monkeypatch)
    pid, aid = _world(db)
    op, op_token = user_token("operator")
    _, member = user_token(None)
    assert api_client.post("/v1/society/company/boardroom/link", headers=auth(member)).status_code == 403
    chat = Chat()
    chat.say(f"/approve {str(pid)[:8]}")
    off, replies = chat.poll(db, settings)
    assert replies == ["refused: this account is not linked to an operator (see /help)"]
    code = api_client.post("/v1/society/company/boardroom/link", headers=auth(op_token)).json()["code"]
    assert db.execute(text("SELECT code_sha256 FROM society_telegram_links")).scalar() != code, "only the hash is stored"
    chat.say(f"/link {code}", chat="-100999")  # another chat: dropped, unanswered, unrecorded
    chat.say(f"/link {code}", sender=OWNER)
    chat.say(f"/approve {str(pid)[:8]}", forwarded=True)
    chat.say(f"/approve {str(pid)[:8]}", at=NOW - timedelta(minutes=16))
    chat.say(f"/approve {str(pid)[:8]}", sender=STRANGER)
    chat.say("/approve 1234")
    chat.say(f"/approve {str(pid)[:8]}")
    chat.say(f"/done {str(aid)[:8]} {NOTE}")
    off, replies = chat.poll(db, settings, off)
    assert replies == ["linked", "refused: commands come from a person's own account, not a bot, a channel or a forward",
                       "refused: older than 15 minutes, send it again", "refused: this account is not linked to an operator (see /help)",
                       "refused: give the plan id (8+ characters)", f"plan {str(pid)[:8]}: approved", f"owner action {str(aid)[:8]}: done"]
    assert off == chat.n + 1
    plan = db.execute(text("SELECT status, decided_by_user_id, decided_by FROM society_daily_plans WHERE id = :i"), {"i": pid}).one()
    assert plan == ("approved", op.id, "owner"), "tickets.decide_plan, as the operator"
    action = db.execute(text("SELECT status, decided_by, note FROM society_owner_actions WHERE id = :i"), {"i": aid}).one()
    assert action == ("done", op.id, NOTE)
    # a repeated update (a restart re-reads unconfirmed ones) decides nothing twice
    _, again = chat.poll(db, settings, None)
    assert again == []
    events = db.execute(text("SELECT payload FROM society_events WHERE event_type = :t ORDER BY created_at"), {"t": boardroom_commands.COMMAND_EVENT}).scalars().all()
    assert len(events) == 8 and all(set(e) == {"command", "outcome", "target", "user_id"} for e in events)
    assert NOTE not in json.dumps(events) and code not in json.dumps(events), "the message text is never kept"
    assert [e["outcome"] for e in events][-2:] == ["approved", "done"] and events[-1]["user_id"] == str(op.id)
    status = api_client.get("/v1/society/company/boardroom/link", headers=auth(op_token)).json()
    assert status["linked"] is True and status["telegram_user_id"] == OWNER


def test_the_role_is_checked_on_every_command_and_a_code_works_once(db, api_client, user_token, monkeypatch, society_settings):
    seed_society(db)
    settings = _on(monkeypatch)
    pid, _ = _world(db)
    op, op_token = user_token("operator")
    code = boardroom_commands.issue_link_code(db, op.id, NOW)["code"]
    chat = Chat()
    chat.say(f"/link {code}")
    chat.say(f"/link {code}", sender=STRANGER)
    off, replies = chat.poll(db, settings)
    assert replies == ["linked", "refused: unknown or expired code"], "single use"
    op.society_role = None
    db.commit()
    chat.say(f"/reject {str(pid)[:8]}")
    off, replies = chat.poll(db, settings, off)
    assert replies == ["refused: this account is not linked to an operator (see /help)"], "revoking the role stops the account at once"
    op.society_role = "operator"
    db.commit()
    late = boardroom_commands.issue_link_code(db, op.id, NOW - timedelta(minutes=11))["code"]
    chat.say("/plan")
    chat.say(f"/link {late}")
    off, replies = chat.poll(db, settings, off)
    assert replies == ["refused: this account is not linked to an operator (see /help)", "refused: unknown or expired code"], \
        "a new code replaced the link, and an expired one never links"
    fresh = api_client.post("/v1/society/company/boardroom/link", headers=auth(op_token)).json()["code"]
    chat.say(f"/link {fresh}")
    chat.say("/plan")
    chat.say(f"/reject {str(pid)[:8]}")
    chat.say(f"/approve {str(pid)[:8]}")
    chat.say("/unlink")
    chat.say("/plan")
    off, replies = chat.poll(db, settings, off)
    assert replies[0] == "linked" and f"/approve {str(pid)[:8]} | /reject {str(pid)[:8]}" in replies[1]
    assert replies[2] == f"plan {str(pid)[:8]}: rejected"
    assert replies[3] == f"refused: no single plan awaiting you starts with {str(pid)[:8]}", "a decided plan is not decided again"
    assert replies[4:] == ["unlinked", "refused: this account is not linked to an operator (see /help)"]
    assert api_client.delete("/v1/society/company/boardroom/link", headers=auth(op_token)).json() == {"unlinked": False}


def test_off_by_default_and_the_flag_needs_the_boardroom(db, monkeypatch, society_settings):
    with pytest.raises(SocietyConfigError, match="SOCIETY_TELEGRAM_COMMANDS_ENABLED"):
        _settings(monkeypatch, SOCIETY_TELEGRAM_COMMANDS_ENABLED="true", SOCIETY_TELEGRAM_ENABLED="false")
    monkeypatch.setenv("SOCIETY_TELEGRAM_COMMANDS_ENABLED", "false")
    off = _settings(monkeypatch, SOCIETY_TELEGRAM_ENABLED="true", SOCIETY_TELEGRAM_CHAT_ID=CHAT)
    chat = Chat()
    chat.say("/help")
    assert boardroom_commands.poll(db, off, chat.fetch, chat.send, 7, NOW) == 7 and chat.offsets == [], "never even fetched"
    assert "/approve" not in boardroom.digest(db, off, NOW), "the digest offers commands only when they are on"


def test_updates_keep_only_command_fields_and_the_token_never_reaches_a_log(monkeypatch, caplog):
    token = "123456:" + "T" * 30
    monkeypatch.setenv("SOCIETY_TELEGRAM_BOT_TOKEN", token)
    settings = _on(monkeypatch)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 9, "message": {"message_id": 1, "date": 1, "chat": {"id": -100777, "title": "BOARD-TITLE"}, "from": {"id": OWNER, "is_bot": False, "first_name": "NAME"},
                                         "text": "/plan"}},
            {"update_id": 10, "message": {"message_id": 2, "date": 2, "chat": {"id": -100777}, "sender_chat": {"id": -100777}, "text": "/plan"}},
            {"update_id": "x"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(boardroom_telegram.httpx, "post", lambda url, **kw: client.post(url, **kw))
    with caplog.at_level(logging.DEBUG):
        got = boardroom_telegram.get_updates(settings, 9)
        boardroom_telegram.send(settings, "hello")
    assert seen[0] == {"timeout": 0, "limit": 20, "allowed_updates": ["message"], "offset": 9}
    assert got["ok"] and got["updates"] == [
        {"update_id": 9, "chat_id": -100777, "from_id": OWNER, "from_is_bot": False, "sender_chat": False, "forwarded": False, "date": 1, "text": "/plan"},
        {"update_id": 10, "chat_id": -100777, "from_id": None, "from_is_bot": False, "sender_chat": True, "forwarded": False, "date": 2, "text": "/plan"}]
    assert token not in caplog.text and "BOARD-TITLE" not in json.dumps(got)
    assert not [r for r in caplog.records if r.name.startswith("httpx") and "api.telegram.org/bot" in r.getMessage()]
