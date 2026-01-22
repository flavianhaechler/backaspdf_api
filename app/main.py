"""
BackasPDF API - main application.

Endpoints:
- GET  /health              Health check
- POST /webhooks/resend     Inbound email webhook
- POST /webhooks/stripe     Stripe payment webhook
- POST /billing/checkout    Create checkout session
- POST /billing/portal_link Generate portal link
- GET  /billing/portal      Redirect to Stripe portal

Future:
- POST /v1/convert/html     Developer API
- POST /v1/convert/email    Developer API
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

from app.config import get_settings
from app import db
from app.services import (
    stripe,
    tokens,
    quota,
    check_quota,
    QuotaResult,
)
from app.services.pdf import close_browser

settings = get_settings()

# Logging
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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.public_app_url,
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===================
# HEALTH
# ===================

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
    
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "database": db_status,
        "version": "1.0.0",
    }


# ===================
# RESEND WEBHOOK
# ===================

@app.post("/webhooks/resend")
async def resend_webhook(request: Request):
    """
    Handle inbound email from Resend.
    
    Creates a job in the queue for the worker to process.
    """
    # TODO: Verify webhook signature
    # signature = request.headers.get("svix-signature", "")
    
    try:
        payload = await request.json()
    except:
        raise HTTPException(400, "Invalid JSON")
    
    # Extract email data
    from_email = payload.get("from", "")
    if "<" in from_email:
        from_email = from_email.split("<")[1].split(">")[0]
    
    message_id = payload.get("messageId", payload.get("message_id", ""))
    subject = payload.get("subject", "")
    
    if not from_email or not message_id:
        logger.warning("Missing from_email or message_id in webhook")
        raise HTTPException(400, "Missing required fields")
    
    # Deduplicate
    if await db.job_exists(message_id):
        logger.info(f"Duplicate message ignored: {message_id}")
        return {"status": "duplicate"}
    
    # Check quota BEFORE creating job
    quota_result = await check_quota(from_email)
    
    if quota_result.action.value == "reject":
        logger.info(f"Quota exceeded for {from_email}: {quota_result.message}")
        # Still create job but mark as rejected
        await db.create_job(
            message_id=message_id,
            from_email=from_email,
            subject=subject,
            payload={"rejected": True, "reason": quota_result.message},
        )
        return {"status": "rejected", "reason": quota_result.message}
    
    # Create job
    job_id = await db.create_job(
        message_id=message_id,
        from_email=from_email,
        subject=subject,
        payload=payload,
    )
    
    logger.info(f"Job created: {job_id} for {from_email}")
    
    return {
        "status": "queued",
        "job_id": job_id,
        "warn": quota_result.action.value if "warn" in quota_result.action.value else None,
    }


# ===================
# STRIPE WEBHOOK
# ===================

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
    
    # Handle events
    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(data)
    
    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(data)
    
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(data)
    
    return {"received": True}


async def _handle_checkout_completed(session: dict):
    """Handle successful checkout."""
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")
    email = session.get("customer_email") or session.get("metadata", {}).get("email")
    
    if not email:
        logger.error("No email in checkout session")
        return
    
    # Update or create sender
    sender = await db.get_sender(email)
    if not sender:
        sender = await db.create_sender(email)
    
    await db.update_sender_stripe(email, customer_id, subscription_id)
    await db.update_sender_plan(email, "pro")
    
    logger.info(f"Checkout completed: {email} upgraded to pro")


async def _handle_subscription_updated(subscription: dict):
    """Handle subscription status change."""
    customer_id = subscription.get("customer")
    status = subscription.get("status")
    
    sender = await db.get_sender_by_customer_id(customer_id)
    if not sender:
        logger.warning(f"No sender for customer {customer_id}")
        return
    
    # Update plan based on status
    if status in ("active", "trialing"):
        await db.update_sender_plan(sender["email"], "pro")
    else:
        await db.update_sender_plan(sender["email"], "free")
    
    logger.info(f"Subscription updated: {sender['email']} status={status}")


async def _handle_subscription_deleted(subscription: dict):
    """Handle subscription cancellation."""
    customer_id = subscription.get("customer")
    
    sender = await db.get_sender_by_customer_id(customer_id)
    if not sender:
        return
    
    await db.update_sender_plan(sender["email"], "free")
    logger.info(f"Subscription deleted: {sender['email']} downgraded to free")


# ===================
# BILLING ENDPOINTS
# ===================

class CheckoutRequest(BaseModel):
    email: EmailStr


class PortalLinkRequest(BaseModel):
    email: EmailStr


@app.post("/billing/checkout")
async def create_checkout(req: CheckoutRequest):
    """Create Stripe Checkout session."""
    try:
        url = await stripe.create_checkout_session(req.email)
        return {"url": url}
    except Exception as e:
        logger.error(f"Checkout error: {e}")
        raise HTTPException(500, "Failed to create checkout session")


@app.post("/billing/portal_link")
async def create_portal_link(req: PortalLinkRequest):
    """
    Generate a portal link for the user.
    
    Returns a short-lived URL that redirects to Stripe portal.
    """
    # Check if user has a Stripe customer
    sender = await db.get_sender(req.email)
    
    if not sender or not sender.get("stripe_customer_id"):
        raise HTTPException(404, "No subscription found for this email")
    
    # Create signed token
    token = tokens.create_portal_token(req.email)
    
    # Build portal URL
    url = f"{settings.api_base_url}/billing/portal?token={token}"
    
    return {"url": url}


@app.get("/billing/portal")
async def portal_redirect(token: str):
    """
    Redirect to Stripe Customer Portal.
    
    Verifies token and redirects to Stripe.
    """
    try:
        payload = tokens.verify_portal_token(token)
    except ValueError as e:
        raise HTTPException(401, str(e))
    
    email = payload["email"]
    
    # Get customer ID
    sender = await db.get_sender(email)
    if not sender or not sender.get("stripe_customer_id"):
        raise HTTPException(404, "No subscription found")
    
    # Create portal session and redirect
    try:
        portal_url = await stripe.create_portal_session(sender["stripe_customer_id"])
        return Response(
            status_code=302,
            headers={"Location": portal_url}
        )
    except Exception as e:
        logger.error(f"Portal redirect error: {e}")
        raise HTTPException(500, "Failed to access billing portal")


# ===================
# DEVELOPER API (Future)
# ===================
# 
# When ready to enable:
# 
# from app.services.auth import verify_api_key
# from app.services.storage import upload_pdf, get_pdf_url
# 
# @app.post("/v1/convert/html")
# async def convert_html(
#     request: ConvertHTMLRequest,
#     api_key: APIKey = Depends(verify_api_key),
# ):
#     """Convert HTML to PDF."""
#     pdf_bytes = await render_pdf(request.html, request.options)
#     
#     # Upload to storage
#     doc_id = await upload_pdf(pdf_bytes, api_key.user_email)
#     url = get_pdf_url(doc_id)
#     
#     return {
#         "id": doc_id,
#         "url": url,
#         "size_bytes": len(pdf_bytes),
#     }
