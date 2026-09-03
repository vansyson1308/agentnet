#!/usr/bin/env python3
"""
Autonomous Society Runtime — end-to-end demo (deterministic by default).

Injects exactly ONE world event and lets the society drive everything else:

    platform.metric.anomaly
      -> Society_Scout wakes, reads mission/goals/memory, proposes an improvement
      -> Society_Governor reviews and approves the proposal
      -> Society_Architect designs a bounded change, escrows a Builder task
      -> Society_Builder implements it in an isolated git worktree (agentnet-auto/<id>)
      -> Society_QA evaluates independently (allow-list, compile, acceptance tests)
      -> candidate READY; Builder completes the escrowed task; memories written

The script does NOT create proposals, tasks, candidates or verdicts itself;
after the initial event it only runs the worker loop and then prints the
persisted chain (events -> runs -> intents -> candidate -> QA -> wallets).

Usage (from the repo root, with Postgres reachable via POSTGRES_* env):

    python examples/demo_autonomous_society.py [--provider scripted|openai_compatible]
                                               [--fund 100] [--keep-workspace] [--json]

Requires the schema at alembic head (0007_society_runtime). Autonomy flags
are forced ON for this process only; production deploy stays hard OFF.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import uuid

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _ev(v):
    return v.value if hasattr(v, "value") else v


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", default=os.getenv("SOCIETY_MODEL_PROVIDER", "scripted"), choices=["scripted", "openai_compatible"])
    parser.add_argument("--fund", type=int, default=100, help="dev-only credits for the Architect wallet (0 = no escrow demo)")
    parser.add_argument("--keep-workspace", action="store_true", help="keep the worktree + agentnet-auto branch")
    parser.add_argument("--json", action="store_true", help="print the correlation story as JSON")
    parser.add_argument("--max-cycles", type=int, default=60)
    args = parser.parse_args()

    # Flags for THIS process only (never written to .env).
    os.environ["SOCIETY_RUNTIME_ENABLED"] = "true"
    os.environ["SOCIETY_AUTONOMOUS_CODE_ENABLED"] = "true"
    os.environ["SOCIETY_MODEL_PROVIDER"] = args.provider
    os.environ.setdefault("SOCIETY_REPO_ROOT", str(REPO))
    os.environ.setdefault("JAEGER_ENABLED", "false")
    os.environ.setdefault("ENVIRONMENT", "development")

    from sqlalchemy import text

    from services.registry.app.database import SessionLocal, engine
    from services.registry.app.models import AgentCapabilityGrant, AgentIntent, AgentRun, CodeCandidate, MemoryItem, SocietyEvent, TaskSession, Wallet, WalletOwnerType
    from services.registry.app.society.config import get_settings, reset_settings_cache
    from services.registry.app.society.engineering import workspace as ws_mod
    from services.registry.app.society.events import EventType, emit_event
    from services.registry.app.society.seed import seed_society
    from services.registry.app.society.worker import SocietyWorker

    reset_settings_cache()
    settings = get_settings()

    with engine.connect() as conn:
        ok = conn.execute(text("SELECT to_regclass('public.society_events')")).scalar()
    if not ok:
        print("society tables missing — run: cd services/registry && alembic upgrade head", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        report = seed_society(db)
        print(f"fleet: created={report.created_agents} reused={report.reused_agents}")
        for g in db.query(AgentCapabilityGrant).all():
            g.wake_cooldown_seconds = 0  # demo runs the whole chain in one process
        if args.fund and settings.runtime_enabled and os.getenv("ENVIRONMENT", "development") == "development":
            w = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == report.agents["architect"]).first()
            if w.balance_credits - w.reserved_credits < 10:
                w.balance_credits += args.fund
                print(f"dev funding: +{args.fund} credits to Society_Architect (operator action, not the runtime)")
        db.commit()

        correlation = uuid.uuid4()
        run_tag = uuid.uuid4().hex[:8]
        ev = emit_event(
            db,
            event_type=EventType.PLATFORM_METRIC_ANOMALY,
            payload={
                "metric": f"task_failure_rate_{run_tag}",
                "value": 0.42,
                "threshold": 0.10,
                "description": "task failure rate above threshold for 15 minutes",
                "severity_score": 70,
            },
            actor_type="system",
            correlation_id=correlation,
            idempotency_key=f"demo-anomaly-{run_tag}",
        )
        db.commit()
        print(f"\ninjected ONE event: {ev.event_type} id={ev.id} correlation={correlation}\n")

        worker = SocietyWorker(SessionLocal, settings=settings, worker_id=f"demo-{run_tag}")
        stats = asyncio.run(worker.run_until_idle(max_cycles=args.max_cycles))
        print("worker stats:", json.dumps({k: v for k, v in stats.as_dict().items() if k != "processed_run_ids"}))

        events = db.query(SocietyEvent).filter(SocietyEvent.correlation_id == correlation).order_by(SocietyEvent.created_at).all()
        runs = db.query(AgentRun).filter(AgentRun.correlation_id == correlation).order_by(AgentRun.created_at).all()
        cands = db.query(CodeCandidate).filter(CodeCandidate.correlation_id == correlation).all()
        cand_ids = {str(c.id) for c in cands}
        tasks = [t for t in db.query(TaskSession).filter(TaskSession.callee_agent_id == report.agents["builder"]).all() if (t.input or {}).get("candidate_id") in cand_ids]

        print("\n=== correlation chain (from the database) ===")
        for e in events:
            src = f" <- run {str(e.source_run_id)[:8]}" if e.source_run_id else ""
            print(f"  event {str(e.id)[:8]} depth={e.causation_depth} {e.event_type:38s} [{_ev(e.status)}]{src}")
        print()
        for r in runs:
            intents = db.query(AgentIntent).filter(AgentIntent.run_id == r.id).order_by(AgentIntent.seq).all()
            print(f"  run {str(r.id)[:8]} {r.role:10s} attempt={r.attempt} [{_ev(r.status)}] {r.model_provider}/{r.model_name}")
            print(f"      decision: {r.decision_summary}")
            for i in intents:
                print(f"      intent {i.seq} {i.intent_type:26s} policy={_ev(i.policy_decision):8s} exec={_ev(i.execution_status):9s} {i.error or ''}")
        print()
        for c in cands:
            print(f"  candidate {str(c.id)[:8]} [{_ev(c.status)}] branch={c.branch_name} files={c.changed_files}")
            print(f"      QA: {(c.qa_report or {}).get('summary')}")
        for t in tasks:
            print(f"  task {str(t.id)[:8]} [{_ev(t.status)}] escrow={t.escrow_amount} caller->callee={str(t.caller_agent_id)[:8]}->{str(t.callee_agent_id)[:8]}")
        for name in ("Society_Architect", "Society_Builder"):
            from services.registry.app.models import Agent

            a = db.query(Agent).filter(Agent.name == name).first()
            w = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == a.id).first()
            print(f"  wallet {name}: balance={w.balance_credits} reserved={w.reserved_credits}")
        run_tags = {f"run:{r.id}" for r in runs}
        mems = [m for m in db.query(MemoryItem).all() if run_tags & set(m.tags or [])]
        print(f"  memory items written by this story: {len(mems)} ({[m.title[:40] for m in mems]})")

        if args.json:
            print(json.dumps({"correlation_id": str(correlation), "events": [e.event_type for e in events], "runs": [(r.role, _ev(r.status)) for r in runs], "candidates": [(str(c.id), _ev(c.status)) for c in cands]}, indent=2))

        ready = bool(cands) and _ev(cands[0].status) == "ready"
        print("\nRESULT:", "PASS — candidate READY, chain reconstructed from durable state" if ready else "FAIL — see intents/errors above")

        if cands and not args.keep_workspace:
            for c in cands:
                if c.workspace_path:
                    try:
                        ws = ws_mod.ensure_workspace(settings, c.id)
                        ws_mod.remove_workspace(settings, ws, delete_branch=True)
                        print(f"cleaned worktree + branch for candidate {str(c.id)[:8]}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"cleanup skipped: {exc}")
        return 0 if ready else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
