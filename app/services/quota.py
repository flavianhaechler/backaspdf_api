"""
Quota management - check and enforce usage limits.
"""
import logging
from dataclasses import dataclass
from enum import Enum

from app.config import get_settings
from app import db

logger = logging.getLogger(__name__)
settings = get_settings()


class QuotaAction(Enum):
    """What to do after quota check."""
    ALLOW = "allow"
    ALLOW_WARN_UPGRADE = "allow_warn_upgrade"
    ALLOW_WARN_FAIR_USE = "allow_warn_fair_use"
    REJECT = "reject"


@dataclass
class QuotaResult:
    """Result of quota check."""
    action: QuotaAction
    plan: str
    used: int
    limit: int
    message: str = ""


async def check_quota(email: str) -> QuotaResult:
    """
    Check if sender can make a conversion.
    
    Returns QuotaResult with action to take:
    - ALLOW: Process normally
    - ALLOW_WARN_UPGRADE: Process + send the one-time monthly limit notice
    - REJECT: Don't process (monthly limit exceeded)
    """
    # Get or create sender
    sender = await db.get_sender(email)
    if not sender:
        sender = await db.create_sender(email)
    
    # Get current usage
    usage = await db.get_usage(email)
    used = usage["count"]
    warned_upgrade = usage.get("warned_upgrade", False)
    limit = settings.monthly_conversion_limit

    if used < limit:
        return QuotaResult(
            action=QuotaAction.ALLOW,
            plan="free",
            used=used,
            limit=limit,
        )

    if used == limit and not warned_upgrade:
        await db.set_warned_flag(email, "warned_upgrade", True)
        return QuotaResult(
            action=QuotaAction.ALLOW_WARN_UPGRADE,
            plan="free",
            used=used,
            limit=limit,
            message="Monthly conversion limit reached",
        )

    return QuotaResult(
        action=QuotaAction.REJECT,
        plan="free",
        used=used,
        limit=limit,
        message=f"Monthly limit of {limit} conversions reached. Your limit resets at the start of next month.",
    )


async def record_usage(email: str) -> int:
    """
    Record a conversion and return new count.
    
    Call this AFTER successful conversion.
    """
    return await db.increment_usage(email)
