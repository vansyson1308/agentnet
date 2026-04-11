"""
Negotiation Protocol — multi-round price negotiation between agents.

Flow:
  1. Agent A creates offer (existing)
  2. Agent B can: accept, reject, OR counter-offer (new)
  3. Counter-offer creates a NegotiationRound linked to original offer
  4. Max 5 rounds, then auto-reject

Invariant: No escrow is locked during negotiation.
Escrow only locks when an offer is accepted and a task session is created.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_user_or_agent
from ...database import get_db
from ...models import Agent, NegotiationRound, Offer, OfferStatus
from ...governance import create_notification
from ...schemas import (
    CounterOfferCreate,
    NegotiationRoundResponse,
    OfferWithNegotiation,
    OfferCreate,
)
from .tasks import _resolve_agent

router = APIRouter()

MAX_NEGOTIATION_ROUNDS = 5

def _resolve_offer_agent(current_user_or_agent, offer, db: Session, caller_agent_id=None):
    from ...models import User, Agent
    if isinstance(current_user_or_agent, Agent):
        return current_user_or_agent
    if caller_agent_id:
        return db.query(Agent).filter(Agent.id == caller_agent_id, Agent.user_id == current_user_or_agent.id).first()
    
    owned_agents = [a.id for a in db.query(Agent).filter(Agent.user_id == current_user_or_agent.id).all()]
    if offer and offer.from_agent_id in owned_agents:
        return db.query(Agent).filter(Agent.id == offer.from_agent_id).first()
    if offer and offer.to_agent_id in owned_agents:
        return db.query(Agent).filter(Agent.id == offer.to_agent_id).first()
    
    return db.query(Agent).filter(Agent.user_id == current_user_or_agent.id).first()

def _get_owned_agent_ids(current_user_or_agent, db: Session):
    from ...models import User, Agent
    if isinstance(current_user_or_agent, Agent):
        return [current_user_or_agent.id]
    return [a.id for a in db.query(Agent).filter(Agent.user_id == current_user_or_agent.id).all()]

@router.get("/", response_model=List[OfferWithNegotiation])
async def list_offers(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent),
):
    """List all offers where current agent is sender or recipient."""
    my_ids = _get_owned_agent_ids(current_user_or_agent, db)
    return db.query(Offer).filter(
        Offer.from_agent_id.in_(my_ids) | Offer.to_agent_id.in_(my_ids)
    ).order_by(Offer.created_at.desc()).offset(skip).limit(limit).all()

@router.post("/", response_model=OfferWithNegotiation, status_code=status.HTTP_201_CREATED)
async def create_offer(
    offer: OfferCreate,
    background_tasks: BackgroundTasks,
    caller_agent_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent),
):
    """Create a new structured offer to another agent."""
    current_agent = _resolve_offer_agent(current_user_or_agent, None, db, caller_agent_id)
    if offer.to_agent_id == current_agent.id:
        raise HTTPException(status_code=400, detail="Cannot send offer to yourself")
    
    from ...models import CurrencyType
    new_offer = Offer(
        id=uuid.uuid4(),
        from_agent_id=current_agent.id,
        to_agent_id=offer.to_agent_id,
        core_task_id=offer.core_task_id,
        title=offer.title,
        description=offer.description,
        price=offer.price,
        currency=CurrencyType[offer.currency.upper()],
        expires_at=offer.expires_at,
        status=OfferStatus.PENDING
    )
    db.add(new_offer)
    db.commit()
    db.refresh(new_offer)

    # Notify recipient owner
    recipient_agent = db.query(Agent).filter(Agent.id == offer.to_agent_id).first()
    if recipient_agent and recipient_agent.user_id:
        background_tasks.add_task(
            create_notification,
            db,
            recipient_agent.user_id,
            "offer",
            "New Offer Received",
            f"Agent {current_agent.name} sent a '{offer.title}' offer for {offer.price} {offer.currency}.",
            f"/offers/{new_offer.id}"
        )

    return new_offer


@router.get("/{offer_id}", response_model=OfferWithNegotiation)
async def get_offer_with_negotiation(
    offer_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Get offer with full negotiation history."""
    offer = db.query(Offer).filter(Offer.id == offer_id).first()
    if not offer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

    return offer


@router.post("/{offer_id}/counter", response_model=NegotiationRoundResponse)
async def counter_offer(
    offer_id: uuid.UUID,
    counter: CounterOfferCreate,
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent),
):
    """
    Submit a counter-offer for an existing offer.

    Only the recipient (to_agent) or sender (from_agent) can counter.
    Max 5 rounds. No escrow locked during negotiation.
    """
    offer = db.query(Offer).filter(Offer.id == offer_id).first()
    if not offer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
        
    current_agent = _resolve_offer_agent(current_user_or_agent, offer, db)

    # Only pending offers can be negotiated
    if offer.status != OfferStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot negotiate: offer status is '{offer.status.value}'",
        )

    # Only sender or recipient can counter
    if current_agent.id not in (offer.from_agent_id, offer.to_agent_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the offer sender or recipient can submit counter-offers",
        )

    # Check round limit
    existing_rounds = db.query(NegotiationRound).filter(NegotiationRound.offer_id == offer_id).count()

    if existing_rounds >= MAX_NEGOTIATION_ROUNDS:
        # Auto-reject after max rounds
        offer.status = OfferStatus.REJECTED
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum negotiation rounds ({MAX_NEGOTIATION_ROUNDS}) reached. Offer auto-rejected.",
        )

    # Cannot counter your own last proposal
    last_round = (
        db.query(NegotiationRound)
        .filter(NegotiationRound.offer_id == offer_id)
        .order_by(NegotiationRound.round_number.desc())
        .first()
    )

    if last_round and last_round.proposed_by_agent_id == current_agent.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot counter your own proposal. Wait for the other party.",
        )

    # If no rounds yet, check that the original recipient is countering
    if not last_round and current_agent.id == offer.from_agent_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The sender cannot counter their own initial offer. Wait for the recipient.",
        )

    # Create the counter-offer round
    new_round = NegotiationRound(
        id=uuid.uuid4(),
        offer_id=offer_id,
        round_number=existing_rounds + 1,
        proposed_by_agent_id=current_agent.id,
        proposed_price=counter.proposed_price,
        proposed_terms=counter.proposed_terms,
        status=OfferStatus.PENDING,
    )

    # Update the offer's price to the latest proposal
    offer.price = counter.proposed_price

    db.add(new_round)
    db.commit()
    db.refresh(new_round)

    return new_round


@router.post("/{offer_id}/accept")
async def accept_offer(
    offer_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent),
):
    """
    Accept the current offer/counter-offer.

    Only the party who did NOT make the last proposal can accept.
    Acceptance finalizes the price. Escrow locking happens when
    a task session is created from this offer.
    """
    offer = db.query(Offer).filter(Offer.id == offer_id).first()
    if not offer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
        
    current_agent = _resolve_offer_agent(current_user_or_agent, offer, db)

    if offer.status != OfferStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot accept: offer status is '{offer.status.value}'",
        )

    # Only sender or recipient can accept
    if current_agent.id not in (offer.from_agent_id, offer.to_agent_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the offer sender or recipient can accept",
        )

    # Check who made the last proposal
    last_round = (
        db.query(NegotiationRound)
        .filter(NegotiationRound.offer_id == offer_id)
        .order_by(NegotiationRound.round_number.desc())
        .first()
    )

    if last_round:
        # Cannot accept your own proposal
        if last_round.proposed_by_agent_id == current_agent.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot accept your own proposal",
            )
        # Accept the last round
        last_round.status = OfferStatus.ACCEPTED
    else:
        # No negotiation rounds — accepting the original offer
        # Only recipient can accept original offer
        if current_agent.id != offer.to_agent_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only the recipient can accept the original offer",
            )

    offer.status = OfferStatus.ACCEPTED
    db.commit()

    return {
        "message": "Offer accepted",
        "offer_id": str(offer.id),
        "final_price": offer.price,
        "currency": offer.currency.value if hasattr(offer.currency, "value") else offer.currency,
    }


@router.post("/{offer_id}/reject")
async def reject_offer(
    offer_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent),
):
    """Reject the offer. Either party can reject at any time."""
    offer = db.query(Offer).filter(Offer.id == offer_id).first()
    if not offer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
        
    current_agent = _resolve_offer_agent(current_user_or_agent, offer, db)

    if offer.status != OfferStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot reject: offer status is '{offer.status.value}'",
        )

    if current_agent.id not in (offer.from_agent_id, offer.to_agent_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the offer sender or recipient can reject",
        )

    offer.status = OfferStatus.REJECTED

    # Reject all pending rounds
    db.query(NegotiationRound).filter(
        NegotiationRound.offer_id == offer_id,
        NegotiationRound.status == OfferStatus.PENDING,
    ).update({"status": OfferStatus.REJECTED})

    db.commit()

    return {"message": "Offer rejected", "offer_id": str(offer.id)}
