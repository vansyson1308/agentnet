-- ============================================================
-- Simulation Service Schema
-- ============================================================
-- Tables for MiroFish swarm simulation integration.
-- All tables use `sim_` prefix.
--
-- Money invariant: These tables NEVER modify wallet balances.
-- Escrow is linked via sim_sessions.task_session_id -> task_sessions.id.
-- Only the payment service modifies wallet state.
-- ============================================================

-- Simulation status enum
DO $$ BEGIN
  CREATE TYPE sim_status AS ENUM (
    'initializing',
    'building_graph',
    'generating_agents',
    'running',
    'generating_report',
    'completed',
    'failed',
    'cancelled',
    'timeout'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Simulation sessions
CREATE TABLE IF NOT EXISTS sim_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  task_session_id UUID REFERENCES task_sessions(id),
  name VARCHAR(255) NOT NULL,
  description TEXT,
  status sim_status NOT NULL DEFAULT 'initializing',
  seed_config JSONB NOT NULL,
  simulation_config JSONB NOT NULL,
  platform VARCHAR(50) NOT NULL DEFAULT 'twitter',
  num_steps INT NOT NULL DEFAULT 100,
  num_simulated_agents INT DEFAULT 0,
  cost_credits INT NOT NULL DEFAULT 0,
  progress_pct INT DEFAULT 0,
  error_message TEXT,
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sim_sessions_user ON sim_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sim_sessions_status ON sim_sessions(status);
CREATE INDEX IF NOT EXISTS idx_sim_sessions_created ON sim_sessions(created_at DESC);

-- Simulated agent profiles
CREATE TABLE IF NOT EXISTS sim_agent_profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sim_session_id UUID NOT NULL REFERENCES sim_sessions(id) ON DELETE CASCADE,
  source_agent_id UUID,  -- references agents(id), NULL if injected
  persona_name VARCHAR(255) NOT NULL,
  persona_data JSONB NOT NULL,
  is_injected BOOLEAN NOT NULL DEFAULT FALSE,
  agent_index INT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sim_profiles_session ON sim_agent_profiles(sim_session_id);

-- Simulation results (step-by-step actions)
CREATE TABLE IF NOT EXISTS sim_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sim_session_id UUID NOT NULL REFERENCES sim_sessions(id) ON DELETE CASCADE,
  step_number INT NOT NULL,
  agent_index INT NOT NULL,
  action_type VARCHAR(100),
  content TEXT,
  metadata JSONB,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sim_results_session ON sim_results(sim_session_id);
CREATE INDEX IF NOT EXISTS idx_sim_results_step ON sim_results(sim_session_id, step_number);

-- Prediction reports
CREATE TABLE IF NOT EXISTS sim_reports (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sim_session_id UUID NOT NULL REFERENCES sim_sessions(id) ON DELETE CASCADE,
  report_type VARCHAR(50) NOT NULL DEFAULT 'prediction',
  title VARCHAR(500),
  content TEXT NOT NULL,
  summary TEXT,
  key_findings JSONB,
  confidence_score FLOAT,
  metadata JSONB,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sim_reports_session ON sim_reports(sim_session_id);

-- Post-simulation chat messages
CREATE TABLE IF NOT EXISTS sim_chat_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sim_session_id UUID NOT NULL REFERENCES sim_sessions(id) ON DELETE CASCADE,
  agent_index INT NOT NULL,
  role VARCHAR(20) NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sim_chat_session ON sim_chat_messages(sim_session_id, agent_index);
