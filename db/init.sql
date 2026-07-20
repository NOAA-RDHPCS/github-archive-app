-- GitHub Archive Database Schema
-- PostgreSQL with immutability guarantees

-- =============================================================================
-- EXTENSIONS
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================================
-- MAIN TABLES (APPEND-ONLY)
-- =============================================================================

-- Raw webhook events - stores complete payload as received
CREATE TABLE webhook_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- GitHub delivery metadata
    delivery_id VARCHAR(64) NOT NULL UNIQUE,
    event_type VARCHAR(64) NOT NULL,
    action VARCHAR(64),
    
    -- Source identification
    organization VARCHAR(255) NOT NULL,
    repository VARCHAR(255),
    sender VARCHAR(255),
    
    -- Complete payload (immutable record)
    payload JSONB NOT NULL,
    
    -- Security/integrity
    signature VARCHAR(128) NOT NULL,  -- X-Hub-Signature-256
    source_ip INET NOT NULL,
    checksum VARCHAR(64) NOT NULL,    -- SHA-256 of payload
    
    -- Indexing
    github_id BIGINT,                 -- GitHub's ID for the resource
    
    -- Metadata
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Extracted content versions - normalized view with version tracking
CREATE TABLE content_versions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Link to raw event
    webhook_event_id UUID NOT NULL REFERENCES webhook_events(id),
    
    -- Content identification
    content_type VARCHAR(64) NOT NULL,  -- issue, pull_request, comment, etc.
    github_id BIGINT NOT NULL,          -- GitHub's ID for this content
    github_node_id VARCHAR(255),        -- GitHub's GraphQL node ID
    
    -- Version tracking
    version_number INTEGER NOT NULL DEFAULT 1,
    previous_version_id UUID REFERENCES content_versions(id),
    is_deletion BOOLEAN NOT NULL DEFAULT FALSE,
    
    -- Content (at this version)
    title TEXT,
    body TEXT,
    state VARCHAR(64),
    
    -- Metadata as JSON (labels, assignees, reviewers, etc.)
    metadata JSONB NOT NULL DEFAULT '{}',
    
    -- Actor who made this change
    actor_login VARCHAR(255) NOT NULL,
    actor_id BIGINT NOT NULL,
    
    -- Timestamps
    github_created_at TIMESTAMPTZ,
    github_updated_at TIMESTAMPTZ,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Integrity
    checksum VARCHAR(64) NOT NULL,  -- SHA-256 of content fields
    
    -- Ensure unique version per content item
    UNIQUE (content_type, github_id, version_number)
);

-- Audit log for system access (also append-only)
CREATE TABLE access_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    action VARCHAR(64) NOT NULL,  -- query, export, verify, etc.
    source_ip INET,
    user_agent TEXT,
    api_key_hash VARCHAR(64),     -- Hashed API key if authenticated
    
    -- What was accessed
    query_params JSONB,
    result_count INTEGER,
    
    -- Always log, even errors
    success BOOLEAN NOT NULL,
    error_message TEXT
);

-- GitHub IP allowlist cache
CREATE TABLE github_ips (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ip_range CIDR NOT NULL,
    category VARCHAR(64) NOT NULL DEFAULT 'hooks',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE (ip_range, category)
);

-- =============================================================================
-- INDEXES
-- =============================================================================

-- Webhook events indexes
CREATE INDEX idx_webhook_events_received_at ON webhook_events(received_at);
CREATE INDEX idx_webhook_events_event_type ON webhook_events(event_type);
CREATE INDEX idx_webhook_events_organization ON webhook_events(organization);
CREATE INDEX idx_webhook_events_repository ON webhook_events(repository);
CREATE INDEX idx_webhook_events_github_id ON webhook_events(github_id);
CREATE INDEX idx_webhook_events_payload_gin ON webhook_events USING GIN(payload);

-- Content versions indexes
CREATE INDEX idx_content_versions_content_type ON content_versions(content_type);
CREATE INDEX idx_content_versions_github_id ON content_versions(github_id);
CREATE INDEX idx_content_versions_captured_at ON content_versions(captured_at);
CREATE INDEX idx_content_versions_actor ON content_versions(actor_login);
CREATE INDEX idx_content_versions_previous ON content_versions(previous_version_id);
CREATE INDEX idx_content_versions_webhook ON content_versions(webhook_event_id);

-- Access log indexes
CREATE INDEX idx_access_log_timestamp ON access_log(timestamp);
CREATE INDEX idx_access_log_action ON access_log(action);

-- =============================================================================
-- IMMUTABILITY TRIGGERS
-- =============================================================================

-- Function to prevent any UPDATE operations
CREATE OR REPLACE FUNCTION prevent_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'UPDATE operations are not permitted on table %. This is an immutable archive.', TG_TABLE_NAME
        USING HINT = 'Records in this archive cannot be modified. Create a new version record instead.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Function to prevent any DELETE operations
CREATE OR REPLACE FUNCTION prevent_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'DELETE operations are not permitted on table %. This is an immutable archive.', TG_TABLE_NAME
        USING HINT = 'Records in this archive cannot be deleted. Mark as deleted by creating a deletion version record.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Function to prevent TRUNCATE
CREATE OR REPLACE FUNCTION prevent_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE operations are not permitted on table %. This is an immutable archive.', TG_TABLE_NAME;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Apply immutability triggers to webhook_events
CREATE TRIGGER webhook_events_no_update
    BEFORE UPDATE ON webhook_events
    FOR EACH ROW EXECUTE FUNCTION prevent_update();

CREATE TRIGGER webhook_events_no_delete
    BEFORE DELETE ON webhook_events
    FOR EACH ROW EXECUTE FUNCTION prevent_delete();

CREATE TRIGGER webhook_events_no_truncate
    BEFORE TRUNCATE ON webhook_events
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_truncate();

-- Apply immutability triggers to content_versions
CREATE TRIGGER content_versions_no_update
    BEFORE UPDATE ON content_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_update();

CREATE TRIGGER content_versions_no_delete
    BEFORE DELETE ON content_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_delete();

CREATE TRIGGER content_versions_no_truncate
    BEFORE TRUNCATE ON content_versions
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_truncate();

-- Apply immutability triggers to access_log
CREATE TRIGGER access_log_no_update
    BEFORE UPDATE ON access_log
    FOR EACH ROW EXECUTE FUNCTION prevent_update();

CREATE TRIGGER access_log_no_delete
    BEFORE DELETE ON access_log
    FOR EACH ROW EXECUTE FUNCTION prevent_delete();

CREATE TRIGGER access_log_no_truncate
    BEFORE TRUNCATE ON access_log
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_truncate();

-- =============================================================================
-- READ-ONLY USER (for queries)
-- =============================================================================

-- Create read-only user (password set via environment variable)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'archive_reader') THEN
        CREATE ROLE archive_reader WITH LOGIN PASSWORD 'readonly_password_change_me';
    END IF;
END
$$;

-- Grant read-only access
GRANT CONNECT ON DATABASE github_archive TO archive_reader;
GRANT USAGE ON SCHEMA public TO archive_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO archive_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO archive_reader;

-- Explicitly deny write permissions
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM archive_reader;

-- =============================================================================
-- HELPER VIEWS
-- =============================================================================

-- View: Latest version of each content item
CREATE VIEW content_latest AS
SELECT DISTINCT ON (content_type, github_id)
    cv.*,
    we.organization,
    we.repository
FROM content_versions cv
JOIN webhook_events we ON cv.webhook_event_id = we.id
ORDER BY content_type, github_id, version_number DESC;

-- View: Content with full version history
CREATE VIEW content_history AS
SELECT 
    cv.*,
    we.organization,
    we.repository,
    we.event_type,
    we.action
FROM content_versions cv
JOIN webhook_events we ON cv.webhook_event_id = we.id
ORDER BY cv.content_type, cv.github_id, cv.version_number;

-- View: Daily event counts (for monitoring)
CREATE VIEW daily_event_counts AS
SELECT 
    DATE(received_at) as date,
    organization,
    event_type,
    COUNT(*) as event_count
FROM webhook_events
GROUP BY DATE(received_at), organization, event_type
ORDER BY date DESC, organization, event_type;

-- =============================================================================
-- INTEGRITY CHECK FUNCTION
-- =============================================================================

-- Function to verify checksums (called by verification script)
CREATE OR REPLACE FUNCTION verify_webhook_checksum(event_id UUID)
RETURNS TABLE(
    id UUID,
    stored_checksum VARCHAR(64),
    computed_checksum VARCHAR(64),
    is_valid BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        we.id,
        we.checksum as stored_checksum,
        encode(digest(we.payload::text, 'sha256'), 'hex') as computed_checksum,
        we.checksum = encode(digest(we.payload::text, 'sha256'), 'hex') as is_valid
    FROM webhook_events we
    WHERE we.id = event_id;
END;
$$ LANGUAGE plpgsql;

-- Function to verify all checksums (batch verification)
CREATE OR REPLACE FUNCTION verify_all_checksums()
RETURNS TABLE(
    total_records BIGINT,
    valid_records BIGINT,
    invalid_records BIGINT,
    invalid_ids UUID[]
) AS $$
DECLARE
    invalid_list UUID[];
BEGIN
    SELECT ARRAY_AGG(we.id) INTO invalid_list
    FROM webhook_events we
    WHERE we.checksum != encode(digest(we.payload::text, 'sha256'), 'hex');
    
    RETURN QUERY
    SELECT 
        COUNT(*)::BIGINT as total_records,
        COUNT(*) FILTER (WHERE checksum = encode(digest(payload::text, 'sha256'), 'hex'))::BIGINT as valid_records,
        COUNT(*) FILTER (WHERE checksum != encode(digest(payload::text, 'sha256'), 'hex'))::BIGINT as invalid_records,
        COALESCE(invalid_list, ARRAY[]::UUID[]) as invalid_ids
    FROM webhook_events;
END;
$$ LANGUAGE plpgsql;
