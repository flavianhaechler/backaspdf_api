-- BackasPDF Database Schema
-- Run automatically on startup (idempotent)

-- ===================
-- SENDERS (Users)
-- ===================
CREATE TABLE IF NOT EXISTS senders (
    email VARCHAR(255) PRIMARY KEY,
    plan VARCHAR(20) DEFAULT 'free',  -- free, pro
    stripe_customer_id VARCHAR(255),
    stripe_subscription_id VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_senders_customer_id ON senders(stripe_customer_id);

-- ===================
-- USAGE TRACKING
-- ===================
CREATE TABLE IF NOT EXISTS sender_usage (
    id SERIAL PRIMARY KEY,
    sender_email VARCHAR(255) NOT NULL,
    month_year VARCHAR(7) NOT NULL,  -- '2025-01'
    count INTEGER DEFAULT 0,
    warned_upgrade BOOLEAN DEFAULT FALSE,
    warned_fair_use BOOLEAN DEFAULT FALSE,
    UNIQUE(sender_email, month_year)
);

CREATE INDEX IF NOT EXISTS idx_usage_email_month ON sender_usage(sender_email, month_year);

-- ===================
-- CONVERSIONS (Job Queue)
-- ===================
CREATE TABLE IF NOT EXISTS conversions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id VARCHAR(255) NOT NULL UNIQUE,
    from_email VARCHAR(255) NOT NULL,
    from_domain VARCHAR(255) NOT NULL,
    subject TEXT,
    job_type VARCHAR(20) DEFAULT 'render',  -- render, merge, split
    email_payload JSONB NOT NULL,
    status VARCHAR(20) DEFAULT 'queued',  -- queued, processing, completed, failed, rejected
    error_code VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    processed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_conversions_status ON conversions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_conversions_email ON conversions(from_email);

-- ===================
-- DOCUMENTS (Receipt Archive)
-- ===================
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_email VARCHAR(255) NOT NULL,
    source_message_id VARCHAR(255),
    
    -- Original email metadata
    original_subject TEXT,
    original_from VARCHAR(255),
    received_at TIMESTAMP WITH TIME ZONE,
    
    -- Extracted receipt data
    merchant_name VARCHAR(255),
    amount DECIMAL(10, 2),
    currency VARCHAR(3),
    document_date DATE,
    category VARCHAR(50),
    
    -- PDF storage
    pdf_filename VARCHAR(255),
    pdf_size_bytes INTEGER,
    pdf_storage_key VARCHAR(255),  -- Future: S3/R2 key
    
    -- Organization
    tags TEXT[],
    notes TEXT,
    is_archived BOOLEAN DEFAULT FALSE,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner_email);
CREATE INDEX IF NOT EXISTS idx_documents_owner_date ON documents(owner_email, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(owner_email, category);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_message_id);

-- ===================
-- API KEYS (Future: Developer API)
-- ===================
-- Uncomment when enabling Developer API:
--
-- CREATE TABLE IF NOT EXISTS api_keys (
--     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
--     key_hash VARCHAR(64) NOT NULL UNIQUE,  -- SHA-256 hash
--     user_email VARCHAR(255) NOT NULL,
--     name VARCHAR(100) NOT NULL,
--     plan VARCHAR(20) DEFAULT 'free',  -- free, starter, growth, scale
--     created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
--     last_used_at TIMESTAMP WITH TIME ZONE,
--     revoked_at TIMESTAMP WITH TIME ZONE
-- );
--
-- CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
-- CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_email);

-- ===================
-- API USAGE (Future: Developer API)
-- ===================
-- Uncomment when enabling Developer API:
--
-- CREATE TABLE IF NOT EXISTS api_usage (
--     id SERIAL PRIMARY KEY,
--     api_key_id UUID NOT NULL REFERENCES api_keys(id),
--     month_year VARCHAR(7) NOT NULL,  -- '2025-01'
--     pdf_count INTEGER DEFAULT 0,
--     UNIQUE(api_key_id, month_year)
-- );
