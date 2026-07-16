"""Versioned request/response contracts for the managed execution API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .managed_models import ManagedExecutionStatus, RunStatus, RuntimeStatus


class AcceptanceSnapshot(BaseModel):
    commands: list[str] = Field(min_length=1, max_length=50)
    expected: dict[str, Any] = Field(default_factory=dict)

    @field_validator("commands")
    @classmethod
    def validate_commands(cls, commands: list[str]) -> list[str]:
        placeholders = {"true", "pass", "test is true", "todo", "tbd"}
        normalized = [command.strip() for command in commands]
        if any(not command or command.lower() in placeholders for command in normalized):
            raise ValueError("acceptance commands must be executable and cannot be placeholders")
        return normalized


class ManagedExecutionCreate(BaseModel):
    control_plane: Literal["paperclip"] = "paperclip"
    goal_id: str = Field(min_length=1, max_length=255)
    work_item_id: str = Field(min_length=1, max_length=255)
    work_item_revision: str = Field(min_length=1, max_length=128)
    attempt_no: int = Field(default=1, ge=1)
    role: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=255)
    priority: int = Field(default=50, ge=0, le=100)
    repository: str = Field(min_length=1, max_length=1024)
    base_commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    repository_scope: list[str] = Field(default_factory=list)
    prompt: str = Field(min_length=1)
    acceptance: AcceptanceSnapshot
    requirements: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    economy_mode: Literal["managed_shadow"] = "managed_shadow"
    approval_policy_version: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=255)
    trace_id: uuid.UUID
    required_runtime_id: uuid.UUID | None = None


class ManagedExecutionResponse(BaseModel):
    id: uuid.UUID
    task_session_id: uuid.UUID
    initial_run_id: uuid.UUID
    lease_id: uuid.UUID
    runtime_id: uuid.UUID
    status: ManagedExecutionStatus
    run_status: RunStatus
    created_at: datetime
    idempotent_replay: bool = False


class RuntimeRegister(BaseModel):
    registration_key: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    agent_id: uuid.UUID | None = None
    role: str = Field(min_length=1, max_length=64)
    adapter: str = Field(min_length=1, max_length=64)
    capabilities: list[str] = Field(default_factory=list)
    repository_scopes: list[str] = Field(default_factory=list)
    permissions: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    provider: str | None = None
    capacity: int = Field(default=1, ge=1, le=128)
    extra_data: dict[str, Any] = Field(default_factory=dict)


class RuntimeRegisterResponse(BaseModel):
    id: uuid.UUID
    token: str
    status: RuntimeStatus
    capacity: int
    created: bool


class RuntimeHeartbeatRequest(BaseModel):
    sequence: int = Field(ge=1)
    run_id: uuid.UUID | None = None
    lease_id: uuid.UUID | None = None
    resources: dict[str, Any] = Field(default_factory=dict)


class AssignmentClaimResponse(BaseModel):
    lease_id: uuid.UUID
    lease_token: str
    lease_expires_at: datetime
    run_id: uuid.UUID
    managed_execution_id: uuid.UUID
    role: str
    capability: str
    repository: str
    base_commit_sha: str
    prompt: str
    acceptance: dict[str, Any]
    budgets: dict[str, Any]
    trace_id: uuid.UUID


class RunHeartbeatRequest(BaseModel):
    lease_token: str = Field(min_length=20)
    sequence: int = Field(ge=1)


class RunEventRequest(BaseModel):
    lease_token: str = Field(min_length=20)
    sequence: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=255)
    trace_id: uuid.UUID
    event_type: Literal["run.started", "run.progress", "run.artifact_submitted"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactCreate(BaseModel):
    lease_token: str = Field(min_length=20)
    artifact_type: Literal["manifest", "patch", "test_result", "log", "archive", "generated_file"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    mime_type: str = Field(min_length=1, max_length=255)
    content_base64: str | None = None
    uri: str | None = None
    base_commit_sha: str | None = None
    candidate_commit_sha: str | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def content_or_uri(self):
        if (self.content_base64 is None) == (self.uri is None):
            raise ValueError("provide exactly one of content_base64 or uri")
        return self


class RunTerminalRequest(BaseModel):
    lease_token: str = Field(min_length=20)
    sequence: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=255)
    trace_id: uuid.UUID
    candidate_commit_sha: str | None = None
    error: str | None = None
