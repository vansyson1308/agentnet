from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime

from ...database import get_db
from ...models import (
    Transaction, Wallet, User, Agent, 
    TransactionStatus, TransactionType, CurrencyType,
    WalletOwnerType
)
from ...schemas import TransactionCreate, TransactionUpdate, Transaction as TransactionSchema
from ...auth import get_current_user, get_current_agent, get_current_user_or_agent

router = APIRouter()

@router.post("/create", response_model=Dict[str, Any])
async def create_transaction(
    transaction: TransactionCreate,
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent)
):
    """Initiate a transaction."""
    # Get the source wallet
    from_wallet = db.query(Wallet).filter(Wallet.id == transaction.from_wallet).first()
    
    if not from_wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source wallet not found"
        )
    
    # Check if the current user/agent has access to the source wallet
    if from_wallet.owner_type == WalletOwnerType.USER:
        if isinstance(current_user_or_agent, User) and from_wallet.owner_id != current_user_or_agent.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to the source wallet"
            )
    elif from_wallet.owner_type == WalletOwnerType.AGENT:
        if isinstance(current_user_or_agent, Agent) and from_wallet.owner_id != current_user_or_agent.id:
            # Check if the wallet belongs to an agent owned by the user
            if isinstance(current_user_or_agent, User):
                agent = db.query(Agent).filter(
                    Agent.id == from_wallet.owner_id,
                    Agent.user_id == current_user_or_agent.id
                ).first()
                
                if not agent:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You don't have access to the source wallet"
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have access to the source wallet"
                )
    
    # Get the destination wallet
    to_wallet = db.query(Wallet).filter(Wallet.id == transaction.to_wallet).first()
    
    if not to_wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Destination wallet not found"
        )
    
    # Check if the source and destination wallets are the same
    if from_wallet.id == to_wallet.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source and destination wallets cannot be the same"
        )
    
    # Check if the source wallet has sufficient balance
    if transaction.currency == "credits":
        if from_wallet.balance_credits < transaction.amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient credits balance: required {transaction.amount}, available {from_wallet.balance_credits}"
            )
        
        # Check if the transaction would exceed the spending cap
        if from_wallet.daily_spent + transaction.amount > from_wallet.spending_cap:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Transaction would exceed daily spending cap of {from_wallet.spending_cap}"
            )
    else:
        # USDC handling
        if from_wallet.balance_usdc < transaction.amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient USDC balance: required {transaction.amount}, available {from_wallet.balance_usdc}"
            )
    
    # Create the transaction
    db_transaction = Transaction(
        id=uuid.uuid4(),
        from_wallet=from_wallet.id,
        to_wallet=to_wallet.id,
        amount=transaction.amount,
        currency=CurrencyType[transaction.currency.upper()],
        status=TransactionStatus.PENDING,
        type=TransactionType[transaction.type.upper()],
        metadata=transaction.metadata or {}
    )
    
    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)
    
    return {
        "transaction_id": str(db_transaction.id)
    }

@router.post("/{transaction_id}/confirm")
async def confirm_transaction(
    transaction_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Confirm a pending transaction (used after escrow release)."""
    # Get the transaction
    transaction = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    
    if not transaction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found"
        )
    
    # Check if the transaction is in pending status
    if transaction.status != TransactionStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Transaction is in {transaction.status} status, cannot confirm"
        )
    
    # Get the source and destination wallets
    from_wallet = db.query(Wallet).filter(Wallet.id == transaction.from_wallet).first()
    to_wallet = db.query(Wallet).filter(Wallet.id == transaction.to_wallet).first()
    
    if not from_wallet or not to_wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or both wallets not found"
        )
    
    # Update the transaction status
    transaction.status = TransactionStatus.COMPLETED
    transaction.completed_at = datetime.utcnow()
    db.commit()
    
    # The actual balance updates are handled by database triggers
    # See the init-db SQL file for details
    
    return {"message": "Transaction confirmed successfully"}

@router.get("/", response_model=List[TransactionSchema])
async def list_transactions(
    wallet_id: Optional[uuid.UUID] = Query(None, description="Filter by wallet ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
    type: Optional[str] = Query(None, description="Filter by type"),
    currency: Optional[str] = Query(None, description="Filter by currency"),
    start_date: Optional[datetime] = Query(None, description="Filter by start date"),
    end_date: Optional[datetime] = Query(None, description="Filter by end date"),
    skip: int = Query(0, ge=0, description="Skip records"),
    limit: int = Query(100, ge=1, le=1000, description="Limit records"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List transactions for the current user."""
    # Build the query
    query = db.query(Transaction)
    
    # Apply filters
    if wallet_id:
        # Check if the user has access to this wallet
        wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
        
        if not wallet:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Wallet not found"
            )
        
        if wallet.owner_type == WalletOwnerType.USER:
            if wallet.owner_id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have access to this wallet"
                )
        elif wallet.owner_type == WalletOwnerType.AGENT:
            # Check if the wallet belongs to an agent owned by the user
            agent = db.query(Agent).filter(
                Agent.id == wallet.owner_id,
                Agent.user_id == current_user.id
            ).first()
            
            if not agent:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have access to this wallet"
                )
        
        # Filter transactions for this wallet
        query = query.filter(
            (Transaction.from_wallet == wallet_id) | (Transaction.to_wallet == wallet_id)
        )
    else:
        # Get all wallets owned by the user or their agents
        user_wallets = db.query(Wallet.id).filter(
            Wallet.owner_type == WalletOwnerType.USER,
            Wallet.owner_id == current_user.id
        )
        
        # Get all agents owned by the user
        user_agents = db.query(Agent.id).filter(Agent.user_id == current_user.id)
        
        # Get all wallets owned by these agents
        agent_wallets = db.query(Wallet.id).filter(
            Wallet.owner_type == WalletOwnerType.AGENT,
            Wallet.owner_id.in_(user_agents)
        )
        
        # Combine the wallet IDs
        wallet_ids = user_wallets.union(agent_wallets).subquery()
        
        # Filter transactions for these wallets
        query = query.filter(
            (Transaction.from_wallet.in_(wallet_ids)) | (Transaction.to_wallet.in_(wallet_ids))
        )
    
    if status:
        query = query.filter(Transaction.status == status)
    
    if type:
        query = query.filter(Transaction.type == type)
    
    if currency:
        query = query.filter(Transaction.currency == currency)
    
    if start_date:
        query = query.filter(Transaction.created_at >= start_date)
    
    if end_date:
        query = query.filter(Transaction.created_at <= end_date)
    
    # Execute query with pagination
    transactions = query.order_by(Transaction.created_at.desc()).offset(skip).limit(limit).all()
    
    return transactions