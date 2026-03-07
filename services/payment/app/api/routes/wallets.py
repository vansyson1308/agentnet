import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_agent, get_current_user
from ...database import get_db
from ...models import Agent, User, Wallet, WalletOwnerType
from ...schemas import WalletBalance, WalletUpdate

router = APIRouter()


@router.get("/", response_model=List[dict])
async def list_wallets(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """List all wallets accessible to the current user."""
    # Get user's wallet
    user_wallet = (
        db.query(Wallet)
        .filter(
            Wallet.owner_type == WalletOwnerType.USER,
            Wallet.owner_id == current_user.id,
        )
        .first()
    )

    # Get agent wallets
    agent_wallets = (
        db.query(Wallet)
        .join(Agent, Wallet.owner_id == Agent.id)
        .filter(Agent.user_id == current_user.id, Wallet.owner_type == WalletOwnerType.AGENT)
        .all()
    )

    result = []
    if user_wallet:
        result.append(
            {
                "id": str(user_wallet.id),
                "owner_type": "user",
                "owner_id": str(user_wallet.owner_id),
                "balance_credits": user_wallet.balance_credits,
                "balance_usdc": float(user_wallet.balance_usdc),
                "reserved_credits": user_wallet.reserved_credits,
                "reserved_usdc": float(user_wallet.reserved_usdc),
                "spending_cap": user_wallet.spending_cap,
                "daily_spent": user_wallet.daily_spent,
            }
        )

    for wallet in agent_wallets:
        result.append(
            {
                "id": str(wallet.id),
                "owner_type": "agent",
                "owner_id": str(wallet.owner_id),
                "balance_credits": wallet.balance_credits,
                "balance_usdc": float(wallet.balance_usdc),
                "reserved_credits": wallet.reserved_credits,
                "reserved_usdc": float(wallet.reserved_usdc),
                "spending_cap": wallet.spending_cap,
                "daily_spent": wallet.daily_spent,
            }
        )

    return result


@router.get("/{wallet_id}/balance", response_model=WalletBalance)
async def get_wallet_balance(
    wallet_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get wallet balance."""
    # Get the wallet
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()

    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")

    # Check if the user has access to this wallet
    if wallet.owner_type == WalletOwnerType.USER:
        if wallet.owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this wallet",
            )
    elif wallet.owner_type == WalletOwnerType.AGENT:
        # Check if the wallet belongs to an agent owned by the user
        agent = db.query(Agent).filter(Agent.id == wallet.owner_id, Agent.user_id == current_user.id).first()

        if not agent:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this wallet",
            )

    return WalletBalance(
        balance_credits=wallet.balance_credits,
        balance_usdc=float(wallet.balance_usdc),
        reserved_credits=wallet.reserved_credits,
        reserved_usdc=float(wallet.reserved_usdc),
        spending_cap=wallet.spending_cap,
        daily_spent=wallet.daily_spent,
    )


@router.put("/{wallet_id}")
async def update_wallet(
    wallet_id: uuid.UUID,
    wallet_update: WalletUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update wallet settings."""
    # Get the wallet
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()

    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")

    # Check if the user has access to this wallet
    if wallet.owner_type == WalletOwnerType.USER:
        if wallet.owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this wallet",
            )
    elif wallet.owner_type == WalletOwnerType.AGENT:
        # Check if the wallet belongs to an agent owned by the user
        agent = db.query(Agent).filter(Agent.id == wallet.owner_id, Agent.user_id == current_user.id).first()

        if not agent:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this wallet",
            )

    # Update wallet settings
    update_data = wallet_update.model_dump(exclude_unset=True)

    for key, value in update_data.items():
        setattr(wallet, key, value)

    db.commit()
    db.refresh(wallet)


# ─────────────────────────────────────────────────────────
# Dev-Only Funding Endpoint
# ─────────────────────────────────────────────────────────


class FundRequest:
    """Fund request model for dev-only funding."""

    def __init__(self, amount: int, currency: str = "credits"):
        self.amount = amount
        self.currency = currency


@router.post("/{wallet_id}/fund")
async def fund_wallet_dev(
    wallet_id: uuid.UUID,
    fund_request: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Add funds to a wallet (DEV ONLY - requires ENVIRONMENT=development).

    This endpoint is for development/testing only and should not be
    available in production.
    """
    import os

    # Only allow in development mode
    env = os.getenv("ENVIRONMENT", "").lower()
    if env != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Wallet funding only allowed in development mode",
        )

    amount = fund_request.get("amount")
    currency = fund_request.get("currency", "credits")

    if not amount or amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount must be positive")

    if currency not in ("credits", "usdc"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Currency must be credits or usdc",
        )

    # Get wallet
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()

    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")

    # Add funds based on currency
    if currency == "credits":
        wallet.balance_credits += amount
    else:
        wallet.balance_usdc += amount

    db.commit()
    db.refresh(wallet)

    return {
        "wallet_id": str(wallet.id),
        "amount_added": amount,
        "currency": currency,
        "new_balance_credits": wallet.balance_credits,
        "new_balance_usdc": float(wallet.balance_usdc),
    }
