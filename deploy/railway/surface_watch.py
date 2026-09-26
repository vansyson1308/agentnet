#!/usr/bin/env python3
"""Read-only operator view of the Society's public-surface work (staging).

Logs in as the staging operator (validate_staging.ensure_user, the staging
practice) and prints:

* the ``public_surface`` block of ``GET /v1/society/company`` (monitor
  settings, open anomaly, recent anomaly/recovery events, the Society's
  workstream on the newest anomaly);
* the newest ``public.surface.*`` events (operator ``/events``);
* the company portfolio, the open improvement proposals that fill it and
  every unfinished candidate;
* the society-worker's own ``society_public_surface_*`` metrics (the monitor
  runs there, not in the registry that serves the operator view);
* for the anomaly's correlation: the operator story (events, runs, their
  decision summaries and intents);
* each candidate's operator detail (spec, files, QA and Security reports,
  risk tier, promotion).

It changes nothing, never prints a token and scrubs every printed body the
same way ``phase5_live`` does (model-authored text is untrusted data). Run it
from ``staging-validator`` with ``VALIDATOR_SCRIPT=surface_watch.py``.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import validate_staging as vs  # noqa: E402
from phase5_live import scrub  # noqa: E402

LIMIT = 24_000
SURFACE_EVENTS = ("public.surface.anomaly", "public.surface.recovered")
OPEN_PROPOSAL_STATUSES = ("PROPOSED", "UNDER_REVIEW", "APPROVED", "CONVERTED_TO_TASK")
OPEN_CANDIDATE_STATUSES = ("requested", "building", "built", "qa_running", "security_review")


def emit(label: str, status: int, body: str) -> None:
    """One bounded, scrubbed line per read."""
    print(f"WATCH {label} HTTP {status} " + scrub(body)[:LIMIT], flush=True)


def main() -> int:
    api = vs.env("REGISTRY_PUBLIC_URL").rstrip("/")
    secret = vs.env("STAGING_VALIDATOR_SECRET") or vs.env("VALIDATOR_SECRET")
    email = vs.env("VALIDATOR_OPERATOR_EMAIL", "staging-operator@staging.agentnet.io.vn")
    rep = vs.Report()
    if not api or len(secret) < 32:
        rep.record("W00", False, "REGISTRY_PUBLIC_URL and a validator secret are required")
        return vs.finish(rep)
    token = vs.ensure_user(rep, "W10", api, email, vs.derive_password(secret))
    if not token:
        return vs.finish(rep)
    st, body = vs.http("GET", f"{api}/v1/society/company", token=token)
    company = json.loads(body) if st == 200 else {}
    view = company.get("public_surface")
    rep.record("W20", st == 200 and view is not None, f"operator company status: HTTP {st}")
    emit("public_surface", st, json.dumps(view, sort_keys=True, default=str))
    emit("portfolio", st, json.dumps(company.get("portfolio"), sort_keys=True, default=str))
    for event_type in SURFACE_EVENTS:
        st, body = vs.http("GET", f"{api}/v1/society/events?event_type={event_type}&limit=5", token=token)
        emit(f"events {event_type}", st, body)
    for status in OPEN_PROPOSAL_STATUSES:
        st, body = vs.http("GET", f"{api}/v1/improvements/?status={status}&limit=50", token=token)
        rows = json.loads(body) if st == 200 else []
        brief = [{k: r.get(k) for k in ("id", "status", "source", "importance", "created_at", "updated_at", "title")} for r in rows if isinstance(r, dict)]
        emit(f"proposals {status}", st, json.dumps(brief, default=str))
    metrics_url = vs.env("SOCIETY_METRICS_URL")
    if metrics_url:
        # the monitor runs in the society-worker; its own counters prove it probes
        st, body = vs.http("GET", metrics_url)
        lines = [ln for ln in body.splitlines() if ln.startswith("society_public_surface_")]
        emit("worker_metrics", st, "\n".join(lines))
    st, body = vs.http("GET", f"{api}/v1/society/candidates?limit=20", token=token)
    open_ids = [c["id"] for c in (json.loads(body) if st == 200 else []) if c.get("status") in OPEN_CANDIDATE_STATUSES]
    for cid in open_ids:
        st, body = vs.http("GET", f"{api}/v1/society/candidates/{cid}", token=token)
        emit(f"open_candidate {cid}", st, body[:4000])
    work = (view or {}).get("workstream") or {}
    corr = work.get("correlation_id")
    if corr:
        st, body = vs.http("GET", f"{api}/v1/society/story/{corr}/detail", token=token)
        emit(f"story_detail {corr}", st, body)
        for c in work.get("candidates") or []:
            cid = c.get("candidate_id")
            if cid:
                st, body = vs.http("GET", f"{api}/v1/society/candidates/{cid}", token=token)
                emit(f"candidate {cid}", st, body)
    return vs.finish(rep)


if __name__ == "__main__":
    raise SystemExit(main())
