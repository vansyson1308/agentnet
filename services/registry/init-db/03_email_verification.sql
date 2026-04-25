-- Migration: Add email verification support
-- Table for storing email verification tokens

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    token text UNIQUE NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz DEFAULT now()
);

-- Create an index on token for faster lookups (though UNIQUE already creates one)
CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_token ON email_verification_tokens(token);

-- Add email verification flag to users table
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_email_verified boolean DEFAULT false;