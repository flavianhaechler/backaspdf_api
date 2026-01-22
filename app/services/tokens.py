"""
Token management - signed tokens for billing portal access.
"""
import hmac
import hashlib
import time
import base64
import json
import logging

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def create_portal_token(email: str) -> str:
    """
    Create a signed token for billing portal access.
    
    Token contains:
    - email: User's email
    - exp: Expiration timestamp
    
    Returns: Base64-encoded signed token
    """
    ttl_seconds = settings.portal_token_ttl_minutes * 60
    exp = int(time.time()) + ttl_seconds
    
    payload = {
        "email": email.lower(),
        "exp": exp,
    }
    
    # Encode payload
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()
    
    # Create signature
    signature = _sign(payload_b64)
    
    # Combine: payload.signature
    token = f"{payload_b64}.{signature}"
    
    return token


def verify_portal_token(token: str) -> dict:
    """
    Verify and decode a portal token.
    
    Returns: Decoded payload dict
    Raises: ValueError if invalid or expired
    """
    try:
        # Split token
        parts = token.split(".")
        if len(parts) != 2:
            raise ValueError("Invalid token format")
        
        payload_b64, signature = parts
        
        # Verify signature
        expected_signature = _sign(payload_b64)
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("Invalid token signature")
        
        # Decode payload
        payload_json = base64.urlsafe_b64decode(payload_b64).decode()
        payload = json.loads(payload_json)
        
        # Check expiration
        if payload.get("exp", 0) < time.time():
            raise ValueError("Token expired")
        
        return payload
        
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Token decode error: {e}")
        raise ValueError("Invalid token")


def _sign(data: str) -> str:
    """Create HMAC signature."""
    signature = hmac.new(
        settings.token_signing_secret.encode(),
        data.encode(),
        hashlib.sha256,
    ).digest()
    
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")
