-- Enable extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Create enum types
CREATE TYPE kyc_status AS ENUM ('pending', 'verified', 'rejected');
CREATE TYPE agent_status AS ENUM ('active', 'inactive', 'unverified', 'banned', 'suspended');
CREATE TYPE wallet_owner_type AS ENUM ('user', 'agent');
CREATE TYPE task_status AS ENUM ('initiated', 'in_progress', 'completed', 'failed', 'timeout', 'refunded');
CREATE TYPE span_status AS ENUM ('success', 'failed', 'timeout');
CREATE TYPE transaction_status AS ENUM ('pending', 'completed', 'failed', 'cancelled');
CREATE TYPE transaction_type AS ENUM ('payment', 'referral_reward', 'withdraw', 'deposit', 'refund');
CREATE TYPE referral_status AS ENUM ('pending', 'completed', 'rejected');
CREATE TYPE offer_status AS ENUM ('pending', 'accepted', 'rejected', 'expired');
CREATE TYPE currency_type AS ENUM ('credits', 'usdc');

-- Create users table
CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email VARCHAR UNIQUE NOT NULL,
  phone VARCHAR,
  password_hash VARCHAR NOT NULL,
  kyc_status kyc_status DEFAULT 'pending',
  telegram_id VARCHAR,
  notification_settings JSONB DEFAULT '{}',
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Create agents table
CREATE TABLE IF NOT EXISTS agents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  name VARCHAR NOT NULL,
  description TEXT,
  capabilities JSONB NOT NULL DEFAULT '[]',
  endpoint VARCHAR NOT NULL,
  public_key TEXT NOT NULL,
  status agent_status DEFAULT 'unverified',
  verify_score INT DEFAULT 0,
  timeout_count INT DEFAULT 0,
  offer_rate_7d FLOAT DEFAULT 0,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Create wallets table
CREATE TABLE IF NOT EXISTS wallets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_type wallet_owner_type NOT NULL,
  owner_id UUID NOT NULL,
  balance_credits BIGINT NOT NULL DEFAULT 0,
  balance_usdc DECIMAL(20,6) NOT NULL DEFAULT 0,
  reserved_credits BIGINT NOT NULL DEFAULT 0,
  reserved_usdc DECIMAL(20,6) NOT NULL DEFAULT 0,
  spending_cap BIGINT NOT NULL DEFAULT 1000,
  daily_spent BIGINT NOT NULL DEFAULT 0,
  daily_reset_at TIMESTAMP DEFAULT NOW(),
  allowance_parent_id UUID REFERENCES wallets(id),
  auto_approve_threshold BIGINT DEFAULT 10,
  whitelist JSONB DEFAULT '[]',
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Create task_sessions table
CREATE TABLE IF NOT EXISTS task_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trace_id UUID NOT NULL,
  span_id UUID NOT NULL,
  parent_span_id UUID,
  caller_agent_id UUID REFERENCES agents(id),
  callee_agent_id UUID REFERENCES agents(id),
  capability VARCHAR NOT NULL,
  input_hash VARCHAR,
  escrow_amount BIGINT NOT NULL,
  currency currency_type NOT NULL DEFAULT 'credits',
  status task_status DEFAULT 'initiated',
  timeout_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  completed_at TIMESTAMP,
  refund_at TIMESTAMP,
  error_message TEXT,
  output JSONB
);

-- Create spans table
CREATE TABLE IF NOT EXISTS spans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trace_id UUID NOT NULL,
  span_id UUID NOT NULL,
  parent_span_id UUID,
  agent_id UUID REFERENCES agents(id),
  event VARCHAR NOT NULL,
  capability VARCHAR,
  duration_ms INT,
  status span_status,
  credits_used BIGINT,
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMP DEFAULT NOW()
);

-- Create transactions table
CREATE TABLE IF NOT EXISTS transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_wallet UUID REFERENCES wallets(id),
  to_wallet UUID REFERENCES wallets(id),
  amount BIGINT NOT NULL,
  currency currency_type NOT NULL DEFAULT 'credits',
  status transaction_status DEFAULT 'pending',
  type transaction_type NOT NULL,
  task_session_id UUID REFERENCES task_sessions(id),
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMP DEFAULT NOW(),
  completed_at TIMESTAMP
);

-- Create referrals table
CREATE TABLE IF NOT EXISTS referrals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inviter_agent_id UUID REFERENCES agents(id),
  invitee_agent_id UUID REFERENCES agents(id),
  status referral_status DEFAULT 'pending',
  reward_amount BIGINT,
  device_fingerprint VARCHAR,
  created_at TIMESTAMP DEFAULT NOW(),
  completed_at TIMESTAMP
);

-- Create offers table
CREATE TABLE IF NOT EXISTS offers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_agent_id UUID REFERENCES agents(id),
  to_agent_id UUID REFERENCES agents(id),
  core_task_id UUID REFERENCES task_sessions(id),
  title VARCHAR NOT NULL,
  description TEXT,
  price BIGINT NOT NULL,
  currency currency_type NOT NULL DEFAULT 'credits',
  expires_at TIMESTAMP NOT NULL,
  status offer_status DEFAULT 'pending',
  baseline_quality_score FLOAT,
  blocked BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Create daily_spending table for enforcing spending caps
CREATE TABLE IF NOT EXISTS daily_spending (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  wallet_id UUID REFERENCES wallets(id) ON DELETE CASCADE,
  date DATE NOT NULL DEFAULT CURRENT_DATE,
  amount BIGINT NOT NULL DEFAULT 0,
  last_updated TIMESTAMP DEFAULT NOW(),
  UNIQUE(wallet_id, date)
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_agents_user_id ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_capabilities ON agents USING GIN (capabilities);
CREATE INDEX IF NOT EXISTS idx_wallets_owner ON wallets(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_task_sessions_trace_id ON task_sessions(trace_id);
CREATE INDEX IF NOT EXISTS idx_task_sessions_status ON task_sessions(status);
CREATE INDEX IF NOT EXISTS idx_task_sessions_timeout_at ON task_sessions(timeout_at);
CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_transactions_from_wallet ON transactions(from_wallet);
CREATE INDEX IF NOT EXISTS idx_transactions_to_wallet ON transactions(to_wallet);
CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_referrals_inviter_agent_id ON referrals(inviter_agent_id);
CREATE INDEX IF NOT EXISTS idx_referrals_invitee_agent_id ON referrals(invitee_agent_id);
CREATE INDEX IF NOT EXISTS idx_offers_from_agent_id ON offers(from_agent_id);
CREATE INDEX IF NOT EXISTS idx_offers_to_agent_id ON offers(to_agent_id);
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
CREATE INDEX IF NOT EXISTS idx_offers_expires_at ON offers(expires_at);
CREATE INDEX IF NOT EXISTS idx_daily_spending_wallet_id ON daily_spending(wallet_id);

-- Create trigger functions
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Create triggers
CREATE TRIGGER update_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_agents_updated_at
    BEFORE UPDATE ON agents
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_wallets_updated_at
    BEFORE UPDATE ON wallets
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Create trigger for spending cap enforcement
CREATE OR REPLACE FUNCTION check_spending_cap()
RETURNS TRIGGER AS $$
DECLARE
    current_daily_spent BIGINT;
    wallet_spending_cap BIGINT;
    today DATE := CURRENT_DATE;
BEGIN
    -- Only check for outgoing transactions
    IF NEW.from_wallet IS NOT NULL THEN
        -- Get wallet spending cap
        SELECT spending_cap INTO wallet_spending_cap
        FROM wallets
        WHERE id = NEW.from_wallet;
        
        -- Get current daily spending
        SELECT COALESCE(amount, 0) INTO current_daily_spent
        FROM daily_spending
        WHERE wallet_id = NEW.from_wallet AND date = today;
        
        -- Check if adding this transaction would exceed the spending cap
        IF current_daily_spent + NEW.amount > wallet_spending_cap THEN
            RAISE EXCEPTION 'Spending cap exceeded for wallet %', NEW.from_wallet;
        END IF;
    END IF;
    
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER check_transaction_spending_cap
    BEFORE INSERT ON transactions
    FOR EACH ROW
    EXECUTE FUNCTION check_spending_cap();

-- Create trigger for updating daily spending
CREATE OR REPLACE FUNCTION update_daily_spending()
RETURNS TRIGGER AS $$
DECLARE
    today DATE := CURRENT_DATE;
BEGIN
    -- Only update for completed outgoing transactions
    IF NEW.status = 'completed' AND NEW.from_wallet IS NOT NULL THEN
        -- Upsert daily spending record
        INSERT INTO daily_spending (wallet_id, date, amount, last_updated)
        VALUES (NEW.from_wallet, today, NEW.amount, NOW())
        ON CONFLICT (wallet_id, date)
        DO UPDATE SET 
            amount = daily_spending.amount + NEW.amount,
            last_updated = NOW();
        
        -- Also update wallet's daily_spent
        UPDATE wallets
        SET daily_spent = daily_spent + NEW.amount,
            updated_at = NOW()
        WHERE id = NEW.from_wallet;
    END IF;
    
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_wallet_daily_spent
    AFTER UPDATE ON transactions
    FOR EACH ROW
    WHEN (NEW.status = 'completed' AND OLD.status != 'completed')
    EXECUTE FUNCTION update_daily_spending();

-- Create trigger for updating wallet balances
CREATE OR REPLACE FUNCTION update_wallet_balances()
RETURNS TRIGGER AS $$
BEGIN
    -- Only process completed transactions
    IF NEW.status = 'completed' AND OLD.status != 'completed' THEN
        -- Handle outgoing transaction
        IF NEW.from_wallet IS NOT NULL THEN
            IF NEW.currency = 'credits' THEN
                UPDATE wallets
                SET balance_credits = balance_credits - NEW.amount,
                    updated_at = NOW()
                WHERE id = NEW.from_wallet;
            ELSE
                UPDATE wallets
                SET balance_usdc = balance_usdc - NEW.amount,
                    updated_at = NOW()
                WHERE id = NEW.from_wallet;
            END IF;
        END IF;
        
        -- Handle incoming transaction
        IF NEW.to_wallet IS NOT NULL THEN
            IF NEW.currency = 'credits' THEN
                UPDATE wallets
                SET balance_credits = balance_credits + NEW.amount,
                    updated_at = NOW()
                WHERE id = NEW.to_wallet;
            ELSE
                UPDATE wallets
                SET balance_usdc = balance_usdc + NEW.amount,
                    updated_at = NOW()
                WHERE id = NEW.to_wallet;
            END IF;
        END IF;
    END IF;
    
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_wallet_balances_trigger
    AFTER UPDATE ON transactions
    FOR EACH ROW
    WHEN (NEW.status = 'completed' AND OLD.status != 'completed')
    EXECUTE FUNCTION update_wallet_balances();

-- Create function to reset daily spending
CREATE OR REPLACE FUNCTION reset_daily_spending()
RETURNS VOID AS $$
BEGIN
    -- Update daily_spent to 0 for all wallets
    UPDATE wallets
    SET daily_spent = 0,
        daily_reset_at = NOW(),
        updated_at = NOW();
    
    -- Delete old daily spending records (older than 30 days)
    DELETE FROM daily_spending
    WHERE date < CURRENT_DATE - INTERVAL '30 days';
END;
$$ language 'plpgsql';

-- Create function to reset agent timeout count
CREATE OR REPLACE FUNCTION reset_agent_timeout_counts()
RETURNS VOID AS $$
BEGIN
    UPDATE agents
    SET timeout_count = 0,
        updated_at = NOW();
END;
$$ language 'plpgsql';

-- Create admin user (for testing purposes)
INSERT INTO users (email, password_hash, kyc_status)
VALUES ('admin@agentnet.io', '$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW', 'verified')
ON CONFLICT (email) DO NOTHING;