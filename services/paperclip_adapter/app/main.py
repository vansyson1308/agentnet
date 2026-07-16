"""Shadow-only Paperclip callback adapter.

The adapter validates and translates a ready WorkItem, then sends exactly one
command to AgentNet. It never selects a runtime and never creates Task/Run in
separate requests.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

app = FastAPI(title="AgentNet Paperclip Adapter", version="0.1.0")

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:8000").rstrip("/")
MANAGED_EXECUTION_SERVICE_TOKEN = os.environ["MANAGED_EXECUTION_SERVICE_TOKEN"]
PAPERCLIP_ADAPTER_TOKEN = os.environ["PAPERCLIP_ADAPTER_TOKEN"]


class Acceptance(BaseModel):
    commands: list[str] = Field(min_length=1)
    expected: dict[str, Any] = Field(default_factory=dict)


class ReadyWorkItem(BaseModel):
    event_id: str
    goal_id: str
    work_item_id: str
    revision: str
    attempt_no: int = 1
    role: str
    capability: str
    priority: int = 50
    repository: str
    base_commit_sha: str
    repository_scope: list[str] = Field(default_factory=list)
    prompt: str
    acceptance: Acceptance
    requirements: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    approval_policy_version: str
    trace_id: str
    required_runtime_id: str | None = None


def require_paperclip_adapter(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {PAPERCLIP_ADAPTER_TOKEN}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid Paperclip adapter token")


def logical_idempotency_key(item: ReadyWorkItem) -> str:
    logical = f"{item.work_item_id}:{item.revision}:{item.attempt_no}:{item.role}"
    return "paperclip:" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


@app.get("/healthz")
def health():
    return {"status": "ok", "economy_mode": "managed_shadow"}


@app.post("/v1/paperclip/work-items/ready", dependencies=[Depends(require_paperclip_adapter)])
async def ready_work_item(item: ReadyWorkItem):
    command = {
        "control_plane": "paperclip",
        "goal_id": item.goal_id,
        "work_item_id": item.work_item_id,
        "work_item_revision": item.revision,
        "attempt_no": item.attempt_no,
        "role": item.role,
        "capability": item.capability,
        "priority": item.priority,
        "repository": item.repository,
        "base_commit_sha": item.base_commit_sha,
        "repository_scope": item.repository_scope,
        "prompt": item.prompt,
        "acceptance": item.acceptance.model_dump(mode="json"),
        "requirements": item.requirements,
        "budgets": item.budgets,
        "economy_mode": "managed_shadow",
        "approval_policy_version": item.approval_policy_version,
        "idempotency_key": logical_idempotency_key(item),
        "trace_id": item.trace_id,
        "required_runtime_id": item.required_runtime_id,
    }
    headers = {
        "Authorization": f"Bearer {MANAGED_EXECUTION_SERVICE_TOKEN}",
        "Idempotency-Key": command["idempotency_key"],
        "X-Trace-ID": item.trace_id,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{REGISTRY_URL}/v1/managed-executions", json=command, headers=headers)
    if response.status_code in {409, 422, 503}:
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail"))
    response.raise_for_status()
    return response.json()
