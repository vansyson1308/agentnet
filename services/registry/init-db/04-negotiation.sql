-- ============================================================
-- Negotiation Protocol Migration
-- ============================================================
-- Adds multi-round negotiation support for offers.
--
-- Invariant: No escrow is locked during negotiation.
-- Escrow only locks when an offer is accepted and a task session is created.
-- ============================================================

-- Create negotiation_rounds table
CREATE TABLE IF NOT EXISTS negotiation_rounds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  offer_id UUID NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
  round_number INT NOT NULL,
  proposed_by_agent_id UUID NOT NULL REFERENCES agents(id),
  proposed_price BIGINT NOT NULL,
  proposed_terms TEXT,
  status offer_status DEFAULT 'pending',
  created_at TIMESTAMP DEFAULT NOW()
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_negotiation_rounds_offer_id ON negotiation_rounds(offer_id);
CREATE INDEX IF NOT EXISTS idx_negotiation_rounds_proposed_by ON negotiation_rounds(proposed_by_agent_id);
