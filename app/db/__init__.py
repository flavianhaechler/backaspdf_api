"""
Database connection and queries.
"""
import logging
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager

import asyncpg

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Connection pool (initialized on startup)
_pool: Optional[asyncpg.Pool] = None


async def init_db():
    """Initialize database connection pool and run migrations."""
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
    )
    
    # Run schema
    async with _pool.acquire() as conn:
        from pathlib import Path
        schema_path = Path(__file__).parent / "schema.sql"
        schema = schema_path.read_text()
        await conn.execute(schema)
    
    logger.info("Database initialized")


async def close_db():
    """Close database connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Get the connection pool."""
    if not _pool:
        raise RuntimeError("Database not initialized")
    return _pool


@asynccontextmanager
async def get_conn():
    """Get a database connection from the pool."""
    async with get_pool().acquire() as conn:
        yield conn


# ===================
# SENDERS (Users)
# ===================

async def get_sender(email: str) -> Optional[dict]:
    """Get sender by email."""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM senders WHERE email = $1",
            email.lower()
        )
        return dict(row) if row else None


async def create_sender(email: str) -> dict:
    """Create a new sender."""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO senders (email, plan, created_at)
            VALUES ($1, 'free', NOW())
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING *
            """,
            email.lower()
        )
        return dict(row)


async def update_sender_stripe(email: str, customer_id: str, subscription_id: str = None):
    """Update sender with Stripe IDs."""
    async with get_conn() as conn:
        await conn.execute(
            """
            UPDATE senders 
            SET stripe_customer_id = $2, stripe_subscription_id = $3
            WHERE email = $1
            """,
            email.lower(), customer_id, subscription_id
        )


async def update_sender_plan(email: str, plan: str):
    """Update sender's plan."""
    async with get_conn() as conn:
        await conn.execute(
            "UPDATE senders SET plan = $2 WHERE email = $1",
            email.lower(), plan
        )


async def get_sender_by_customer_id(customer_id: str) -> Optional[dict]:
    """Get sender by Stripe customer ID."""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM senders WHERE stripe_customer_id = $1",
            customer_id
        )
        return dict(row) if row else None


# ===================
# USAGE TRACKING
# ===================

async def get_usage(email: str) -> dict:
    """Get current month's usage for sender."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM sender_usage 
            WHERE sender_email = $1 AND month_year = $2
            """,
            email.lower(), month
        )
        if row:
            return dict(row)
        return {"sender_email": email, "month_year": month, "count": 0}


async def increment_usage(email: str) -> int:
    """Increment usage and return new count."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO sender_usage (sender_email, month_year, count)
            VALUES ($1, $2, 1)
            ON CONFLICT (sender_email, month_year) 
            DO UPDATE SET count = sender_usage.count + 1
            RETURNING count
            """,
            email.lower(), month
        )
        return row["count"]


_ALLOWED_FLAGS = {"warned_upgrade", "warned_fair_use"}


async def set_warned_flag(email: str, flag: str, value: bool = True):
    """Set warned_upgrade or warned_fair_use flag."""
    if flag not in _ALLOWED_FLAGS:
        raise ValueError(f"Invalid flag: {flag}")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    async with get_conn() as conn:
        await conn.execute(
            f"""
            UPDATE sender_usage
            SET {flag} = $3
            WHERE sender_email = $1 AND month_year = $2
            """,
            email.lower(), month, value
        )


# ===================
# CONVERSIONS (Job Queue)
# ===================

async def create_job(
    message_id: str,
    from_email: str,
    subject: str,
    payload: dict,
    job_type: str = "render",
) -> str:
    """Create a conversion job."""
    import json

    from_domain = from_email.split("@")[1] if "@" in from_email else ""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO conversions
            (message_id, from_email, from_domain, subject, job_type, email_payload, status, created_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'queued', NOW())
            RETURNING id
            """,
            message_id, from_email.lower(), from_domain, subject, job_type, json.dumps(payload)
        )
        return str(row["id"])


async def get_pending_jobs(limit: int = 5) -> list[dict]:
    """Get pending jobs for processing."""
    async with get_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM conversions 
            WHERE status = 'queued'
            ORDER BY created_at ASC
            LIMIT $1
            FOR UPDATE SKIP LOCKED
            """,
            limit
        )
        return [dict(r) for r in rows]


async def update_job_status(job_id: str, status: str, error: str = None):
    """Update job status."""
    async with get_conn() as conn:
        await conn.execute(
            """
            UPDATE conversions 
            SET status = $2,
                error_code = $3,
                processed_at = NOW(),
                email_payload = CASE
                    WHEN $2 IN ('completed', 'failed', 'rejected') THEN '{}'::jsonb
                    ELSE email_payload
                END
            WHERE id = $1
            """,
            job_id, status, error
        )


async def job_exists(message_id: str) -> bool:
    """Check if job already exists (deduplication)."""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM conversions WHERE message_id = $1",
            message_id
        )
        return row is not None


# ===================
# DOCUMENTS (Archive)
# ===================

async def save_document(
    owner_email: str,
    source_message_id: str,
    subject: str,
    from_email: str,
    pdf_filename: str,
    pdf_size_bytes: int,
    merchant_name: str = None,
    amount: float = None,
    currency: str = None,
    category: str = None,
) -> str:
    """Save document record for receipt archive."""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO documents 
            (owner_email, source_message_id, original_subject, original_from,
             pdf_filename, pdf_size_bytes, merchant_name, amount, currency, category,
             received_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            RETURNING id
            """,
            owner_email.lower(), source_message_id, subject, from_email,
            pdf_filename, pdf_size_bytes, merchant_name, amount, currency, category
        )
        return str(row["id"])
