CREATE TABLE IF NOT EXISTS audit_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id uuid,
    actor_ip inet,
    action text NOT NULL,
    target_id text,
    payload_summary text,
    success boolean DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_action_created_at ON audit_log (action, created_at);