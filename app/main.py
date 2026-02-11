"""
BackasPDF API - main application.
"""
import html
import logging
import time
import httpx
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

from app.config import get_settings
from app import db
from app.services import (
    stripe,
    tokens,
    check_quota,
    send_contact_email,
)
from app.services.pdf import close_browser

settings = get_settings()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    logger.info("Starting BackasPDF API")
    await db.init_db()
    yield
    logger.info("Shutting down")
    await close_browser()
    await db.close_db()


app = FastAPI(
    title="BackasPDF API",
    version="1.0.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_app_url, "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check."""
    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "ok"
    except:
        db_status = "error"
    
    return {"status": "ok" if db_status == "ok" else "degraded", "database": db_status, "version": "1.0.0"}


@app.post("/webhooks/resend")
async def resend_webhook(request: Request):
    """Handle inbound email from Resend."""
    try:
        payload = await request.json()
    except:
        raise HTTPException(400, "Invalid JSON")
    
    # Resend wraps email in "data" field
    data = payload.get("data", payload)
    
    # Extract metadata
    from_email = data.get("from", "")
    if "<" in from_email:
        from_email = from_email.split("<")[1].split(">")[0]
    
    message_id = data.get("messageId", data.get("message_id", ""))
    subject = data.get("subject", "")
    email_id = data.get("email_id", "")

    # Determine job type from To-Email
    to_field = data.get("to", [""])[0] if isinstance(data.get("to"), list) else data.get("to", "")
    to_lower = to_field.lower()
    if "merge@" in to_lower:
        job_type = "merge"
    elif "split@" in to_lower:
        job_type = "split"
    else:
        job_type = "render"
    logger.info(f"Job type: {job_type} (to: {to_field})")
    
    if not from_email or not message_id:
        logger.warning("Missing from_email or message_id")
        raise HTTPException(400, "Missing required fields")
    
    # Fetch full email content from Resend API
    if email_id:
        logger.info(f"Fetching email content for {email_id}")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://api.resend.com/emails/receiving/{email_id}",
                    headers={"Authorization": f"Bearer {settings.resend_api_key}"}
                )
                logger.info(f"Resend API response: {response.status_code}")
                if response.status_code == 200:
                    email_data = response.json()
                    data["html"] = email_data.get("html", "")
                    data["text"] = email_data.get("text", "")
                    data["headers"] = email_data.get("headers", [])
                    logger.info(f"Fetched: HTML={len(data.get('html',''))} Text={len(data.get('text',''))}")
                else:
                    logger.error(f"Failed to fetch email: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Error fetching email: {e}")
    
    # Deduplicate
    if await db.job_exists(message_id):
        return {"status": "duplicate"}
    
    # Check quota
    quota_result = await check_quota(from_email)
    if quota_result.action.value == "reject":
        await db.create_job(message_id=message_id, from_email=from_email, subject=subject, payload={"rejected": True, "reason": quota_result.message}, job_type=job_type)
        return {"status": "rejected"}

    # Create job
    job_id = await db.create_job(message_id=message_id, from_email=from_email, subject=subject, payload=data, job_type=job_type)
    logger.info(f"Job created: {job_id} for {from_email}")
    
    return {"status": "queued", "job_id": job_id}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    
    try:
        event = stripe.construct_webhook_event(payload, signature)
    except Exception as e:
        logger.error(f"Stripe webhook verification failed: {e}")
        raise HTTPException(400, "Invalid signature")
    
    event_type = event["type"]
    data = event["data"]["object"]
    logger.info(f"Stripe event: {event_type}")
    
    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(data)
    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(data)
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(data)
    
    return {"received": True}


async def _handle_checkout_completed(session: dict):
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")
    email = session.get("customer_email") or session.get("metadata", {}).get("email")
    
    if not email:
        logger.error("No email in checkout session")
        return
    
    sender = await db.get_sender(email)
    if not sender:
        sender = await db.create_sender(email)
    
    await db.update_sender_stripe(email, customer_id, subscription_id)
    await db.update_sender_plan(email, "pro")
    logger.info(f"Checkout completed: {email} upgraded to pro")


async def _handle_subscription_updated(subscription: dict):
    customer_id = subscription.get("customer")
    status = subscription.get("status")
    
    sender = await db.get_sender_by_customer_id(customer_id)
    if not sender:
        return
    
    if status in ("active", "trialing"):
        await db.update_sender_plan(sender["email"], "pro")
    else:
        await db.update_sender_plan(sender["email"], "free")
    logger.info(f"Subscription updated: {sender['email']} status={status}")


async def _handle_subscription_deleted(subscription: dict):
    customer_id = subscription.get("customer")
    sender = await db.get_sender_by_customer_id(customer_id)
    if not sender:
        return
    await db.update_sender_plan(sender["email"], "free")
    logger.info(f"Subscription deleted: {sender['email']} downgraded to free")


# --- Rate limiter for /contact (in-memory, per-process) ---
_contact_rate: dict[str, list[float]] = defaultdict(list)
CONTACT_RATE_LIMIT = 3
CONTACT_RATE_WINDOW = 3600  # 1 hour


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    timestamps = _contact_rate[ip]
    # Remove expired entries
    _contact_rate[ip] = [t for t in timestamps if now - t < CONTACT_RATE_WINDOW]
    if len(_contact_rate[ip]) >= CONTACT_RATE_LIMIT:
        return True
    _contact_rate[ip].append(now)
    return False


class ContactRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr = Field(..., max_length=255)
    message: str = Field(..., min_length=1, max_length=2000)


@app.post("/contact")
async def contact(req: ContactRequest, request: Request):
    """Handle contact form submissions."""
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
    if _is_rate_limited(ip):
        return Response(
            content='{"error":"Rate limited"}',
            status_code=429,
            media_type="application/json",
        )

    # Trim and sanitize (strip HTML)
    name = html.escape(req.name.strip())
    email = req.email.strip()
    message = html.escape(req.message.strip())

    if not name or not message:
        raise HTTPException(400, "Invalid input")

    success = await send_contact_email(name, email, message)
    if not success:
        raise HTTPException(500, "Internal error")

    return {"status": "sent"}


class CheckoutRequest(BaseModel):
    email: EmailStr

class PortalLinkRequest(BaseModel):
    email: EmailStr


@app.post("/billing/checkout")
async def create_checkout(req: CheckoutRequest):
    try:
        url = await stripe.create_checkout_session(req.email)
        return {"url": url}
    except Exception as e:
        logger.error(f"Checkout error: {e}")
        raise HTTPException(500, "Failed to create checkout session")


@app.post("/billing/portal_link")
async def create_portal_link(req: PortalLinkRequest):
    sender = await db.get_sender(req.email)
    if not sender or not sender.get("stripe_customer_id"):
        raise HTTPException(404, "No subscription found for this email")
    
    token = tokens.create_portal_token(req.email)
    url = f"{settings.api_base_url}/billing/portal?token={token}"
    return {"url": url}


@app.get("/billing/portal")
async def portal_redirect(token: str):
    try:
        payload = tokens.verify_portal_token(token)
    except ValueError as e:
        raise HTTPException(401, str(e))
    
    email = payload["email"]
    sender = await db.get_sender(email)
    if not sender or not sender.get("stripe_customer_id"):
        raise HTTPException(404, "No subscription found")
    
    try:
        portal_url = await stripe.create_portal_session(sender["stripe_customer_id"])
        return Response(status_code=302, headers={"Location": portal_url})
    except Exception as e:
        logger.error(f"Portal redirect error: {e}")
        raise HTTPException(500, "Failed to access billing portal")