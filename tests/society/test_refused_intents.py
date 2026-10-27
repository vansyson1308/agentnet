"""The audit names the invalid-intent rate by intent type and reason class (phase5_live I01).

Staging 2026-10-27: 20 of 125 intents in 24h were invalid and nobody could say which or why. The
reason class is structural (intents.safe_error_summary: error type @ declared field), never the
payload."""

from __future__ import annotations

import importlib.util
import pathlib

from services.registry.app.society.seed import seed_society

from .test_builder_decline import _run_as


def _phase5():
    spec = importlib.util.spec_from_file_location("phase5_live_refused", pathlib.Path(__file__).resolve().parents[2] / "deploy/railway/phase5_live.py")
    p5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p5)
    return p5


def test_invalid_intents_are_counted_by_type_and_structural_reason(db, engine, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    _run_as(db, SessionLocal, society_settings, "scout", [
        {"type": "SEND_MESSAGE", "payload": {"title": "t", "content": "c"}},
        {"type": "SEND_MESSAGE", "payload": {"title": "t"}},
        {"type": "SEND_MESSAGE", "payload": {"content": "secret plan text"}},
        {"type": "DO_SOMETHING_NEW", "payload": {}},
    ])
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            out = _phase5().refused_intents(cur, None)
        conn.rollback()
    finally:
        conn.close()
    assert out["intents"] == 4 and out["invalid"] == 3
    got = {(x["intent_type"], x["reason"], x["count"]) for x in out["by_type_reason"] if x["decision"] == "invalid"}
    assert got == {("SEND_MESSAGE", "schema: missing@content", 1), ("SEND_MESSAGE", "schema: missing@title", 1),
                   ("DO_SOMETHING_NEW", "unknown intent type (not in the intent vocabulary)", 1)}
    assert "secret plan text" not in str(out)
