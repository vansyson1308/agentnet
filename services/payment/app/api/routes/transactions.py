import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_agent, get_current_user, get_current_user_or_agent
from ...database import get_db
from ...models import (
    Agent,
    CurrencyType,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    Wallet,
    WalletOwnerType,
)
from ...schemas import Transaction as TransactionSchema
from ...schemas import TransactionCreate, TransactionUpdate

router = APIRouter()


def wallet_owned_by(db: Session, wallet: Wallet, principal) -> bool:
    """True when ``principal`` (User or Agent) owns ``wallet``."""
    if wallet.owner_type == WalletOwnerType.USER:
        return isinstance(principal, User) and wallet.owner_id == principal.id
    if wallet.owner_type == WalletOwnerType.AGENT:
        if isinstance(principal, Agent):
            return wallet.owner_id == principal.id
        if isinstance(principal, User):
            return (
                db.query(Agent.id).filter(Agent.id == wallet.owner_id, Agent.user_id == principal.id).first() is not None
            )
    return False


def _lock_wallets(db: Session, *wallet_ids) -> Dict[uuid.UUID, Wallet]:
    """``SELECT ... FOR UPDATE`` the wallets in id order (one global order, so two
    opposite-direction transfers never deadlock) and return fresh rows by id."""
    rows = (
        db.query(Wallet)
        .filter(Wallet.id.in_(set(wallet_ids)))
        .order_by(Wallet.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    return {w.id: w for w in rows}


def _check_can_spend(wallet: Wallet, amount: int, currency: str) -> None:
    """Same rule as ``task_service.create_task_with_escrow``: only the balance NET
    of escrow is spendable, and spent-today + reserved + amount must fit the cap.
    The caller holds the wallet's row lock."""
    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Transaction amount must be positive")
    if currency == "credits":
        available = wallet.balance_credits - wallet.reserved_credits
        if available < amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient available credits: required {amount}, available {available}",
            )
        committed = wallet.daily_spent + wallet.reserved_credits
        if committed + amount > wallet.spending_cap:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Transaction would exceed daily spending cap of {wallet.spending_cap}",
            )
    else:
        available = float(wallet.balance_usdc) - float(wallet.reserved_usdc)
        if available < amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient available USDC: required {amount}, available {available}",
            )


@router.post("/create", response_model=Dict[str, Any])
async def create_transaction(
    transaction: TransactionCreate,
    db: Session = Depends(get_db),
    current_user_or_agent=Depends(get_current_user_or_agent),
):
    """Initiate a direct payment (PENDING until the payer confirms it).

    Money invariant: a direct payment moves ``amount > 0`` from the payer's
    AVAILABLE balance (balance minus escrow). The check here is advisory (a
    pending row reserves nothing); ``confirm_transaction`` re-checks it under
    the payer's row lock before any money moves.
    """
    # Get the source wallet
    from_wallet = db.query(Wallet).filter(Wallet.id == transaction.from_wallet).first()

    if not from_wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source wallet not found")

    # The caller must OWN the source wallet: a user owns their own wallet and
    # the wallets of their agents; an agent owns only its own wallet. Every
    # other combination is refused (previously the user-vs-agent-wallet and
    # agent-vs-user-wallet cases skipped the check entirely).
    if not wallet_owned_by(db, from_wallet, current_user_or_agent):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to the source wallet",
        )

    # Get the destination wallet
    to_wallet = db.query(Wallet).filter(Wallet.id == transaction.to_wallet).first()

    if not to_wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Destination wallet not found")

    # Check if the source and destination wallets are the same
    if from_wallet.id == to_wallet.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source and destination wallets cannot be the same",
        )

    _check_can_spend(from_wallet, transaction.amount, transaction.currency)

    # Create the transaction
    db_transaction = Transaction(
        id=uuid.uuid4(),
        from_wallet=from_wallet.id,
        to_wallet=to_wallet.id,
        amount=transaction.amount,
        currency=CurrencyType(transaction.currency),
        status=TransactionStatus.PENDING,
        type=TransactionType.PAYMENT,
        metadata=transaction.metadata or {},
    )

    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)

    return {"transaction_id": str(db_transaction.id)}


@router.post("/{transaction_id}/confirm")
async def confirm_transaction(
    transaction_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Confirm (settle) a pending direct payment.

    Locks the transaction row, then both wallets in id order, and re-checks the
    payer's available balance and cap before the status flips; the balance
    trigger then moves the money inside the same locks. Concurrent confirms of
    one payer's transfers therefore serialize and never spend the same credits
    twice; a second confirm of the same row finds it no longer PENDING.
    """
    transaction = (
        db.query(Transaction)
        .filter(Transaction.id == transaction_id)
        .with_for_update()
        .populate_existing()
        .first()
    )

    if not transaction:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

    # Check if the transaction is in pending status
    if transaction.status != TransactionStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Transaction is in {transaction.status} status, cannot confirm",
        )

    # Task escrow payments settle only through the task lifecycle
    # (task_service.confirm_task_completion), never through this route.
    if transaction.task_session_id is not None or transaction.to_wallet is None or transaction.from_wallet is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task escrow payments settle through the task lifecycle",
        )

    wallets = _lock_wallets(db, transaction.from_wallet, transaction.to_wallet)
    from_wallet = wallets.get(transaction.from_wallet)
    to_wallet = wallets.get(transaction.to_wallet)

    if not from_wallet or not to_wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or both wallets not found",
        )

    # Only the owner of the paying wallet may settle it.
    if not wallet_owned_by(db, from_wallet, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to the source wallet",
        )

    currency = transaction.currency.value if hasattr(transaction.currency, "value") else str(transaction.currency)
    _check_can_spend(from_wallet, transaction.amount, currency)

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
    current_user: User = Depends(get_current_user),
):
    """List transactions for the current user."""
    # Build the query
    query = db.query(Transaction)

    # Apply filters
    if wallet_id:
        # Check if the user has access to this wallet
        wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()

        if not wallet:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")

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

        # Filter transactions for this wallet
        query = query.filter((Transaction.from_wallet == wallet_id) | (Transaction.to_wallet == wallet_id))
    else:
        # Get all wallets owned by the user or their agents
        user_wallets = db.query(Wallet.id).filter(
            Wallet.owner_type == WalletOwnerType.USER,
            Wallet.owner_id == current_user.id,
        )

        # Get all agents owned by the user
        user_agents = db.query(Agent.id).filter(Agent.user_id == current_user.id)

        # Get all wallets owned by these agents
        agent_wallets = db.query(Wallet.id).filter(
            Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id.in_(user_agents)
        )

        # Combine the wallet IDs
        wallet_ids = user_wallets.union(agent_wallets).subquery()

        # Filter transactions for these wallets
        query = query.filter((Transaction.from_wallet.in_(wallet_ids)) | (Transaction.to_wallet.in_(wallet_ids)))

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
