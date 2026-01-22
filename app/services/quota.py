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
    - ALLOW_WARN_UPGRADE: Process + send upgrade email (free user at limit)
    - ALLOW_WARN_FAIR_USE: Process + send fair use notice (pro user high usage)
    - REJECT: Don't process (hard limit exceeded)
    """
    # Get or create sender
    sender = await db.get_sender(email)
    if not sender:
        sender = await db.create_sender(email)
    
    plan = sender["plan"]
    
    # Get current usage
    usage = await db.get_usage(email)
    used = usage["count"]
    warned_upgrade = usage.get("warned_upgrade", False)
    warned_fair_use = usage.get("warned_fair_use", False)
    
    # Determine limits based on plan
    if plan == "pro":
        return await _check_pro_quota(email, used, warned_fair_use)
    else:
        return await _check_free_quota(email, used, warned_upgrade)


async def _check_free_quota(email: str, used: int, warned: bool) -> QuotaResult:
    """Check quota for free users."""
    limit = settings.free_monthly_limit
    
    # Under limit
    if used < limit:
        return QuotaResult(
            action=QuotaAction.ALLOW,
            plan="free",
            used=used,
            limit=limit,
        )
    
    # At limit, first time
    if used == limit and not warned:
        await db.set_warned_flag(email, "warned_upgrade", True)
        return QuotaResult(
            action=QuotaAction.ALLOW_WARN_UPGRADE,
            plan="free",
            used=used,
            limit=limit,
            message="Free limit reached",
        )
    
    # Over limit
    return QuotaResult(
        action=QuotaAction.REJECT,
        plan="free",
        used=used,
        limit=limit,
        message=f"Monthly limit of {limit} conversions reached. Upgrade to Pro for unlimited conversions.",
    )


async def _check_pro_quota(email: str, used: int, warned: bool) -> QuotaResult:
    """Check quota for pro users."""
    soft_limit = settings.pro_soft_limit
    hard_limit = settings.pro_hard_limit
    
    # Under soft limit
    if used < soft_limit:
        return QuotaResult(
            action=QuotaAction.ALLOW,
            plan="pro",
            used=used,
            limit=soft_limit,
        )
    
    # Between soft and hard limit
    if used < hard_limit:
        # First time hitting soft limit
        if not warned:
            await db.set_warned_flag(email, "warned_fair_use", True)
            return QuotaResult(
                action=QuotaAction.ALLOW_WARN_FAIR_USE,
                plan="pro",
                used=used,
                limit=soft_limit,
                message="High usage notice",
            )
        
        # Already warned, still allow
        return QuotaResult(
            action=QuotaAction.ALLOW,
            plan="pro",
            used=used,
            limit=soft_limit,
        )
    
    # Hard limit reached
    return QuotaResult(
        action=QuotaAction.REJECT,
        plan="pro",
        used=used,
        limit=hard_limit,
        message=f"Fair use limit of {hard_limit} conversions reached. Contact support for higher limits.",
    )


async def record_usage(email: str) -> int:
    """
    Record a conversion and return new count.
    
    Call this AFTER successful conversion.
    """
    return await db.increment_usage(email)
