from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
import httpx
from datetime import datetime

from ...database import get_db
from ...models import (
    ApprovalRequest, User, Agent, Wallet, 
    ApprovalStatus, WalletOwnerType
)
from ...schemas import (
    ApprovalRequestCreate, ApprovalRequestUpdate, 
    ApprovalRequest as ApprovalRequestSchema, ApprovalResponse
)
from ...auth import get_current_user, get_current_agent

router = APIRouter()

@router.post("/", response_model=ApprovalResponse)
async def create_approval_request(
    approval_request: ApprovalRequestCreate,
    db: Session = Depends(get_db),
    current_agent: Agent = Depends(get_current_agent)
):
    """Create an approval request for user (if spending exceeds auto-approve threshold)."""
    # Get the agent's wallet
    wallet = db.query(Wallet).filter(
        Wallet.owner_type == WalletOwnerType.AGENT,
        Wallet.owner_id == current_agent.id
    ).first()
    
    if not wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent wallet not found"
        )
    
    # Check if the amount is below the auto-approve threshold
    if wallet.auto_approve_threshold is not None and approval_request.amount <= wallet.auto_approve_threshold:
        return ApprovalResponse(
            approval_id=uuid.uuid4(),  # Dummy ID since we're auto-approving
            status="approved",
            message="Amount is below auto-approve threshold, no approval needed"
        )
    
    # Get the agent's owner (user)
    user = db.query(User).filter(User.id == current_agent.user_id).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent owner not found"
        )
    
    # Create the approval request
    db_approval_request = ApprovalRequest(
        id=uuid.uuid4(),
        agent_id=current_agent.id,
        user_id=user.id,
        amount=approval_request.amount,
        currency=approval_request.currency,
        description=approval_request.description,
        callback_url=approval_request.callback_url,
        status=ApprovalStatus.PENDING
    )
    
    db.add(db_approval_request)
    db.commit()
    db.refresh(db_approval_request)
    
    # Send notification to user (via Telegram bot or other channels)
    # This would typically be handled by a separate service
    # For now, we'll just log it
    
    return ApprovalResponse(
        approval_id=db_approval_request.id,
        status=db_approval_request.status,
        message="Approval request created successfully"
    )

@router.post("/{approval_id}/respond", response_model=ApprovalResponse)
async def respond_to_approval_request(
    approval_id: uuid.UUID,
    response: ApprovalRequestUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """User approves or denies."""
    # Get the approval request
    approval_request = db.query(ApprovalRequest).filter(
        ApprovalRequest.id == approval_id
    ).first()
    
    if not approval_request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval request not found"
        )
    
    # Check if the user is the owner of the approval request
    if approval_request.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this approval request"
        )
    
    # Check if the approval request is already responded to
    if approval_request.status != ApprovalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Approval request is already {approval_request.status}"
        )
    
    # Update the approval request status
    approval_request.status = ApprovalStatus.APPROVED if response.approved else ApprovalStatus.DENIED
    approval_request.responded_at = datetime.utcnow()
    db.commit()
    
    # If approved, update the agent's wallet auto_approve_threshold
    if response.approved:
        wallet = db.query(Wallet).filter(
            Wallet.owner_type == WalletOwnerType.AGENT,
            Wallet.owner_id == approval_request.agent_id
        ).first()
        
        if wallet and (wallet.auto_approve_threshold is None or approval_request.amount > wallet.auto_approve_threshold):
            # Increase the auto-approve threshold to the approved amount
            wallet.auto_approve_threshold = approval_request.amount
            db.commit()
    
    # If there's a callback URL, send the response
    if approval_request.callback_url:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    approval_request.callback_url,
                    json={
                        "approval_id": str(approval_request.id),
                        "approved": response.approved,
                        "amount": approval_request.amount,
                        "currency": approval_request.currency
                    }
                )
        except httpx.RequestError:
            # Log the error but don't fail the response
            pass
    
    return ApprovalResponse(
        approval_id=approval_request.id,
        status=approval_request.status,
        message=f"Approval request {approval_request.status} successfully"
    )

@router.get("/", response_model=List[ApprovalRequestSchema])
async def list_approval_requests(
    status: Optional[str] = Query(None, description="Filter by status"),
    skip: int = Query(0, ge=0, description="Skip records"),
    limit: int = Query(100, ge=1, le=1000, description="Limit records"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List approval requests for the current user."""
    # Build the query
    query = db.query(ApprovalRequest).filter(ApprovalRequest.user_id == current_user.id)
    
    # Apply filters
    if status:
        query = query.filter(ApprovalRequest.status == status)
    
    # Execute query with pagination
    approval_requests = query.order_by(ApprovalRequest.created_at.desc()).offset(skip).limit(limit).all()
    
    return approval_requests