"""The Owner Console (/owner): operators only, decided by the registry.

The page makes no role decision of its own: it calls the registry's operator routes with the
signed-in user's token and turns the registry's 401/403 into sign-in/403. Every action is an
existing operator route; a POST needs the session's CSRF token. The registry is stubbed at
``ApiClient._request`` (no network)."""

from __future__ import annotations

import pathlib
import re

import pytest

DASH = pathlib.Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app"
PLAN_ID = "11111111-1111-4111-8111-111111111111"
ACTION_ID = "22222222-2222-4222-8222-222222222222"
INCIDENT_ID = "33333333-3333-4333-8333-333333333333"

OPERATOR_DATA = {
    "/v1/society/company/directive": {
        "active": {"id": "d1", "text": "first paid USDC task on staging this week; focus on O4", "objective_ids": ["O4"], "created_at": "2026-10-29T08:00:00+00:00"},
        "history": [], "weight": 1.0,
        "following": [{"ticket_id": "t1", "title": "Quote and accept a paid USDC task", "objective_id": "O4", "status": "planned", "planned": True,
                       "review": "reviewing", "candidate": None, "promotion": None, "pr_url": None}],
        "objectives": [{"id": "O4", "title": "Revenue", "status": "active", "key_results": [{"metric_id": "paid_usdc_tasks_30d", "direction": "up"}]}],
    },
    "/v1/society/company/north-star": {"summary_text": "North star: $0.00 / $1,000,000"},
    "/v1/society/company/report": {"text": "Last 24h: 1 cycle(s)", "data": {
        "ready_candidates": [{"candidate": "feb64aa6", "title": "O4 doc", "promotion": "pr_open", "pr_number": 121, "pr_url": "https://github.com/o/r/pull/121", "reason": None}],
        "change_budget": {"promotions_today": 1, "max_promotions_per_day": 3, "open_prs": 1, "max_open_autonomous_prs": 3, "auto_merge_enabled": False}}},
    "/v1/society/company/plans": {"plans": [{"id": PLAN_ID, "plan_date": "2026-10-29", "status": "awaiting_owner",
                                             "ranking": [{"title": "Quote and accept a paid USDC task", "objective_id": "O4", "score": 2.0, "owner_directive": "d1"}]}]},
    "/v1/society/company/owner-actions": {"owner_actions": [{"id": ACTION_ID, "objective_id": "O4", "title": "Post the offer", "reason": "public post"}]},
    "/v1/society/incidents": {"incidents": [{"id": INCIDENT_ID, "source": "fitness", "reason": "regressed", "openedAt": "2026-10-28", "liftedAt": None}]},
    "/v1/society/budget": {"model_spend_today_usd": "1.20", "daily_model_budget_usd": "10", "remaining_usd": "8.80", "by_agent": []},
    "/v1/society/company/owner-thread": {"working": True, "entries": [
        {"id": "e2", "kind": "message", "text": "find our first paying external agent", "refs": {}, "created_at": "2026-10-29T09:30:00+00:00",
         "status": "working", "reply": None, "plans": [], "tickets": [], "work": []},
        {"id": "e1", "kind": "directive", "text": "focus on O4", "refs": {"directive_id": "d1"}, "created_at": "2026-10-29T09:21:00+00:00",
         "status": "answered", "reply": {"text": "Understood: O4 first. The Scout files the quote ticket now.", "plan_steps": [{"step": "file the O4 ticket", "state": "ticket", "ticket_id": "t1aaaaaa-x", "status": "planned"},
                                                                                                                                  {"step": "request the PR", "state": "started", "type": "REQUEST_PR_PROMOTION", "status": "executed"},
                                                                                                                                  {"step": "design review", "state": "next"}],
                                         "ticket_ids": ["t1"], "owner_actions_needed": ["post the offer"], "created_at": "2026-10-29T09:22:10+00:00"},
         "work": [{"type": "REQUEST_PR_PROMOTION", "role": "governor", "status": "executed", "policy": "allowed",
                   "result": {"promotion_id": "44444444-4444-4444-8444-444444444444"}, "error": None}],
         "plans": [{"id": PLAN_ID, "status": "awaiting_owner"}],
         "tickets": [{"ticket_id": "t1", "title": "Quote and accept a paid USDC task", "objective_id": "O4", "status": "planned", "planned": True,
                      "review": None, "candidate": None, "promotion": None, "pr_url": None}]}],
        "slo": {"window_days": 7, "first_run_start": {"n": 2, "p95_s": 4.2, "target_s": 30, "late_unanswered": 0, "ok": True},
                "reply": {"n": 1, "p95_s": 70.0, "target_s": 120, "late_unanswered": 0, "ok": True},
                "first_work": {"n": 1, "p95_s": 95.0, "target_s": 300, "late_unanswered": 0, "ok": True},
                "directive_to_plan": {"n": 0, "p95_s": None, "target_s": 600, "late_unanswered": 0, "ok": None}}},
    "/v1/society/company/funnel": {"window_hours": 24, "as_of": "2026-10-29T12:00:00+00:00", "spend_usd": 42.5, "runs_by_role": {"builder": 30, "governor": 12},
                                   "tickets_created": 9, "tickets_approved": 7, "designs_go": 5, "designs_returned": 2, "builds_attempted": 6, "builds_delivered": 4,
                                   "ready": 3, "prs_opened": 2, "ci_green": 1, "merged": 0, "usd_per_ready": 14.17, "usd_per_pr": 21.25,
                                   "rejections": {"spec_outside_files_allowed": 2}, "stages": [],
                                   "bottleneck": {"stage": "qa_security", "waiting": 3, "oldest_h": 5.0, "median_h": 3.5},
                                   "by_2h": [{"from": "2026-10-29T10:00:00+00:00", "to": "2026-10-29T12:00:00+00:00", "spend_usd": 6.0, "tickets_created": 2,
                                              "tickets_approved": 1, "designs_go": 1, "builds_delivered": 1, "ready": 1, "prs_opened": 1, "merged": 0}]},
}


@pytest.fixture()
def console(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    from services.dashboard.app import api_client as client_mod
    from services.dashboard.app import main

    calls = []
    state = {"role": "operator"}

    def stub(self, method, path, **kwargs):
        auth = (kwargs.get("headers") or {}).get("Authorization")
        calls.append((method, path, auth, kwargs.get("json")))
        if path.startswith("/v1/society"):
            if auth != "Bearer user-jwt":
                raise client_mod.APIError("unauthorized", 401)
            if state["role"] != "operator":
                raise client_mod.APIError("forbidden", 403, "society operator role required")
            if method == "GET" and path in OPERATOR_DATA:
                return OPERATOR_DATA[path]
            if method == "POST":
                return {}
        raise client_mod.APIError(f"not stubbed: {method} {path}", 404)

    monkeypatch.setattr(client_mod.ApiClient, "_request", stub)
    main.app.config.update(TESTING=True)
    with main.app.test_client() as c:
        yield c, calls, state


def _sign_in(c):
    with c.session_transaction() as s:
        s["access_token"] = "user-jwt"


def _csrf(c):
    with c.session_transaction() as s:
        return s.get("owner_csrf")


def test_anonymous_visitors_are_sent_to_sign_in(console):
    c, calls, _ = console
    r = c.get("/owner")
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/login")
    assert not calls, "nothing is asked of the registry without a session"


def test_a_signed_in_non_operator_gets_403_from_the_registry_decision(console):
    c, calls, state = console
    state["role"] = None
    _sign_in(c)
    r = c.get("/owner")
    assert r.status_code == 403 and b"operators only" in r.data
    assert calls and calls[0][2] == "Bearer user-jwt", "the registry decided, with the user's own token"


def test_an_operator_sees_every_section_from_the_operator_api(console):
    c, calls, _ = console
    _sign_in(c)
    r = c.get("/owner")
    html = r.data.decode()
    assert r.status_code == 200 and "Owner Console" in html
    for marker in ("Kim chỉ nam", "first paid USDC task on staging", "What came from it", "North star", "Today's plan", "Approve",
                   "Owner actions", "Post the offer", "READY candidates", "PR #121", "Open freezes", "Lift", "Budget and spend", "New task",
                   "Ra lệnh / hỏi công ty", "find our first paying external agent", "⚙️ working", "✅ answered", "Understood: O4 first.",
                   "file the O4 ticket", "Needs you: post the offer", "from this directive", "p95 70.0s / 120s", "no data",
                   "→ ticket t1aaaaaa (planned)", "→ REQUEST_PR_PROMOTION (executed)", "· next", "Work started", "promotion_id=44444444",
                   "→ first work started p95 95.0s / 300s", "Throughput funnel (24h)", "Spend $42.50", "$/READY 14.17", "$/PR 21.25",
                   "Bottleneck now:</strong> qa_security", "spec_outside_files_allowed 2"):
        assert marker in html, marker
    assert {p for m, p, _, _ in calls if m == "GET"} == set(OPERATOR_DATA), "only the existing operator routes (+ the directive)"


def test_actions_post_to_operator_routes_with_the_users_token_and_csrf(console):
    c, calls, _ = console
    _sign_in(c)
    c.get("/owner")
    token = _csrf(c)
    calls.clear()
    assert c.post("/owner/directive", data={"csrf": "wrong", "text": "x"}).status_code == 302
    assert not calls, "a POST without the session's CSRF token reaches nothing"
    c.post("/owner/directive", data={"csrf": token, "text": "focus on O4", "objective_ids": ["O4"]})
    c.post(f"/owner/plans/{PLAN_ID}/approve", data={"csrf": token})
    c.post(f"/owner/owner-actions/{ACTION_ID}/done", data={"csrf": token, "note": "posted"})
    c.post(f"/owner/incidents/{INCIDENT_ID}/lift", data={"csrf": token})
    c.post("/owner/tickets", data={"csrf": token, "title": "T", "problem": "P", "objective_id": "O4", "metric_id": "paid_usdc_tasks_30d",
                                   "expected_effect": "1", "direction": "up", "proof": "tests/test_x.py::test_y"})
    c.post("/owner/messages", data={"csrf": token, "text": "  what is the company doing about O4?  "})
    posts = [(p, body) for m, p, auth, body in calls if m == "POST" and auth == "Bearer user-jwt"]
    assert posts == [
        ("/v1/society/company/directive", {"text": "focus on O4", "objective_ids": ["O4"]}),
        (f"/v1/society/company/plans/{PLAN_ID}/approve", {}),
        (f"/v1/society/company/owner-actions/{ACTION_ID}/done", {"note": "posted"}),
        (f"/v1/society/incidents/{INCIDENT_ID}/lift", {"reason": "lifted by the owner from the Owner Console"}),
        ("/v1/society/company/tickets", {"title": "T", "problem": "P", "objective_id": "O4", "metric_id": "paid_usdc_tasks_30d",
                                        "expected_effect": 1.0, "direction": "up", "proof": ["tests/test_x.py::test_y"]}),
        ("/v1/society/company/owner-messages", {"text": "what is the company doing about O4?"}),
    ]


def test_the_conversation_polls_every_5s_only_while_something_is_working(console):
    c, calls, state = console
    _sign_in(c)
    html = c.get("/owner").data.decode()
    assert 'id="owner-thread" data-working="1"' in html and "setTimeout(tick, 5000)" in html and "/owner/thread" in html
    calls.clear()
    frag = c.get("/owner/thread")
    assert frag.status_code == 200 and 'data-working="1"' in frag.data.decode() and "<html" not in frag.data.decode()
    assert [(m, p, a) for m, p, a, _ in calls] == [("GET", "/v1/society/company/owner-thread", "Bearer user-jwt")], "one operator route, the user's token"
    idle = dict(OPERATOR_DATA["/v1/society/company/owner-thread"], working=False)
    OPERATOR_DATA["/v1/society/company/owner-thread"], saved = idle, OPERATOR_DATA["/v1/society/company/owner-thread"]
    try:
        assert 'data-working="0"' in c.get("/owner/thread").data.decode(), "nothing working: the poll stops"
    finally:
        OPERATOR_DATA["/v1/society/company/owner-thread"] = saved
    state["role"] = None
    assert c.get("/owner/thread").status_code == 403, "the registry decides the fragment too"
    with c.session_transaction() as s:
        s.pop("access_token", None)
    assert c.get("/owner/thread").status_code in (302, 303)


def test_the_console_makes_no_role_decision_of_its_own():
    """No client-side checks, no email comparisons: authority is the registry's require_operator."""
    src = (DASH / "main.py").read_text(encoding="utf-8")
    console_src = "\n".join(line for line in src[src.index("OWNER CONSOLE"):].splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"email|society_role|is_operator|OPERATOR_EMAILS|BOOTSTRAP", console_src, re.IGNORECASE)
    assert "Authorization" not in src.split("OWNER CONSOLE")[0], "the token is never put on shared headers"


def test_a_direction_with_more_than_two_objectives_is_flagged_as_no_priority(console):
    """Owner 2026-10-29: 'everything x2 = no priority' -- the console warns, the registry still decides."""
    c, calls, _ = console
    _sign_in(c)
    c.get("/owner")
    token = _csrf(c)
    calls.clear()
    r = c.post("/owner/directive", data={"csrf": token, "text": "do it all", "objective_ids": ["O1", "O2", "O4"]}, follow_redirects=True)
    assert "3 objectives selected: everything x2 = no priority" in r.data.decode()
    assert ("POST", "/v1/society/company/directive") in [(m, p) for m, p, _, _ in calls], "a warning, not a block"
    calls.clear()
    c.post("/owner/directive", data={"csrf": token, "text": "focus", "objective_ids": ["O4", "O2"]})
    assert "no priority" not in c.get("/owner").data.decode()
    active = OPERATOR_DATA["/v1/society/company/directive"]["active"]
    OPERATOR_DATA["/v1/society/company/directive"]["active"] = dict(active, objective_ids=["O1", "O2", "O3"])
    try:
        assert "3 objectives selected: everything ×2 = no priority" in c.get("/owner").data.decode(), "the active direction is flagged too"
    finally:
        OPERATOR_DATA["/v1/society/company/directive"]["active"] = active


def test_an_answer_with_no_work_says_words_only(console):
    c, _, _ = console
    _sign_in(c)
    thread = OPERATOR_DATA["/v1/society/company/owner-thread"]
    answered = dict(thread["entries"][1], work=[])
    OPERATOR_DATA["/v1/society/company/owner-thread"] = dict(thread, entries=[answered])
    try:
        html = c.get("/owner/thread").data.decode()
    finally:
        OPERATOR_DATA["/v1/society/company/owner-thread"] = thread
    assert "No work started yet: words only." in html and "Work started</th>" not in html
