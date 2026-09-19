"""Deterministic identifiers shared by the executor and the models.

A code candidate's id is derived from (correlation, proposal, title) so an
Architect can, in the SAME run, both request the candidate and escrow a
Builder task whose input references it — and so a retried request maps to
the same row instead of a duplicate."""

from __future__ import annotations

import uuid
from typing import Optional

CANDIDATE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agentnet://society/code-candidate")

# The idempotency key a canary gives the ONE event it injects. It marks the
# whole correlation as a rehearsal, which is how the runtime knows not to let
# it leave permanent state behind (see events.is_rehearsal_correlation). It
# lives here because the HTTP canary must stay importable without the ORM.
CANARY_IDEMPOTENCY_PREFIX = "canary-"


def candidate_id_for(correlation_id: uuid.UUID | str, proposal_id: Optional[uuid.UUID | str], title: str) -> uuid.UUID:
    return uuid.uuid5(CANDIDATE_NAMESPACE, f"{correlation_id}:{proposal_id or ''}:{(title or '').strip().lower()}")
