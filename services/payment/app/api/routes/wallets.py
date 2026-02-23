from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid

from ...database import get_db
from ...models import Wallet, User, Agent, WalletOwnerType
from ...schemas import WalletBalance, WalletUpdate
from ...auth import get_current_user, get_current_agent

router = APIRouter()

@router.get("/{wallet_id}/balance", response_model=WalletBalance)
async def get_wallet_balance(
    wallet_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get wallet balance."""
    # Get the wallet
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    
    if not wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Wallet not found"
        )
    
    # Check if the user has access to this wallet
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
    
    return WalletBalance(
        balance_credits=wallet.balance_credits,
        balance_usdc=float(wallet.balance_usdc),
        reserved_credits=wallet.reserved_credits,
        reserved_usdc=float(wallet.reserved_usdc),
        spending_cap=wallet.spending_cap,
        daily_spent=wallet.daily_spent
    )

@router.put("/{wallet_id}")
async def update_wallet(
    wallet_id: uuid.UUID,
    wallet_update: WalletUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update wallet settings."""
    # Get the wallet
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    
    if not wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Wallet not found"
        )
    
    # Check if the user has access to this wallet
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
    
    # Update wallet settings
    update_data = wallet_update.model_dump(exclude_unset=True)
    
    for key, value in update_data.items():
        setattr(wallet, key, value)
    
    db.commit()
    db.refresh(wallet)
    
    return {"message": "Wallet updated successfully"}