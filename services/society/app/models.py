"""Society data models — governed agent platform layer."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Optional


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Permission ───

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "product_strategist": [
        "goal:create", "task:create", "roadmap:create", "improvement:evaluate",
    ],
    "project_manager": [
        "task:assign", "task:update_status", "task:escalate", "status:report",
    ],
    "architect": [
        "design:create", "decision:create", "architecture:review", "task:create_technical",
    ],
    "builder": [
        "task:start", "patch:create", "build:request", "task:submit_for_review",
        "evidence:create",
    ],
    "reviewer": [
        "review:create", "task:approve", "task:request_changes", "risk:flag",
    ],
    "qa": [
        "test:create", "test:run", "bug:create", "qa:approve", "qa:reject",
    ],
    "auditor": [
        "audit:create", "improvement:create", "risk:flag", "compliance:check",
    ],
    "memory": [
        "memory:create", "lesson:create", "context:summarize",
    ],
    "devops": [
        "release:prepare", "deployment:request", "rollback:prepare",
        "deployment:status_check",
    ],
}

HUMAN_ONLY_ACTIONS: set[str] = {
    "deployment:approve", "deployment:reject", "build:approve", "system:override",
}


class PermissionResult:
    def __init__(self, allowed: bool, reason: str = "",
                 required_permission: str = "", agent_role: str = ""):
        self.allowed = allowed
        self.reason = reason
        self.required_permission = required_permission
        self.agent_role = agent_role

    def dict(self) -> dict:
        return {
            "allowed": self.allowed, "reason": self.reason,
            "required_permission": self.required_permission,
            "agent_role": self.agent_role,
        }


# Hard governance rules
CANNOT_SELF_APPROVE_ROLES = {"builder", "qa", "devops"}
TASK_STATUS_IMMUTABLE_FOR = {
    "memory": None,  # memory cannot change any task status
}


def can_perform(agent: dict, action: str, resource: str = "",
                task: Optional[dict] = None) -> PermissionResult:
    """Central permission guard — must be called BEFORE any state mutation."""
    role = agent.get("role", "")
    agent_id = agent.get("id", "")

    # Human-only actions
    if action in HUMAN_ONLY_ACTIONS:
        return PermissionResult(False,
                                f"Action '{action}' requires human operator approval",
                                action, role)

    # Role-based permission list
    allowed_actions = ROLE_PERMISSIONS.get(role, [])
    if action not in allowed_actions:
        return PermissionResult(False,
                                f"Role '{role}' is not allowed to perform '{action}'",
                                action, role)

    # ─── Hard governance rules ───

    # Memory agent cannot modify any task status
    if role == "memory" and action in ("task:update_status", "task:approve",
                                        "task:start", "task:submit_for_review"):
        return PermissionResult(False,
                                "Memory Agent (Mnemosyne) cannot modify task status",
                                action, role)

    # Builder cannot approve own work
    if action == "task:approve" and task and agent_id == task.get("assignedTo"):
        if role in CANNOT_SELF_APPROVE_ROLES:
            return PermissionResult(False,
                                    f"{role.capitalize()} cannot approve a task assigned to themselves",
                                    action, role)

    # Reviewer cannot review own task
    if role == "reviewer" and action == "review:create" and task:
        if agent_id == task.get("assignedTo"):
            return PermissionResult(False,
                                    "Reviewer cannot review a task they are assigned to",
                                    action, role)

    # Auditor cannot deploy
    if role == "auditor" and "deploy" in action:
        return PermissionResult(False,
                                "Auditor cannot perform deployment actions", action, role)

    return PermissionResult(True, "", action, role)


TASK_STATUSES = [
    "BACKLOG", "READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED",
    "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED",
    "QA_TESTING", "QA_PASSED", "QA_FAILED",
    "AWAITING_HUMAN_APPROVAL", "DEPLOYMENT_READY",
    "DEPLOYED", "DONE", "FAILED", "CANCELLED",
]

VALID_TRANSITIONS: dict[str, list[str]] = {
    "BACKLOG": ["READY"],
    "READY": ["ASSIGNED"],
    "ASSIGNED": ["IN_PROGRESS", "BLOCKED"],
    "IN_PROGRESS": ["IN_REVIEW", "BLOCKED", "FAILED"],
    "IN_REVIEW": ["APPROVED", "CHANGES_REQUESTED", "BLOCKED"],
    "CHANGES_REQUESTED": ["IN_PROGRESS", "BLOCKED", "FAILED"],
    "APPROVED": ["QA_TESTING", "BLOCKED"],
    "QA_TESTING": ["QA_PASSED", "QA_FAILED", "BLOCKED"],
    "QA_FAILED": ["CHANGES_REQUESTED", "BLOCKED"],
    "QA_PASSED": ["AWAITING_HUMAN_APPROVAL", "BLOCKED"],
    "AWAITING_HUMAN_APPROVAL": ["DEPLOYMENT_READY", "FAILED"],
    "DEPLOYMENT_READY": ["DEPLOYED", "FAILED"],
    "DEPLOYED": ["DONE", "FAILED"],
    # Terminal states — no transitions from these
    "DONE": [],
    "FAILED": [],
    "CANCELLED": [],
    "BLOCKED": ["IN_PROGRESS", "CHANGES_REQUESTED", "FAILED", "CANCELLED"],
}
TERMINAL_STATUSES = {"DONE", "FAILED", "CANCELLED"}


def validate_transition(from_status: str, to_status: str) -> tuple[bool, str]:
    from_s = from_status.upper()
    to_s = to_status.upper()
    allowed = VALID_TRANSITIONS.get(from_s, [])
    if to_s not in allowed:
        return False, f"Invalid transition: {from_s} -> {to_s}"
    return True, ""


# ─── Data Models ───

class TaskHistoryEntry(dict):
    @staticmethod
    def create(task_id: str, from_status: str, to_status: str,
               actor_id: str, reason: str = "",
               evidence_ids: Optional[list[str]] = None) -> dict:
        return {
            "id": new_id("th-"),
            "taskId": task_id,
            "fromStatus": from_status,
            "toStatus": to_status,
            "actorId": actor_id,
            "reason": reason,
            "evidenceIds": evidence_ids or [],
            "createdAt": now_iso(),
        }


class Evidence(dict):
    @staticmethod
    def create(created_by: str, etype: str, title: str,
               summary: str = "", content: str = "",
               task_id: Optional[str] = None,
               metadata: Optional[dict] = None) -> dict:
        return {
            "id": new_id("evd-"),
            "taskId": task_id,
            "createdBy": created_by,
            "type": etype,
            "title": title,
            "summary": summary or title,
            "content": content,
            "metadata": metadata or {},
            "createdAt": now_iso(),
        }


class ToolCall(dict):
    @staticmethod
    def create(agent_id: str, tool_name: str,
               inp: Optional[dict] = None,
               task_id: Optional[str] = None) -> dict:
        return {
            "id": new_id("tc-"),
            "agentId": agent_id,
            "taskId": task_id,
            "toolName": tool_name,
            "input": inp or {},
            "output": {},
            "status": "PENDING",
            "error": None,
            "evidenceId": None,
            "createdAt": now_iso(),
            "completedAt": None,
        }


class BuildRequest(dict):
    @staticmethod
    def create(task_id: str, requested_by: str, title: str,
               description: str = "",
               patch_proposal_evidence_id: Optional[str] = None) -> dict:
        return {
            "id": new_id("br-"),
            "taskId": task_id,
            "requestedBy": requested_by,
            "title": title,
            "description": description,
            "patchProposalEvidenceId": patch_proposal_evidence_id,
            "status": "DRAFT",
            "requiresHumanApproval": True,
            "humanApprovalStatus": "NOT_REQUIRED",
            "buildResult": None,
            "errorMessage": None,
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
        }


class AgentTrace(dict):
    @staticmethod
    def create(tick_number: int, agent_id: str, phase: str = "decide",
               observation_summary: str = "",
               selected_action: str = "",
               reasoning_summary: str = "") -> dict:
        return {
            "id": new_id("trace-"),
            "tickNumber": tick_number,
            "agentId": agent_id,
            "phase": phase,
            "observationSummary": observation_summary,
            "selectedAction": selected_action,
            "reasoningSummary": reasoning_summary,
            "llmRequestId": None,
            "llmModel": None,
            "llmLatencyMs": None,
            "toolCallIds": [],
            "eventIds": [],
            "messageIds": [],
            "evidenceIds": [],
            "memoryIds": [],
            "errors": [],
            "createdAt": now_iso(),
        }
