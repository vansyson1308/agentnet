"""
Approval Workflow Endpoints for Task Escrow Payments.

Features:
- Idempotent approve/deny actions
- Proper state machine transitions
- Authorization: only the user who owns the agent can approve
- Worker integration for expiry handling
"""

import uuid
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...approval_workflow import (
    APPROVAL_ALLOWED_TRANSITIONS,
    EscrowApprovalStatus,
    get_approval_timeout,
    is_idempotent_action,
    validate_approval_transition,
)
from ...auth import get_current_agent, get_current_user
from ...database import get_db
from ...models import (
    Agent,
    ApprovalRequest,
    ApprovalStatus,
    CurrencyType,
    TaskSession,
    TaskStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    Wallet,
    WalletOwnerType,
)
from ...schemas import ApprovalRequest as ApprovalRequestSchema
from ...schemas import ApprovalRequestCreate, ApprovalRequestUpdate, ApprovalResponse

router = APIRouter()


# ─────────────────────────────────────────────────────────
# Create Approval Request
# ─────────────────────────────────────────────────────────


@router.post("/", response_model=ApprovalResponse)
async def create_approval_request(
    approval_request: ApprovalRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create an approval request.

    Can be used for:
    1. Task escrow payment approval (task_session_id provided)
    2. Wallet auto-approve threshold increase (legacy)
    """
    # Check if this is a task escrow approval
    if approval_request.task_session_id:
        # Validate task session exists and belongs to user's agent
        task = db.query(TaskSession).filter(TaskSession.id == approval_request.task_session_id).first()

        if not task:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task session not found")

        # Get the caller agent and verify ownership
        caller_agent = db.query(Agent).filter(Agent.id == task.caller_agent_id).first()

        if not caller_agent or caller_agent.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the task caller's owner can create approval request",
            )

        # Check task status is appropriate for approval
        if task.status not in [TaskStatus.INITIATED, TaskStatus.IN_PROGRESS]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Task is in {task.status} status, cannot create approval",
            )

        # Check if approval already exists for this task
        existing = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.task_session_id == approval_request.task_session_id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
            .first()
        )

        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Pending approval already exists for this task",
            )

        agent_id = task.caller_agent_id
    else:
        # Legacy: auto-approve threshold request
        agent = db.query(Agent).filter(Agent.id == approval_request.agent_id).first()

        if not agent or agent.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to create approval for this agent",
            )
        agent_id = agent.id

    # Get wallet
    wallet = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == agent_id).first()

    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent wallet not found")

    # Calculate expiry
    expires_at = get_approval_timeout(approval_request.expires_in_hours)

    # Create approval request
    db_approval = ApprovalRequest(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=current_user.id,
        amount=approval_request.amount,
        currency=CurrencyType(approval_request.currency.upper()),
        description=approval_request.description,
        callback_url=approval_request.callback_url,
        status=ApprovalStatus.PENDING,
        task_session_id=approval_request.task_session_id,
        expires_at=expires_at,
    )

    db.add(db_approval)
    db.commit()
    db.refresh(db_approval)

    # Send notification stub
    await _send_approval_notification(db, db_approval, "created")

    return ApprovalResponse(
        approval_id=db_approval.id,
        status=db_approval.status.value,
        message="Approval request created",
    )


# ─────────────────────────────────────────────────────────
# Approve Request
# ─────────────────────────────────────────────────────────


@router.post("/{approval_id}/approve", response_model=ApprovalResponse)
async def approve_request(
    approval_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Approve an approval request.

    Idempotent: approving an already APPROVED request returns success (no-op).
    """
    approval = db.query(ApprovalRequest).filter(ApprovalRequest.id == approval_id).first()

    if not approval:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")

    # Authorization: only the user who owns the approval can approve
    if approval.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to approve this request",
        )

    # Check current status
    current_status = EscrowApprovalStatus(approval.status.value)

    # Idempotency check
    if is_idempotent_action(current_status, EscrowApprovalStatus.APPROVED):
        return ApprovalResponse(
            approval_id=approval.id,
            status=approval.status.value,
            message="Already approved (idempotent)",
        )

    # Validate transition
    is_valid, error = validate_approval_transition(current_status, EscrowApprovalStatus.APPROVED)

    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    # Update approval
    now = datetime.utcnow()
    approval.status = ApprovalStatus.APPROVED
    approval.responded_at = now
    approval.approved_at = now

    # If this is a task escrow approval, also update the transaction
    if approval.task_session_id:
        task = db.query(TaskSession).filter(TaskSession.id == approval.task_session_id).first()

        if task:
            # Find pending transaction for this task
            transaction = (
                db.query(Transaction)
                .filter(
                    Transaction.task_session_id == approval.task_session_id,
                    Transaction.status == TransactionStatus.PENDING,
                )
                .first()
            )

            if transaction:
                # Mark transaction as approved
                metadata = transaction.metadata or {}
                metadata["approved"] = True
                metadata["approved_at"] = now.isoformat()
                transaction.metadata = metadata

    db.commit()
    db.refresh(approval)

    # Send notification
    await _send_approval_notification(db, approval, "approved")

    # Callback if provided
    if approval.callback_url:
        await _send_callback(
            approval.callback_url,
            {
                "approval_id": str(approval.id),
                "status": "approved",
                "amount": approval.amount,
                "currency": approval.currency.value,
            },
        )

    return ApprovalResponse(
        approval_id=approval.id,
        status=approval.status.value,
        message="Approved successfully",
    )


# ─────────────────────────────────────────────────────────
# Deny Request
# ─────────────────────────────────────────────────────────


@router.post("/{approval_id}/deny", response_model=ApprovalResponse)
async def deny_request(
    approval_id: uuid.UUID,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Deny an approval request.

    Idempotent: denying an already DENIED request returns success (no-op).
    When denying a task escrow approval, reserved funds are released.
    """
    approval = db.query(ApprovalRequest).filter(ApprovalRequest.id == approval_id).first()

    if not approval:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")

    # Authorization
    if approval.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to deny this request",
        )

    # Check current status
    current_status = EscrowApprovalStatus(approval.status.value)

    # Idempotency
    if is_idempotent_action(current_status, EscrowApprovalStatus.DENIED):
        return ApprovalResponse(
            approval_id=approval.id,
            status=approval.status.value,
            message="Already denied (idempotent)",
        )

    # Validate transition
    is_valid, error = validate_approval_transition(current_status, EscrowApprovalStatus.DENIED)

    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    # Update approval
    now = datetime.utcnow()
    approval.status = ApprovalStatus.DENIED
    approval.responded_at = now
    approval.denied_at = now
    if reason:
        approval.description = f"{approval.description}\n\nDenial reason: {reason}"

    # If this is a task escrow approval, release reserved funds
    if approval.task_session_id:
        task = db.query(TaskSession).filter(TaskSession.id == approval.task_session_id).first()

        if task:
            # Release reserved funds
            wallet = (
                db.query(Wallet)
                .filter(
                    Wallet.owner_type == WalletOwnerType.AGENT,
                    Wallet.owner_id == task.caller_agent_id,
                )
                .first()
            )

            if wallet:
                if task.currency == CurrencyType.CREDITS:
                    wallet.reserved_credits = max(0, wallet.reserved_credits - task.escrow_amount)
                else:
                    wallet.reserved_usdc = max(0, float(wallet.reserved_usdc) - task.escrow_amount)

            # Update transaction
            transaction = (
                db.query(Transaction)
                .filter(
                    Transaction.task_session_id == approval.task_session_id,
                    Transaction.status == TransactionStatus.PENDING,
                )
                .first()
            )

            if transaction:
                transaction.status = TransactionStatus.CANCELLED
                metadata = transaction.metadata or {}
                metadata["denied"] = True
                metadata["denied_at"] = now.isoformat()
                transaction.metadata = metadata

            # Mark task as failed
            task.status = TaskStatus.FAILED
            task.error_message = f"Payment denied by user: {reason or 'No reason'}"

    db.commit()
    db.refresh(approval)

    # Send notification
    await _send_approval_notification(db, approval, "denied")

    return ApprovalResponse(
        approval_id=approval.id,
        status=approval.status.value,
        message="Denied successfully",
    )


# ─────────────────────────────────────────────────────────
# List Approvals
# ─────────────────────────────────────────────────────────


@router.get("/", response_model=List[ApprovalRequestSchema])
async def list_approvals(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    task_session_id: Optional[uuid.UUID] = Query(None, description="Filter by task session"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List approval requests for the current user."""
    query = db.query(ApprovalRequest).filter(ApprovalRequest.user_id == current_user.id)

    if status_filter:
        query = query.filter(ApprovalRequest.status == status_filter)

    if task_session_id:
        query = query.filter(ApprovalRequest.task_session_id == task_session_id)

    approvals = query.order_by(ApprovalRequest.created_at.desc()).offset(skip).limit(limit).all()

    return approvals


@router.get("/{approval_id}", response_model=ApprovalRequestSchema)
async def get_approval(
    approval_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific approval request."""
    approval = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.id == approval_id,
            ApprovalRequest.user_id == current_user.id,
        )
        .first()
    )

    if not approval:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")

    return approval


# ─────────────────────────────────────────────────────────
# Worker: Expire Pending Approvals
# ─────────────────────────────────────────────────────────


@router.post("/worker/expire")
async def expire_pending_approvals(db: Session = Depends(get_db)):
    """
    Worker endpoint to expire pending approvals that have timed out.

    This should be called periodically by the worker service.
    On expiry, reserved funds are released for task escrow approvals.
    """
    now = datetime.utcnow()

    # Find expired pending approvals
    expired = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.status == ApprovalStatus.PENDING,
            ApprovalRequest.expires_at < now,
        )
        .all()
    )

    expired_count = 0

    for approval in expired:
        # Update status
        approval.status = ApprovalStatus.DENIED
        approval.responded_at = now
        approval.denied_at = now
        approval.description = f"{approval.description}\n\nExpired: auto-denied by system"

        # If task escrow approval, release reserved funds
        if approval.task_session_id:
            task = db.query(TaskSession).filter(TaskSession.id == approval.task_session_id).first()

            if task and task.status in [TaskStatus.INITIATED, TaskStatus.IN_PROGRESS]:
                # Release reserved funds
                wallet = (
                    db.query(Wallet)
                    .filter(
                        Wallet.owner_type == WalletOwnerType.AGENT,
                        Wallet.owner_id == task.caller_agent_id,
                    )
                    .first()
                )

                if wallet:
                    if task.currency == CurrencyType.CREDITS:
                        wallet.reserved_credits = max(0, wallet.reserved_credits - task.escrow_amount)
                    else:
                        wallet.reserved_usdc = max(0, float(wallet.reserved_usdc) - task.escrow_amount)

                # Cancel transaction
                transaction = (
                    db.query(Transaction)
                    .filter(
                        Transaction.task_session_id == approval.task_session_id,
                        Transaction.status == TransactionStatus.PENDING,
                    )
                    .first()
                )

                if transaction:
                    transaction.status = TransactionStatus.CANCELLED
                    metadata = transaction.metadata or {}
                    metadata["expired"] = True
                    transaction.metadata = metadata

                # Mark task as failed
                task.status = TaskStatus.FAILED
                task.error_message = "Payment approval expired"

        expired_count += 1

    db.commit()

    return {
        "expired_count": expired_count,
        "message": f"Expired {expired_count} pending approvals",
    }


# ─────────────────────────────────────────────────────────
# Notification Helpers (Stubs)
# ─────────────────────────────────────────────────────────


async def _send_approval_notification(db: Session, approval: ApprovalRequest, action: str):
    """
    Send notification about approval action.

    Stub: logs to console. In production, integrate with:
    - Telegram bot
    - Email service
    - WebSocket push
    """
    print(f"[NOTIFICATION] Approval {action}: ID={approval.id}, amount={approval.amount} {approval.currency.value}")

    # Future: publish to Redis for telegram-bot to consume
    # For now, this is a stub


async def _send_callback(url: str, data: dict):
    """Send callback to configured URL."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=data, timeout=10.0)
    except httpx.RequestError as e:
        print(f"[CALLBACK] Failed to send callback to {url}: {e}")
