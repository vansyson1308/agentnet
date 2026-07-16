"""Contract tests for the managed-shadow vertical-slice foundation."""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys
import uuid

import pytest
from pydantic import ValidationError

REPO = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = REPO / "services" / "registry"
sys.path.insert(0, str(REGISTRY))

from app.managed_execution_service import canonical_request_hash  # noqa: E402
from app.managed_models import Lease, ManagedExecution  # noqa: E402
from app.managed_schemas import AcceptanceSnapshot, ManagedExecutionCreate  # noqa: E402
from app.runtime_allocator import _matches_repository, _permissions_satisfy  # noqa: E402

from app import artifact_store  # noqa: E402


def _command(**changes):
    values = {
        "goal_id": "goal-1",
        "work_item_id": "issue-7",
        "work_item_revision": "4",
        "role": "builder",
        "capability": "repository.edit",
        "repository": "https://github.com/example/repo.git",
        "base_commit_sha": "a" * 40,
        "prompt": "Implement the accepted change",
        "acceptance": {"commands": ["pytest -q"]},
        "approval_policy_version": "shadow-v1",
        "idempotency_key": "paperclip:stable-key",
        "trace_id": uuid.uuid4(),
    }
    values.update(changes)
    return ManagedExecutionCreate(**values)


def test_acceptance_rejects_placeholders():
    with pytest.raises(ValidationError):
        AcceptanceSnapshot(commands=["test is true"])


def test_request_hash_is_canonical_and_payload_bound():
    command = _command(requirements={"provider": "openai", "model": "codex"})
    same = _command(
        trace_id=command.trace_id,
        requirements={"model": "codex", "provider": "openai"},
    )
    changed = _command(trace_id=command.trace_id, prompt="A different request")
    assert canonical_request_hash(command) == canonical_request_hash(same)
    assert canonical_request_hash(command) != canonical_request_hash(changed)


def test_runtime_scope_and_permissions_are_deny_by_default():
    repository = "https://github.com/example/repo.git"
    assert not _matches_repository(repository, [])
    assert _matches_repository(repository, ["https://github.com/example/*"])
    assert _permissions_satisfy({"tools": ["git", "codex"], "network": False}, {"tools": ["git"]})
    assert not _permissions_satisfy({"tools": ["git"]}, {"tools": ["git", "codex"]})


def test_partial_unique_lease_constraints_cover_offered_ack_active():
    indexes = {index.name: str(index.dialect_options["postgresql"]["where"]) for index in Lease.__table__.indexes}
    assert "offered" in indexes["uq_active_lease_per_run"]
    assert "acknowledged" in indexes["uq_active_lease_per_runtime_slot"]
    assert "active" in indexes["uq_active_lease_per_runtime_slot"]


def test_managed_execution_has_task_and_idempotency_uniqueness():
    assert ManagedExecution.__table__.c.task_session_id.unique
    assert ManagedExecution.__table__.c.idempotency_key.unique
    logical = next(
        constraint
        for constraint in ManagedExecution.__table__.constraints
        if constraint.name == "uq_managed_execution_work_attempt_role"
    )
    assert [column.name for column in logical.columns] == [
        "control_plane",
        "work_item_id",
        "work_item_revision",
        "external_attempt_no",
        "role",
    ]


def test_artifact_store_hashes_and_atomically_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_PATH", str(tmp_path))
    content = b"small patch"
    digest = hashlib.sha256(content).hexdigest()
    uri = artifact_store.store_bytes(content, digest, len(content))
    assert uri == f"sha256/{digest[:2]}/{digest}"
    assert (tmp_path / uri).read_bytes() == content
    assert artifact_store.store_bytes(content, digest, len(content)) == uri


def test_adapter_duplicate_callbacks_share_one_logical_key(monkeypatch):
    monkeypatch.setenv("MANAGED_EXECUTION_SERVICE_TOKEN", "managed-test-token")
    monkeypatch.setenv("PAPERCLIP_ADAPTER_TOKEN", "paperclip-test-token")
    path = REPO / "services" / "paperclip_adapter" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("paperclip_adapter_test_main", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    base = {
        "goal_id": "g1",
        "work_item_id": "w1",
        "revision": "7",
        "role": "builder",
        "capability": "repository.edit",
        "repository": "repo",
        "base_commit_sha": "a" * 40,
        "prompt": "do it",
        "acceptance": {"commands": ["pytest"]},
        "approval_policy_version": "v1",
        "trace_id": str(uuid.uuid4()),
    }
    first = module.ReadyWorkItem(event_id="delivery-1", **base)
    duplicate = module.ReadyWorkItem(event_id="delivery-100", **base)
    assert module.logical_idempotency_key(first) == module.logical_idempotency_key(duplicate)


def test_shadow_service_never_uses_financial_transaction_model():
    source = (REGISTRY / "app" / "managed_execution_service.py").read_text(encoding="utf-8")
    assert "Transaction(" not in source
    assert "escrow_amount=0" in source
    assert source.count("db.commit()") == 1


def test_legacy_dispatchers_are_disabled_by_default():
    main = (REGISTRY / "app" / "main.py").read_text(encoding="utf-8")
    worker = (REPO / "services" / "worker" / "app" / "worker.py").read_text(encoding="utf-8")
    assert 'LEGACY_AUTO_SCALER_ENABLED", "false"' in main
    assert 'LEGACY_BACKLOG_EXPORT_ENABLED", "false"' in worker


def test_managed_routes_are_mounted_once_under_v1():
    routes = (REGISTRY / "app" / "api" / "routes" / "__init__.py").read_text(encoding="utf-8")
    api = (REGISTRY / "app" / "api" / "__init__.py").read_text(encoding="utf-8")
    assert 'prefix="/v1"' in api
    assert 'prefix="/managed-executions"' in routes
    assert 'prefix="/v1/managed-executions"' not in routes
