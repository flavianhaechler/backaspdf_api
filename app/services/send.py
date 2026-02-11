"""
Email sending - send PDF replies and notification emails via Resend.
"""
import base64
import logging
import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

RESEND_API_URL = "https://api.resend.com/emails"


async def send_pdf_reply(
    to_email: str,
    subject: str,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> bool:
    """
    Send PDF as email attachment.
    
    The reply email contains the converted PDF attached.
    """
    # Prepare email content
    html_body = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #333;">Your PDF is ready! 📄</h2>
        <p style="color: #666; line-height: 1.6;">
            We've converted your email to PDF and attached it to this message.
        </p>
        <p style="color: #666; line-height: 1.6;">
            <strong>File:</strong> {pdf_filename}<br>
            <strong>Size:</strong> {len(pdf_bytes) / 1024:.1f} KB
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="color: #999; font-size: 12px;">
            Sent by <a href="{settings.public_app_url}" style="color: #0066cc;">Back as PDF</a>
            — Forward emails to <strong>{settings.inbound_email}</strong> to convert them.
        </p>
    </div>
    """
    
    # Prepare attachment
    pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
    
    payload = {
        "from": f"{settings.from_name} <{settings.from_email}>",
        "to": [to_email],
        "subject": f"PDF: {subject}" if subject else "Your PDF",
        "html": html_body,
        "attachments": [
            {
                "filename": pdf_filename,
                "content": pdf_base64,
                "content_type": "application/pdf",
            }
        ],
    }
    
    return await _send_email(payload)


async def send_pdf_reply_multi(
    to_email: str,
    subject: str,
    attachments: list[tuple[str, bytes]],
) -> bool:
    """
    Send multiple PDF attachments in one email (used for split).

    Args:
        attachments: List of (filename, pdf_bytes) tuples.
    """
    total_size = sum(len(b) for _, b in attachments)
    html_body = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #333;">Your PDFs are ready! 📄</h2>
        <p style="color: #666; line-height: 1.6;">
            We've split your PDF into <strong>{len(attachments)} pages</strong> and attached them to this message.
        </p>
        <p style="color: #666; line-height: 1.6;">
            <strong>Total size:</strong> {total_size / 1024:.1f} KB
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="color: #999; font-size: 12px;">
            Sent by <a href="{settings.public_app_url}" style="color: #0066cc;">Back as PDF</a>
        </p>
    </div>
    """

    payload = {
        "from": f"{settings.from_name} <{settings.from_email}>",
        "to": [to_email],
        "subject": f"PDF: {subject}" if subject else "Your PDFs",
        "html": html_body,
        "attachments": [
            {
                "filename": filename,
                "content": base64.b64encode(pdf_bytes).decode("utf-8"),
                "content_type": "application/pdf",
            }
            for filename, pdf_bytes in attachments
        ],
    }

    return await _send_email(payload)


async def send_upgrade_email(to_email: str, used: int, limit: int) -> bool:
    """
    Send upgrade prompt when free user hits limit.
    
    Sent once per month when user reaches their free quota.
    """
    html_body = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #333;">You've hit your monthly limit 📊</h2>
        <p style="color: #666; line-height: 1.6;">
            You've used <strong>{used} of {limit}</strong> free conversions this month.
        </p>
        <p style="color: #666; line-height: 1.6;">
            Upgrade to <strong>Pro</strong> for unlimited conversions:
        </p>
        <div style="margin: 24px 0;">
            <a href="{settings.public_app_url}/subscribe" 
               style="display: inline-block; background: #0066cc; color: white; 
                      padding: 12px 24px; text-decoration: none; border-radius: 6px;">
                Upgrade to Pro
            </a>
        </div>
        <p style="color: #999; font-size: 14px;">
            Your limit resets at the start of next month.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="color: #999; font-size: 12px;">
            <a href="{settings.public_app_url}" style="color: #0066cc;">Back as PDF</a>
        </p>
    </div>
    """
    
    payload = {
        "from": f"{settings.from_name} <{settings.notify_email}>",
        "to": [to_email],
        "subject": "You've reached your free limit — Upgrade to Pro",
        "html": html_body,
    }
    
    return await _send_email(payload)


async def send_fair_use_email(to_email: str, used: int, soft_limit: int) -> bool:
    """
    Send fair use notice to Pro user approaching hard limit.
    
    Friendly heads-up, not a hard block.
    """
    html_body = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #333;">High usage notice 📈</h2>
        <p style="color: #666; line-height: 1.6;">
            You've converted <strong>{used}</strong> emails this month — 
            that's a lot of PDFs! 🎉
        </p>
        <p style="color: #666; line-height: 1.6;">
            Our fair use policy is around {soft_limit} conversions per month.
            If you regularly need more, let us know — we'd love to hear about
            your use case.
        </p>
        <p style="color: #666; line-height: 1.6;">
            Just reply to this email if you have any questions.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="color: #999; font-size: 12px;">
            <a href="{settings.public_app_url}" style="color: #0066cc;">Back as PDF</a>
        </p>
    </div>
    """
    
    payload = {
        "from": f"{settings.from_name} <{settings.notify_email}>",
        "to": [to_email],
        "subject": "High usage notice — Back as PDF",
        "html": html_body,
    }
    
    return await _send_email(payload)


async def send_contact_email(name: str, email: str, message: str) -> bool:
    """
    Forward a contact form submission to the team inbox.
    """
    payload = {
        "from": f"{settings.from_name} <noreply@backaspdf.com>",
        "to": ["flavian.haechler@aquilas.com"],
        "reply_to": email,
        "subject": f"[Contact] {name}",
        "html": (
            f"<p><strong>From:</strong> {name} ({email})</p>"
            f"<p><strong>Message:</strong></p>"
            f"<p>{message}</p>"
        ),
    }

    return await _send_email(payload)


async def send_rejection_email(to_email: str, reason: str) -> bool:
    """
    Send rejection notice when conversion is blocked.
    
    Used for hard limit violations or other blocks.
    """
    html_body = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #333;">Conversion not processed ⚠️</h2>
        <p style="color: #666; line-height: 1.6;">
            We couldn't process your email conversion.
        </p>
        <p style="color: #666; line-height: 1.6;">
            <strong>Reason:</strong> {reason}
        </p>
        <p style="color: #666; line-height: 1.6;">
            Questions? Just reply to this email.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="color: #999; font-size: 12px;">
            <a href="{settings.public_app_url}" style="color: #0066cc;">Back as PDF</a>
        </p>
    </div>
    """
    
    payload = {
        "from": f"{settings.from_name} <{settings.notify_email}>",
        "to": [to_email],
        "subject": "Conversion not processed — Back as PDF",
        "html": html_body,
    }
    
    return await _send_email(payload)


async def _send_email(payload: dict) -> bool:
    """Send email via Resend API."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Email sent: {data.get('id')} to {payload['to']}")
                return True
            else:
                logger.error(f"Resend error: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        logger.error(f"Email send error: {e}")
        return False
