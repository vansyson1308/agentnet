"""Public agent directories (a2a/federation/directory.py): find other people's A2A agents once a
day and ask to be listed once a week -- off unless configured, bounded, untrusted, structural."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from services.registry.app.a2a.federation import catalog, directory
from services.registry.app.a2a.federation.fetcher import CardFetchError
from services.registry.app.a2a.orm import A2ARemoteAgent
from services.registry.app.models import SocietyEvent

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
LISTING = {"agents": [
    {"name": "weather", "wellKnownURI": "https://weather.example.org/.well-known/agent-card.json"},
    {"name": "base url only", "url": "https://translate.example.net"},
    {"name": "duplicate", "agent_card_url": "https://weather.example.org/.well-known/agent-card.json"},
    {"name": "plain http", "wellKnownURI": "http://insecure.example.com/.well-known/agent-card.json"},
    {"name": "credentials", "wellKnownURI": "https://user:pw@evil.example.com/card.json"},
    {"name": "endpoint path, no card", "url": "https://api.example.com/a2a/v1"},
    "not an object",
    {"name": "broken", "wellKnownURI": "https://broken.example.com/.well-known/agent-card.json"},
    {"name": "over budget", "wellKnownURI": "https://late.example.com/.well-known/agent-card.json"},
]}


def test_only_https_card_urls_are_taken_from_an_untrusted_listing():
    assert directory.card_urls(LISTING) == [
        "https://weather.example.org/.well-known/agent-card.json",
        "https://translate.example.net/.well-known/agent-card.json",
        "https://broken.example.com/.well-known/agent-card.json",
        "https://late.example.com/.well-known/agent-card.json",
    ]
    assert directory.card_urls([{"cardUrl": "https://a.example.com/card.json"}]) == ["https://a.example.com/card.json"]
    assert directory.card_urls("<html>") == [] and directory.card_urls({"agents": "nope"}) == []


def _configure(monkeypatch, **env):
    monkeypatch.setenv("A2A_FEDERATION_ENABLED", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_off_unless_configured(monkeypatch, SessionLocal):
    monkeypatch.delenv("A2A_DIRECTORY_URLS", raising=False)
    monkeypatch.setenv("A2A_FEDERATION_ENABLED", "true")
    assert asyncio.run(directory.scan(SessionLocal, NOW)) is None
    monkeypatch.setenv("A2A_DIRECTORY_URLS", "https://a2aregistry.org/api/agents")
    monkeypatch.setenv("A2A_FEDERATION_ENABLED", "false")
    assert asyncio.run(directory.scan(SessionLocal, NOW)) is None
    monkeypatch.delenv("A2A_DIRECTORY_REGISTER_URLS", raising=False)
    assert asyncio.run(directory.publish_self(SessionLocal, NOW)) is None


def test_a_daily_scan_adds_new_agents_as_discovered_within_budget_and_refreshes_stale_cards(db, SessionLocal, monkeypatch):
    _configure(monkeypatch, A2A_DIRECTORY_URLS="https://a2aregistry.org/api/agents,https://down.example.com/agents", A2A_DIRECTORY_MAX_NEW_PER_DAY="2")
    stale = A2ARemoteAgent(id=uuid.uuid4(), card_url="https://old.example.com/.well-known/agent-card.json", host="old.example.com", state="discovered",
                           last_validated_at=NOW - timedelta(days=5), provenance={})
    known = A2ARemoteAgent(id=uuid.uuid4(), card_url="https://weather.example.org/.well-known/agent-card.json", host="weather.example.org", state="verified",
                           last_validated_at=NOW, provenance={})
    db.add_all([stale, known])
    db.commit()

    async def get_json(url, client=None):
        if "down" in url:
            raise CardFetchError("http_503", "down")
        return LISTING

    discovered, refreshed = [], []

    async def discover(sf, url, provenance):
        if "broken" in url:
            raise CardFetchError("invalid_card", "bad")
        discovered.append((url, provenance["source"], provenance["directory"]))
        return {}

    async def refresh(sf, rid):
        refreshed.append(rid)
        return {}

    monkeypatch.setattr(directory, "_get_json", get_json)
    monkeypatch.setattr(catalog, "discover", discover)
    monkeypatch.setattr(catalog, "refresh", refresh)
    counts = asyncio.run(directory.scan(SessionLocal, NOW))
    assert [u for u, *_ in discovered] == ["https://translate.example.net/.well-known/agent-card.json"], "known skipped; broken and over-budget count"
    assert discovered[0][1:] == ("directory", "a2aregistry.org")
    assert counts["new"] == 1 and counts["listed"] == 4 and counts["refreshed"] == 1 and refreshed == [stale.id]
    assert counts["errors"] == {"directory_http_503": 1, "card_invalid_card": 1}
    # once a day, whatever happens next
    assert asyncio.run(directory.scan(SessionLocal, NOW + timedelta(hours=3))) is None
    rows = db.execute(text("SELECT idempotency_key, payload FROM society_events WHERE event_type = :t ORDER BY created_at"), {"t": directory.SCANNED_EVENT}).all()
    assert [k for k, _ in rows] == ["a2a.directory.scan:2026-10-13", "a2a.directory.scan:2026-10-13:done"]
    assert set(rows[1][1]) == {"directories", "listed", "new", "refreshed", "errors"}, "structural counts only"


class _Client:
    def __init__(self, status):
        self.status, self.posts = status, []

    async def post(self, url, json):
        self.posts.append((url, json))

        class R:
            status_code = self.status
        return R()

    async def aclose(self):
        pass


def test_agentnet_asks_to_be_listed_once_a_week_with_only_its_public_card_url(db, SessionLocal, monkeypatch):
    _configure(monkeypatch, A2A_DIRECTORY_REGISTER_URLS="https://a2aregistry.org/api/agents/register", A2A_SERVER_ENABLED="true",
               A2A_PUBLIC_BASE_URL="https://api.agentnet.io.vn")
    client = _Client(201)
    assert asyncio.run(directory.publish_self(SessionLocal, NOW, client=client)) == {"a2aregistry.org": "listed"}
    assert client.posts == [("https://a2aregistry.org/api/agents/register", {"wellKnownURI": "https://api.agentnet.io.vn/.well-known/agent-card.json"})]
    assert asyncio.run(directory.publish_self(SessionLocal, NOW + timedelta(days=1), client=client)) == {}, "same ISO week"
    assert len(client.posts) == 1
    assert asyncio.run(directory.publish_self(SessionLocal, NOW + timedelta(days=7), client=_Client(409))) == {"a2aregistry.org": "already_listed"}


def test_a_listing_blocked_by_this_process_settings_says_which_once_a_week(db, SessionLocal, monkeypatch):
    _configure(monkeypatch, A2A_DIRECTORY_REGISTER_URLS="https://a2aregistry.org/api/agents/register", ENVIRONMENT="production")
    monkeypatch.delenv("A2A_SERVER_ENABLED", raising=False)
    monkeypatch.delenv("A2A_PUBLIC_BASE_URL", raising=False)
    client = _Client(201)
    assert asyncio.run(directory.publish_self(SessionLocal, NOW, client=client)) is None
    assert asyncio.run(directory.publish_self(SessionLocal, NOW + timedelta(days=1), client=client)) is None
    assert client.posts == []
    rows = db.query(SocietyEvent).filter(SocietyEvent.event_type == directory.LISTED_EVENT).all()
    assert [r.payload["outcome"] for r in rows] == ["skipped_unset:A2A_SERVER_ENABLED+A2A_PUBLIC_BASE_URL"]
    assert rows[0].idempotency_key.endswith(":done")
