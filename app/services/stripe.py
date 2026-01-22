"""
Stripe integration - checkout, portal, and webhook handling.
"""
import logging
from typing import Optional

import stripe

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Initialize Stripe
stripe.api_key = settings.stripe_secret_key


async def create_checkout_session(email: str) -> str:
    """
    Create Stripe Checkout session for Pro subscription.
    
    Returns: Checkout URL
    """
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer_email=email,
            line_items=[{
                "price": settings.stripe_price_id_pro,
                "quantity": 1,
            }],
            success_url=f"{settings.public_app_url}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{settings.public_app_url}/subscribe?canceled=1",
            metadata={
                "email": email,
            },
        )
        
        logger.info(f"Checkout session created for {email}: {session.id}")
        return session.url
        
    except stripe.error.StripeError as e:
        logger.error(f"Stripe checkout error: {e}")
        raise


async def create_portal_session(customer_id: str, return_url: str = None) -> str:
    """
    Create Stripe Customer Portal session.
    
    Returns: Portal URL
    """
    if return_url is None:
        return_url = f"{settings.public_app_url}/manage"
    
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
        
        logger.info(f"Portal session created for customer {customer_id}")
        return session.url
        
    except stripe.error.StripeError as e:
        logger.error(f"Stripe portal error: {e}")
        raise


async def get_customer_by_email(email: str) -> Optional[str]:
    """
    Find Stripe customer ID by email.
    
    Returns: Customer ID or None
    """
    try:
        customers = stripe.Customer.list(email=email, limit=1)
        if customers.data:
            return customers.data[0].id
        return None
        
    except stripe.error.StripeError as e:
        logger.error(f"Stripe customer lookup error: {e}")
        return None


async def get_subscription_status(customer_id: str) -> Optional[str]:
    """
    Get subscription status for customer.
    
    Returns: Status string (active, canceled, past_due, etc.) or None
    """
    try:
        subscriptions = stripe.Subscription.list(
            customer=customer_id,
            status="all",
            limit=1,
        )
        
        if subscriptions.data:
            return subscriptions.data[0].status
        return None
        
    except stripe.error.StripeError as e:
        logger.error(f"Stripe subscription lookup error: {e}")
        return None


def construct_webhook_event(payload: bytes, signature: str) -> stripe.Event:
    """
    Construct and verify Stripe webhook event.
    
    Raises: stripe.error.SignatureVerificationError if invalid
    """
    return stripe.Webhook.construct_event(
        payload,
        signature,
        settings.stripe_webhook_secret,
    )
