"""The Maintenance Release Controller (ADR-0010 D15-D18). No model, ever.

Runs in its own process/service (``python -m app.maintenance.release_worker``,
the ``release-control`` boundary) with the only production release
credentials (release_providers.py). The Society process has none of them.

For one attested release it:

1. VERIFIES independently -- never trusting a label the Society wrote:
   attestation digest + signature; the case is really releasing this row;
   the exact SHA is on ``main``; ``production`` is an ancestor of it; the
   production..sha diff is exactly the attested repair (no unrelated
   unreleased change rides along) and not truncated; the TRUSTED maintenance
   classifier (this process's copy) says MAINTENANCE_GREEN -- or AMBER with an
   owner merge verified on GitHub; required checks passed on that SHA; no
   release freeze, no foreign incident freeze, error budget allows, daily cap,
   provider healthy, Railway schema offers the mutations it will use.
   Records the KNOWN-GOOD production state (deployments per service, sha,
   tree) before anything changes.
2. validates the exact SHA on the release-preview surface;
3. opens ``release/prod-<sha>`` -> ``production`` (same rules as humans: PR,
   required CI, strict, no bypass, no force push) and merges it when green;
4. deploys ONLY the changed services at that exact SHA (adopting a deploy
   Railway's own "Wait for CI" already started -- never a second one);
5. requires N consecutive healthy public observations (provider SUCCESS is
   never enough) -> SUCCEEDED and a new known-good record;
6. on a regression rolls back to the recorded known-good deployments
   (falling back to redeploying the known-good SHA when an image expired),
   requires the public desired state restored, then opens a branch
   reconciliation PR and holds a production release freeze until branch and
   runtime agree again. A failed rollback is P0: SAFELY_ESCALATED + freeze,
   no retry storm.

Anything inconclusive fails closed (REFUSED); the kernel escalates the case.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import IncidentFreeze
from ..society.events import utcnow
from . import attestation as att_mod
from . import policy as pol
from . import slo as slo_mod
from . import state_machine as sm
from .config import MaintenanceSettings, get_maintenance_settings
from .orm import MaintenanceHeartbeat, MaintenanceIncident, MaintenanceKnownGood, MaintenanceRelease, MaintenanceReleaseFreeze, RepairCase
from .release_providers import (
    ContractProbe,
    LiveGitHub,
    LiveRailway,
    Preview,
    ProviderRefused,
    ProviderTransient,
    PublicProbe,
    ReleaseGitHub,
    ReleaseRailway,
    services_for,
)
from .taxonomy import IncidentClass, MaintenanceRiskClass as MRC, ReleaseStatus as RS

logger = logging.getLogger(__name__)
RELEASE_VERSION = "release-controller/1"
_BOOL = {"1", "true", "yes", "on"}


@dataclass
class ReleaseSettings:
    provider: str = field(default_factory=lambda: (os.getenv("MAINTENANCE_RELEASE_PROVIDER") or "disabled").strip().lower())
    repo: str = field(default_factory=lambda: (os.getenv("MAINTENANCE_RELEASE_REPO") or "vansyson1308/agentnet").strip())
    main_branch: str = field(default_factory=lambda: (os.getenv("MAINTENANCE_RELEASE_MAIN_BRANCH") or "main").strip())
    production_branch: str = field(default_factory=lambda: (os.getenv("MAINTENANCE_RELEASE_PRODUCTION_BRANCH") or "production").strip())
    railway_project_id: str = field(default_factory=lambda: (os.getenv("MAINTENANCE_RAILWAY_PROJECT_ID") or "").strip())
    railway_environment_id: str = field(default_factory=lambda: (os.getenv("MAINTENANCE_RAILWAY_ENVIRONMENT_ID") or "").strip())
    railway_service_ids: Dict[str, str] = field(default_factory=lambda: json.loads(os.getenv("MAINTENANCE_RAILWAY_SERVICE_IDS") or "{}"))
    ui_origin: str = field(default_factory=lambda: (os.getenv("MAINTENANCE_RELEASE_UI_ORIGIN") or "https://agentnet.io.vn").strip())
    api_origin: str = field(default_factory=lambda: (os.getenv("MAINTENANCE_RELEASE_API_ORIGIN") or "https://api.agentnet.io.vn").strip())
    owner_logins: Tuple[str, ...] = field(default_factory=lambda: tuple(x.strip() for x in (os.getenv("MAINTENANCE_RELEASE_OWNER_LOGINS") or "").split(",") if x.strip()))
    require_signature: bool = field(default_factory=lambda: (os.getenv("MAINTENANCE_RELEASE_REQUIRE_SIGNATURE") or "true").strip().lower() in _BOOL)
    deploy_grace_seconds: int = field(default_factory=lambda: int(os.getenv("MAINTENANCE_RELEASE_DEPLOY_GRACE_SECONDS") or "180"))
    step_backoff_seconds: int = field(default_factory=lambda: int(os.getenv("MAINTENANCE_RELEASE_BACKOFF_SECONDS") or "30"))
    max_transient: int = field(default_factory=lambda: int(os.getenv("MAINTENANCE_RELEASE_MAX_TRANSIENT") or "20"))


@dataclass
class Providers:
    github: ReleaseGitHub
    railway: ReleaseRailway
    probe: PublicProbe
    preview: Optional[Preview]


def live_providers(rs: ReleaseSettings) -> Optional[Providers]:
    if rs.provider != "live":
        return None
    if not (rs.railway_project_id and rs.railway_environment_id and rs.railway_service_ids):
        return None
    return Providers(
        github=LiveGitHub(rs.repo),
        railway=LiveRailway(project_id=rs.railway_project_id, environment_id=rs.railway_environment_id, service_ids=rs.railway_service_ids),
        probe=ContractProbe(rs.ui_origin, rs.api_origin),
        preview=None,  # a release-preview surface must be configured explicitly (fail closed without one)
    )


@dataclass
class ReleaseStats:
    claimed: int = 0
    advanced: int = 0
    refused: int = 0
    rolled_back: int = 0
    notes: List[str] = field(default_factory=list)


class ReleaseController:
    def __init__(self, session_factory, *, providers: Optional[Providers] = None, settings: Optional[MaintenanceSettings] = None, release_settings: Optional[ReleaseSettings] = None, worker_id: Optional[str] = None):
        self.session_factory = session_factory
        self._providers = providers
        self._settings = settings
        self._rs = release_settings
        self.worker_id = worker_id or f"release-{socket.gethostname()}-{os.getpid()}"

    @property
    def settings(self) -> MaintenanceSettings:
        return self._settings or get_maintenance_settings()

    @property
    def rs(self) -> ReleaseSettings:
        return self._rs or ReleaseSettings()

    def providers(self) -> Optional[Providers]:
        return self._providers or live_providers(self.rs)

    # ── cycle ──────────────────────────────────────────────────────────────
    def run_once(self, *, now: Optional[datetime] = None, max_items: int = 3) -> ReleaseStats:
        now = now or utcnow()
        st = ReleaseStats()
        self._heartbeat(now)
        prov = self.providers()
        for _ in range(max_items):
            rid = self._claim(now)
            if rid is None:
                break
            st.claimed += 1
            db = self.session_factory()
            try:
                rel = db.get(MaintenanceRelease, rid)
                if prov is None:
                    self._refuse(db, rel, now, "no release provider configured in release-control", st)
                else:
                    self._step(db, rel, prov, now, st)
                rel.lease_owner = None
                rel.lease_expires_at = None
                db.commit()
            except Exception as exc:  # noqa: BLE001 -- bounded: backoff, never a retry storm
                db.rollback()
                logger.exception("release controller: step failed")
                self._backoff(rid, now, type(exc).__name__)
            finally:
                db.close()
        if prov is not None:
            self._parity_sweep(prov, now)
        return st

    _CLAIM = text(
        """
        WITH c AS (
            SELECT id FROM maintenance_releases
             WHERE status NOT IN ('succeeded','rolled_back','rollback_failed','refused')
               AND next_action_at <= :now
               AND (lease_expires_at IS NULL OR lease_expires_at < :now)
             ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
        )
        UPDATE maintenance_releases r SET lease_owner = :w, lease_expires_at = :until, attempt = r.attempt + 1
          FROM c WHERE r.id = c.id RETURNING r.id
        """
    )

    def _claim(self, now: datetime):
        db = self.session_factory()
        try:
            row = db.execute(self._CLAIM, {"now": now, "w": self.worker_id, "until": now + timedelta(minutes=10)}).fetchone()
            db.commit()
            return row[0] if row else None
        finally:
            db.close()

    def _heartbeat(self, now: datetime) -> None:
        db = self.session_factory()
        try:
            hb = db.get(MaintenanceHeartbeat, "release_controller")
            if hb is None:
                hb = MaintenanceHeartbeat(component="release_controller", worker_id=self.worker_id, beat_at=now, cycles=0, errors=0, details={})
                db.add(hb)
            hb.beat_at, hb.worker_id, hb.cycles = now, self.worker_id, int(hb.cycles or 0) + 1
            hb.details = {"version": RELEASE_VERSION, "provider": self.rs.provider, "green_release_enabled": self.settings.green_release_enabled}
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

    def _backoff(self, rid, now: datetime, err: str) -> None:
        db = self.session_factory()
        try:
            rel = db.get(MaintenanceRelease, rid)
            if rel is not None:
                rel.lease_owner = None
                rel.lease_expires_at = None
                rel.next_action_at = now + timedelta(seconds=min(900, self.rs.step_backoff_seconds * (2 ** min(int(rel.attempt or 0), 5))))
                v = dict(rel.verification or {})
                v["last_error"] = err[:64]
                rel.verification = v
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

    # ── transitions ───────────────────────────────────────────────────────
    def _set(self, rel: MaintenanceRelease, status: RS, now: datetime, *, delay: float = 0.0, reason: Optional[str] = None) -> None:
        rel.status = status.value
        rel.updated_at = now
        rel.next_action_at = now + timedelta(seconds=delay)
        if reason:
            rel.failure_reason = reason[:500]
        if status in (RS.SUCCEEDED, RS.ROLLED_BACK, RS.ROLLBACK_FAILED, RS.REFUSED):
            rel.completed_at = now
        hist = list((rel.verification or {}).get("history") or [])
        hist.append({"at": now.isoformat(), "status": status.value, "reason": (reason or "")[:200]})
        rel.verification = {**(rel.verification or {}), "history": hist[-60:]}

    def _refuse(self, db, rel, now, reason: str, st: ReleaseStats) -> None:
        self._set(rel, RS.REFUSED, now, reason=reason)
        st.refused += 1

    def _wait(self, rel, now, reason: str, st: ReleaseStats, *, seconds: Optional[int] = None) -> None:
        """A transient condition. Bounded: past the deadline or after too many
        transient waits the release is refused (fail closed)."""
        waits = int((rel.verification or {}).get("transient_waits") or 0) + 1
        rel.verification = {**(rel.verification or {}), "transient_waits": waits, "waiting_on": reason[:200]}
        if now >= rel.deadline_at or waits > self.rs.max_transient:
            self._refuse(None, rel, now, f"inconclusive: {reason}", st)
            return
        rel.next_action_at = now + timedelta(seconds=seconds or self.rs.step_backoff_seconds)

    # ── the step ──────────────────────────────────────────────────────────
    def _step(self, db: Session, rel: MaintenanceRelease, p: Providers, now: datetime, st: ReleaseStats) -> None:
        status = RS(rel.status)
        before = rel.status
        try:
            getattr(self, f"_s_{status.value}")(db, rel, p, now, st)
        except ProviderTransient as exc:
            if status in (RS.DEPLOYING, RS.POST_DEPLOY_VERIFYING, RS.ROLLING_BACK) and now >= rel.deadline_at:
                if status is RS.ROLLING_BACK:
                    self._rollback_failed(db, rel, now, f"provider unavailable during rollback: {exc}", st)
                else:
                    self._start_rollback(db, rel, now, f"provider unavailable after merge: {exc}", st)
            else:
                self._wait(rel, now, f"provider transient: {exc}", st)
        except ProviderRefused as exc:
            if status in (RS.PENDING, RS.VERIFYING, RS.PREVIEW_VALIDATING, RS.PR_OPEN):
                self._refuse(db, rel, now, f"provider refused: {exc}", st)
            elif status is RS.ROLLING_BACK:
                self._rollback_failed(db, rel, now, f"rollback refused: {exc}", st)
            else:
                self._start_rollback(db, rel, now, f"provider refused after merge: {exc}", st)
        if rel.status != before:
            st.advanced += 1

    def _s_pending(self, db, rel, p, now, st):
        s = self.settings
        if not s.autonomy_enabled:
            return self._refuse(db, rel, now, "MAINTENANCE_AUTONOMY_ENABLED is false (owner kill switch)", st)
        if not s.green_release_enabled:
            return self._refuse(db, rel, now, "MAINTENANCE_GREEN_RELEASE_ENABLED is false in release-control", st)
        self._set(rel, RS.VERIFYING, now)
        self._s_verifying(db, rel, p, now, st)

    def _s_verifying(self, db, rel, p, now, st):
        ok, transient, reason, facts = self.verify(db, rel, p, now)
        rel.verification = {**(rel.verification or {}), "checks": facts, "verified_at": now.isoformat() if ok else None}
        if transient:
            return self._wait(rel, now, reason, st)
        if not ok:
            return self._refuse(db, rel, now, reason, st)
        kg = self._record_known_good(db, rel, p, now, facts)
        rel.known_good_id = kg.id
        rel.services = facts["services"]
        rel.tree_sha = facts.get("tree_sha")
        baseline = p.probe.check()
        rel.rollback = {**(rel.rollback or {}), "baseline_probe": baseline}
        self._set(rel, RS.PREVIEW_VALIDATING, now)

    def _s_preview_validating(self, db, rel, p, now, st):
        if p.preview is None:
            return self._refuse(db, rel, now, "no release-preview surface is configured (fail closed)", st)
        res = p.preview.validate(rel.head_sha)
        rel.verification = {**(rel.verification or {}), "preview": res}
        state = res.get("state")
        if state == "pending":
            return self._wait(rel, now, "release preview pending", st, seconds=60)
        if state != "passed":
            return self._refuse(db, rel, now, f"release preview failed: {str(res.get('reason', ''))[:200]}", st)
        branch = f"release/prod-{rel.head_sha[:12]}"
        p.github.ensure_branch(branch, rel.head_sha)
        att = rel.attestation or {}
        body = (
            "Automated MAINTENANCE_GREEN release by the deterministic Maintenance Release Controller (no model).\n\n"
            f"- incident: `{att.get('incident_id')}` case: `{att.get('repair_case_id')}`\n- exact sha: `{rel.head_sha}`\n- attestation digest: `{rel.attestation_digest}`\n"
            f"- changed files: {', '.join(att.get('changed_files') or [])}\n- known-good production sha: `{(rel.verification or {}).get('checks', {}).get('production_head')}`\n"
        )
        pr = p.github.open_pr(branch, self.rs.production_branch, f"release(maintenance): {rel.head_sha[:12]} for case {str(att.get('repair_case_id'))[:8]}", body)
        rel.release_branch, rel.pr_number, rel.pr_url = branch, pr.number, pr.url
        self._set(rel, RS.PR_OPEN, now, delay=30)

    def _s_pr_open(self, db, rel, p, now, st):
        pr = p.github.pr_state(rel.pr_number)
        if pr.merged:
            rel.production_merge_sha = pr.merge_sha
            self._set(rel, RS.MERGED, now)
            return self._s_merged(db, rel, p, now, st)
        expected_prod = ((rel.verification or {}).get("checks") or {}).get("production_head")
        if p.github.branch_head(self.rs.production_branch) != expected_prod:
            return self._refuse(db, rel, now, "production moved after verification; re-attestation required (no stale release)", st)
        if pr.head_sha != rel.head_sha:
            return self._refuse(db, rel, now, "release branch head is not the attested sha", st)
        if pr.mergeable is False:
            return self._refuse(db, rel, now, "production PR has a merge conflict (no force push, no rebase)", st)
        if pr.checks == "failed":
            return self._refuse(db, rel, now, "required production CI failed", st)
        if pr.checks != "passed" or pr.mergeable is None:
            return self._wait(rel, now, "production CI pending", st, seconds=60)
        if self._open_release_freeze(db):
            return self._wait(rel, now, "production release freeze open", st, seconds=120)
        rel.production_merge_sha = p.github.merge_pr(rel.pr_number, rel.head_sha)
        self._set(rel, RS.MERGED, now)
        self._s_merged(db, rel, p, now, st)

    def _s_merged(self, db, rel, p, now, st):
        rel.deployments = {**(rel.deployments or {}), "_merged_at": now.isoformat()}
        self._set(rel, RS.DEPLOYING, now, delay=0)

    def _s_deploying(self, db, rel, p, now, st):
        sha = rel.production_merge_sha
        deps = dict(rel.deployments or {})
        merged_at = datetime.fromisoformat(deps.get("_merged_at", now.isoformat()))
        states = {}
        for svc in rel.services or []:
            rec = deps.get(svc)
            if rec is None:
                # adopt a deployment Railway already started for this exact sha
                found = next((d for d in p.railway.deployments(svc, 10) if d.commit_sha == sha), None)
                if found is not None:
                    rec = {"id": found.id, "source": "adopted"}
                elif (now - merged_at).total_seconds() >= self.rs.deploy_grace_seconds:
                    rec = {"id": p.railway.deploy_sha(svc, sha), "source": "triggered"}
                    deps[svc] = rec
                    rel.deployments = deps
                    db.flush()  # the trigger is recorded before anything else can fail
                if rec is not None:
                    deps[svc] = rec
            if rec is not None:
                d = p.railway.get(rec["id"])
                states[svc] = d.status
            else:
                states[svc] = "WAITING_FOR_TRIGGER"
        rel.deployments = deps
        if any(s in ("FAILED", "CRASHED", "REMOVED") for s in states.values()):
            return self._start_rollback(db, rel, now, f"deployment failed: {states}", st)
        if all(s == "SUCCESS" for s in states.values()):
            self._set(rel, RS.POST_DEPLOY_VERIFYING, now, delay=self.settings.post_deploy_interval_seconds)
            return
        if now >= rel.deadline_at:
            return self._start_rollback(db, rel, now, f"deployment did not finish before the deadline: {states}", st)
        rel.next_action_at = now + timedelta(seconds=30)

    def _regressions(self, rel, probe: Dict[str, bool]) -> List[str]:
        baseline = (rel.rollback or {}).get("baseline_probe") or {}
        return sorted(k for k, ok in probe.items() if not ok and baseline.get(k, True))

    def _s_post_deploy_verifying(self, db, rel, p, now, st):
        probe = p.probe.check()
        reg = self._regressions(rel, probe)
        rel.verification = {**(rel.verification or {}), "last_probe": probe}
        if reg:
            return self._start_rollback(db, rel, now, f"post-deploy regression: {reg}", st)
        rel.healthy_streak = int(rel.healthy_streak or 0) + 1
        if rel.healthy_streak >= self.settings.post_deploy_healthy_observations:
            self._record_new_known_good(db, rel, p, now)
            self._set(rel, RS.SUCCEEDED, now)
            return
        rel.next_action_at = now + timedelta(seconds=self.settings.post_deploy_interval_seconds)

    def _start_rollback(self, db, rel, now, reason: str, st: ReleaseStats) -> None:
        rel.rollback = {**(rel.rollback or {}), "reason": reason[:300], "started_at": now.isoformat(), "services": {}}
        rel.healthy_streak = 0
        self._set(rel, RS.ROLLING_BACK, now, reason=reason)

    def _s_rolling_back(self, db, rel, p, now, st):
        kg = db.get(MaintenanceKnownGood, rel.known_good_id) if rel.known_good_id else None
        if kg is None:
            return self._rollback_failed(db, rel, now, "no known-good record to roll back to", st)
        rb = dict(rel.rollback or {})
        svcs = dict(rb.get("services") or {})
        states = {}
        for svc in rel.services or []:
            rec = svcs.get(svc)
            if rec is None:
                target = (kg.deployments or {}).get(svc)
                try:
                    if not target:
                        raise ProviderRefused("no known-good deployment id")
                    rec = {"id": p.railway.rollback(target), "method": "rollback", "to": target}
                except ProviderRefused:
                    # the retained image may have expired: redeploy the known-good exact sha
                    rec = {"id": p.railway.deploy_sha(svc, kg.production_sha), "method": "redeploy_known_good_sha", "to": kg.production_sha}
                svcs[svc] = rec
                rb["services"] = svcs
                rel.rollback = rb
                db.flush()
            states[svc] = p.railway.get(rec["id"]).status
        rb["services"] = svcs
        if any(s in ("FAILED", "CRASHED", "REMOVED") for s in states.values()):
            rel.rollback = rb
            return self._rollback_failed(db, rel, now, f"rollback deployment failed: {states}", st)
        if not all(s == "SUCCESS" for s in states.values()):
            rel.rollback = rb
            if now >= rel.deadline_at + timedelta(hours=1):
                return self._rollback_failed(db, rel, now, "rollback did not complete", st)
            rel.next_action_at = now + timedelta(seconds=30)
            return
        # recovery succeeds only when the public desired state is restored
        probe = p.probe.check()
        reg = self._regressions(rel, probe)
        rb["probes"] = (rb.get("probes") or [])[-10:] + [{"at": now.isoformat(), "regressions": reg}]
        rb["healthy"] = 0 if reg else int(rb.get("healthy") or 0) + 1
        rel.rollback = rb
        if reg:
            if now >= rel.deadline_at + timedelta(hours=1):
                return self._rollback_failed(db, rel, now, f"public desired state not restored after rollback: {reg}", st)
            rel.next_action_at = now + timedelta(seconds=self.settings.post_deploy_interval_seconds)
            return
        if rb["healthy"] < self.settings.post_deploy_healthy_observations:
            rel.next_action_at = now + timedelta(seconds=self.settings.post_deploy_interval_seconds)
            return
        rb["result"] = "restored"
        rel.rollback = rb
        self._branch_reconciliation(db, rel, p, kg, now)
        self._set(rel, RS.ROLLED_BACK, now)
        st.rolled_back += 1

    def _rollback_failed(self, db, rel, now, reason: str, st: ReleaseStats) -> None:
        rel.rollback = {**(rel.rollback or {}), "result": "failed", "failure": reason[:300]}
        self._freeze(db, "rollback_failed_p0", {"release_id": str(rel.id), "reason": reason[:300]}, owner_only=True, now=now)
        self._set(rel, RS.ROLLBACK_FAILED, now, reason=reason)

    def _branch_reconciliation(self, db, rel, p, kg: MaintenanceKnownGood, now) -> None:
        """Runtime is back on known-good but ``production`` still holds the
        bad release: open a PR whose tree IS the known-good tree, and freeze
        releases until branch and runtime agree (the parity sweep lifts it)."""
        prod_head = p.github.branch_head(self.rs.production_branch)
        tree = kg.tree_sha or p.github.tree_sha(kg.production_sha)
        branch = f"reconcile/prod-{str(rel.id)[:8]}"
        sha = p.github.commit_with_tree(branch, prod_head, tree, f"reconcile production with known-good {kg.production_sha[:12]} after automatic rollback of {rel.head_sha[:12]}")
        pr = p.github.open_pr(branch, self.rs.production_branch, f"reconcile(production): restore known-good {kg.production_sha[:12]}", f"Automatic rollback of release {rel.id} restored runtime to known-good `{kg.production_sha}`. This PR makes the production branch match it (tree `{tree}`).")
        rel.rollback = {**(rel.rollback or {}), "reconciliation": {"branch": branch, "commit": sha, "pr": pr.number, "url": pr.url, "tree": tree}}
        self._freeze(db, "rollback_parity", {"release_id": str(rel.id), "known_good_tree": tree, "pr": pr.number}, owner_only=False, now=now)

    def _parity_sweep(self, p: Providers, now: datetime) -> None:
        """Merge a green reconciliation PR, then lift the parity freeze once the
        production branch tree equals the known-good tree."""
        db = self.session_factory()
        try:
            for fr in db.query(MaintenanceReleaseFreeze).filter(MaintenanceReleaseFreeze.lifted_at.is_(None), MaintenanceReleaseFreeze.reason_code == "rollback_parity").all():
                d = fr.detail or {}
                try:
                    pr = p.github.pr_state(int(d["pr"]))
                    if not pr.merged and pr.checks == "passed" and pr.mergeable:
                        p.github.merge_pr(pr.number, pr.head_sha)
                    head = p.github.branch_head(self.rs.production_branch)
                    if p.github.tree_sha(head) == d.get("known_good_tree"):
                        fr.lifted_at, fr.lifted_by, fr.lift_reason = now, self.worker_id, "production branch tree equals the running known-good tree"
                except (ProviderTransient, ProviderRefused, KeyError, ValueError):
                    continue
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

    # ── independent verification ────────────────────────────────────────────
    def verify(self, db: Session, rel: MaintenanceRelease, p: Providers, now: datetime) -> Tuple[bool, bool, str, Dict[str, Any]]:
        """(ok, transient, reason, facts). Every check is recomputed here."""
        rs, s = self.rs, self.settings
        facts: Dict[str, Any] = {}
        att = rel.attestation or {}
        ok, why = att_mod.verify(att, rel.attestation_digest, rel.attestation_signature, require_signature=rs.require_signature)
        facts["attestation"] = why
        if not ok:
            return False, False, f"attestation: {why}", facts
        if att.get("merged_sha") != rel.head_sha or att.get("repair_case_id") != str(rel.case_id):
            return False, False, "attestation does not describe this release", facts
        case = db.get(RepairCase, rel.case_id)
        if case is None or case.state != sm.CaseState.RELEASING.value or case.release_id != rel.id:
            return False, False, "the repair case is not releasing this row", facts
        incident = db.get(MaintenanceIncident, rel.incident_id)
        # exact sha, ancestry, the diff that would really ship
        prod_head = p.github.branch_head(rs.production_branch)
        facts["production_head"] = prod_head
        cmp_main = p.github.compare(rel.head_sha, p.github.branch_head(rs.main_branch))
        if cmp_main.behind_by:
            return False, False, "the attested sha is not on main", facts
        cmp = p.github.compare(prod_head, rel.head_sha)
        if cmp.behind_by:
            return False, False, "production has commits that are not on main (branch parity broken)", facts
        if cmp.truncated:
            return False, False, "the production..sha diff is too large to verify completely", facts
        shipped = sorted(cmp.files)
        attested = sorted(att.get("changed_files") or [])
        facts["shipped_files"] = shipped[:100]
        if shipped != attested:
            return False, False, f"the release would ship unrelated unreleased changes: {sorted(set(shipped) - set(attested))[:10]}", facts
        decision = pol.classify_patch(shipped, cmp.patch, settings=s)
        facts["risk"] = decision.to_dict()
        risk = decision.risk_class
        if risk is MRC.AMBER:
            appr = att.get("owner_approval") or {}
            merged_by = p.github.pr_merged_by(int(appr["pr"])) if appr.get("pr") else None
            facts["owner_merged_by"] = merged_by
            if not (merged_by and merged_by in rs.owner_logins):
                return False, False, "AMBER release without a verified owner approval", facts
        elif risk is not MRC.MAINTENANCE_GREEN:
            return False, False, f"trusted classification is {risk.value}; autonomous release refused", facts
        checks = p.github.checks(rel.head_sha)
        facts["ci"] = checks
        if checks == "failed":
            return False, False, "required checks failed on the attested sha", facts
        if checks != "passed":
            return False, True, "required checks pending on the attested sha", facts
        try:
            facts["tree_sha"] = p.github.tree_sha(rel.head_sha)
        except ProviderRefused:
            facts["tree_sha"] = None
        # schema discovery before any mutation is attempted
        schema = p.railway.discover()
        facts["railway_schema"] = schema
        if not all(schema.values()):
            return False, False, f"Railway schema lacks required mutations: {schema}", facts
        if not p.railway.healthy():
            return False, True, "provider unhealthy; not deploying into it", facts
        if self._open_release_freeze(db):
            return False, True, "production release freeze open", facts
        foreign = [f for f in db.query(IncidentFreeze).filter(IncidentFreeze.lifted_at.is_(None)).all() if (f.evidence or {}).get("incident_id") != str(rel.incident_id) and f.source != "public_surface"]
        if foreign:
            return False, True, f"incident freeze open for another incident ({len(foreign)})", facts
        security = incident is not None and incident.incident_class == IncidentClass.SECURITY.value
        if not slo_mod.release_allowed_by_budget(db, target=s.target, priority=(case.priority if case else "P3"), security=security, now=now):
            return False, True, "error budget exhausted; only P0/P1 and security repairs release", facts
        if self._releases_today(db, now, exclude=rel.id) >= s.max_auto_releases_per_day:
            return False, True, f"daily autonomous release cap ({s.max_auto_releases_per_day}) reached", facts
        facts["services"] = services_for(shipped)
        return True, False, "ok", facts

    def _releases_today(self, db, now, *, exclude) -> int:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            db.query(MaintenanceRelease)
            .filter(MaintenanceRelease.id != exclude, MaintenanceRelease.created_at >= start, MaintenanceRelease.status.notin_([RS.REFUSED.value, RS.PENDING.value, RS.VERIFYING.value]))
            .count()
        )

    def _open_release_freeze(self, db) -> bool:
        return db.query(MaintenanceReleaseFreeze).filter(MaintenanceReleaseFreeze.lifted_at.is_(None)).first() is not None

    def _freeze(self, db, reason_code: str, detail: Dict[str, Any], *, owner_only: bool, now) -> None:
        db.add(MaintenanceReleaseFreeze(id=uuid.uuid4(), reason_code=reason_code, detail=detail, owner_only=owner_only, opened_by=self.worker_id, opened_at=now))

    def _record_known_good(self, db, rel, p: Providers, now, facts) -> MaintenanceKnownGood:
        deployments = {}
        for svc in sorted(set(self.rs.railway_service_ids) | set(facts.get("services") or [])):
            cur = p.railway.current(svc)
            if cur is not None:
                deployments[svc] = cur.id
        prod = facts["production_head"]
        kg = MaintenanceKnownGood(id=uuid.uuid4(), environment="production", production_sha=prod, tree_sha=p.github.tree_sha(prod), deployments=deployments,
                                  config_digest=None, evidence={"captured": "pre_release", "release_id": str(rel.id), "config": "not captured (variables are not readable with a release-scoped token)"},
                                  source=f"pre_release:{rel.id}", recorded_at=now)
        db.add(kg)
        db.flush()
        return kg

    def _record_new_known_good(self, db, rel, p: Providers, now) -> None:
        deployments = {svc: (rel.deployments or {}).get(svc, {}).get("id") for svc in rel.services or []}
        prev = db.get(MaintenanceKnownGood, rel.known_good_id) if rel.known_good_id else None
        merged = {**((prev.deployments or {}) if prev else {}), **{k: v for k, v in deployments.items() if v}}
        db.add(MaintenanceKnownGood(id=uuid.uuid4(), environment="production", production_sha=rel.production_merge_sha, tree_sha=rel.tree_sha, deployments=merged,
                                    config_digest=None, evidence={"release_id": str(rel.id), "healthy_observations": rel.healthy_streak}, source=f"release:{rel.id}", recorded_at=now))


__all__ = ["ReleaseController", "ReleaseSettings", "Providers", "ReleaseStats", "live_providers", "RELEASE_VERSION"]
