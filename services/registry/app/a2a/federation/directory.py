"""Public agent directories: find other people's A2A agents, and be found (deterministic, no model).

Two halves, each off unless configured, each bounded and recorded as a structural event:

* **Scout** (``A2A_DIRECTORY_URLS``): once per UTC day, read each public directory listing
  (for example ``https://a2aregistry.org/api/agents``) through the network guard, take
  the Agent Card URLs it names and add the agents AgentNet does not know yet to the
  federated catalog with ``catalog.discover`` (at most ``A2A_DIRECTORY_MAX_NEW_PER_DAY``),
  then re-validate the catalog's oldest cards (``A2A_DIRECTORY_REFRESH_PER_PASS``).
  A directory is UNTRUSTED data: only https card URLs are taken from it, every card is
  fetched and validated by the SDK through ``netguard``, and a new agent enters as
  ``discovered`` -- never ``verified``. Nothing here sends a task or a message to anyone.
* **Listing** (``A2A_DIRECTORY_REGISTER_URLS``): once per ISO week, ask each directory to
  list AgentNet by posting the public card URL built from ``A2A_PUBLIC_BASE_URL``
  (``{"wellKnownURI": ...}``). Only that URL is sent: no credential, no data.

No wallet, grant, secret or model is touched. The Society sees the outcome only as the
``a2a_agents_live_card`` key result and the structural ``a2a.directory.*`` events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import config
from ..orm import A2ARemoteAgent
from . import catalog
from .fetcher import CardFetchError, normalize_card_url
from .netguard import OutboundRefused, safe_client

logger = logging.getLogger(__name__)

CARD_PATH = "/.well-known/agent-card.json"
CARD_KEYS = ("wellKnownURI", "well_known_uri", "agentCardUrl", "agent_card_url", "cardUrl", "card_url")
LIST_KEYS = ("agents", "items", "data", "results")
MAX_ENTRIES = 200
MAX_URL = 512
DIRECTORY_MAX_BYTES = 1024 * 1024
STALE_AFTER = timedelta(days=3)
SCANNED_EVENT = "a2a.directory.scanned"
LISTED_EVENT = "a2a.directory.listed"


def _urls(name: str) -> List[str]:
    out = []
    for raw in os.getenv(name, "").split(","):
        u = raw.strip()
        if u and urlsplit(u).scheme == "https" and len(u) <= MAX_URL:
            out.append(u)
    return out


def directory_urls() -> List[str]:
    return _urls("A2A_DIRECTORY_URLS")


def register_urls() -> List[str]:
    return _urls("A2A_DIRECTORY_REGISTER_URLS")


def max_new_per_day() -> int:
    return config._bounded_int("A2A_DIRECTORY_MAX_NEW_PER_DAY", 10, 0, 50)


def refresh_per_pass() -> int:
    return config._bounded_int("A2A_DIRECTORY_REFRESH_PER_PASS", 10, 0, 50)


def card_urls(listing: Any) -> List[str]:
    """The https Agent Card URLs an (untrusted) directory listing names, deduplicated, bounded."""
    entries: Iterable[Any] = []
    if isinstance(listing, list):
        entries = listing
    elif isinstance(listing, dict):
        entries = next((listing[k] for k in LIST_KEYS if isinstance(listing.get(k), list)), [])
    out: List[str] = []
    for entry in list(entries)[:MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        url = next((entry[k] for k in CARD_KEYS if isinstance(entry.get(k), str)), None)
        if url is None and isinstance(entry.get("url"), str):
            base = entry["url"]
            url = base if base.endswith(".json") else f"{base.rstrip('/')}{CARD_PATH}" if urlsplit(base).path in ("", "/") else None
        if not url or len(url) > MAX_URL:
            continue
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            continue
        try:
            url = normalize_card_url(url)
        except Exception:  # noqa: BLE001 -- an unusable entry is skipped, never fatal
            continue
        if url not in out:
            out.append(url)
    return out


def _marker(db: Session, key: str) -> bool:
    return db.execute(text("SELECT 1 FROM society_events WHERE idempotency_key = :k"), {"k": key}).first() is not None


def _record(db: Session, event_type: str, key: str, payload: Dict[str, Any]) -> None:
    from ...society.events import emit_event  # noqa: PLC0415

    emit_event(db, event_type=event_type, payload=payload, actor_type="system", idempotency_key=key[:160])
    db.commit()


async def _get_json(url: str, client: Any = None) -> Any:
    own = client is None
    client = client or safe_client(timeout=15.0, max_response_bytes=DIRECTORY_MAX_BYTES, headers={"Accept": "application/json"})
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise CardFetchError(f"http_{resp.status_code}", "directory listing")
        return json.loads(resp.content)
    finally:
        if own:
            await client.aclose()


def _known(db: Session) -> set:
    return {u for (u,) in db.query(A2ARemoteAgent.card_url).all()}


async def scan(session_factory: Callable[[], Session], now: Optional[datetime] = None, *, client: Any = None) -> Optional[Dict[str, Any]]:
    """The daily directory pass. Returns its counts, or None when it is not due / not configured."""
    dirs = directory_urls()
    if not dirs or not config.federation_enabled():
        return None
    now = now or datetime.now(timezone.utc)
    key = f"a2a.directory.scan:{now.date().isoformat()}"
    db = session_factory()
    try:
        if _marker(db, key):
            return None
        known = _known(db)
        # the day's marker first: a pass that dies or times out is not retried until tomorrow
        _record(db, SCANNED_EVENT, key, {"started_at": now.isoformat(), "directories": len(dirs)})
    finally:
        db.close()
    counts: Dict[str, Any] = {"directories": len(dirs), "listed": 0, "new": 0, "refreshed": 0, "errors": {}}
    budget = max_new_per_day()

    def err(cls: str) -> None:
        counts["errors"][cls] = counts["errors"].get(cls, 0) + 1

    for d in dirs:
        try:
            urls = card_urls(await _get_json(d, client))
        except (CardFetchError, OutboundRefused, ValueError) as exc:
            err(f"directory_{getattr(exc, 'result', None) or type(exc).__name__}")
            continue
        except Exception as exc:  # noqa: BLE001 -- one directory never stops the pass
            err(f"directory_{type(exc).__name__}")
            continue
        counts["listed"] += len(urls)
        for url in urls:
            if budget <= 0:
                break
            if url in known:
                continue
            known.add(url)
            budget -= 1
            try:
                await catalog.discover(session_factory, url, {"source": "directory", "directory": urlsplit(d).hostname, "at": now.isoformat()})
                counts["new"] += 1
            except CardFetchError as exc:
                err(f"card_{exc.result}")
            except Exception as exc:  # noqa: BLE001
                err(f"card_{type(exc).__name__}")
    db = session_factory()
    try:
        stale = [a.id for a in db.query(A2ARemoteAgent).filter(A2ARemoteAgent.state.in_(["discovered", "verified", "degraded"]))
                 .filter((A2ARemoteAgent.last_validated_at.is_(None)) | (A2ARemoteAgent.last_validated_at < now - STALE_AFTER))
                 .order_by(A2ARemoteAgent.last_validated_at.asc().nullsfirst()).limit(refresh_per_pass()).all()]
    finally:
        db.close()
    for rid in stale:
        try:
            await catalog.refresh(session_factory, rid)
            counts["refreshed"] += 1
        except Exception as exc:  # noqa: BLE001
            err(f"refresh_{type(exc).__name__}")
    db = session_factory()
    try:
        _record(db, SCANNED_EVENT, f"{key}:done", counts)
    finally:
        db.close()
    return counts


async def publish_self(session_factory: Callable[[], Session], now: Optional[datetime] = None, *, client: Any = None) -> Optional[Dict[str, Any]]:
    """The weekly listing request. Sends only AgentNet's public card URL."""
    targets = register_urls()
    base = config.public_base_url()
    if not targets or not config.federation_enabled() or not config.server_enabled() or base is None:
        return None
    now = now or datetime.now(timezone.utc)
    week = "%d-W%02d" % now.isocalendar()[:2]
    card = f"{base}{CARD_PATH}"
    results: Dict[str, str] = {}
    own = client is None
    client = client or safe_client(timeout=15.0, headers={"Accept": "application/json"})
    try:
        for target in targets:
            key = f"a2a.directory.register:{hashlib.sha256(target.encode()).hexdigest()[:16]}:{week}"
            db = session_factory()
            try:
                if _marker(db, key):
                    continue
                _record(db, LISTED_EVENT, key, {"directory": urlsplit(target).hostname, "outcome": "attempted", "card_url": card})
            finally:
                db.close()
            try:
                resp = await client.post(target, json={"wellKnownURI": card})
                outcome = "listed" if resp.status_code in (200, 201, 202) else "already_listed" if resp.status_code == 409 else f"http_{resp.status_code}"
            except OutboundRefused:
                outcome = "refused_by_netguard"
            except Exception as exc:  # noqa: BLE001
                outcome = f"error_{type(exc).__name__}"
            results[urlsplit(target).hostname or "?"] = outcome
            db = session_factory()
            try:
                _record(db, LISTED_EVENT, f"{key}:done", {"directory": urlsplit(target).hostname, "outcome": outcome, "card_url": card})
            finally:
                db.close()
    finally:
        if own:
            await client.aclose()
    return results
