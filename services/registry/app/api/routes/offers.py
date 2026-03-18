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
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...auth import get_current_agent
from ...database import get_db
from ...models import Agent, NegotiationRound, Offer, OfferStatus
from ...schemas import (
    CounterOfferCreate,
    NegotiationRoundResponse,
    OfferWithNegotiation,
)

router = APIRouter()

MAX_NEGOTIATION_ROUNDS = 5


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
    current_agent: Agent = Depends(get_current_agent),
):
    """
    Submit a counter-offer for an existing offer.

    Only the recipient (to_agent) or sender (from_agent) can counter.
    Max 5 rounds. No escrow locked during negotiation.
    """
    offer = db.query(Offer).filter(Offer.id == offer_id).first()
    if not offer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

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
    current_agent: Agent = Depends(get_current_agent),
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
    current_agent: Agent = Depends(get_current_agent),
):
    """Reject the offer. Either party can reject at any time."""
    offer = db.query(Offer).filter(Offer.id == offer_id).first()
    if not offer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

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
