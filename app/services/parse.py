"""
Email parsing - extract HTML and metadata from inbound emails.
"""
import base64
import logging
import re
from dataclasses import dataclass
from typing import Optional
from email.utils import parseaddr

logger = logging.getLogger(__name__)


@dataclass
class ParsedEmail:
    """Parsed email ready for PDF conversion."""
    message_id: str
    from_email: str
    from_name: Optional[str]
    subject: Optional[str]
    html: str
    text: Optional[str]
    has_attachments: bool


def parse_email(webhook_data: dict) -> ParsedEmail:
    """
    Parse Resend inbound webhook payload.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # DEBUG: Log everything
    logger.info(f"Webhook keys: {webhook_data.keys()}")
    logger.info(f"HTML length: {len(webhook_data.get('html', ''))}")
    logger.info(f"Text length: {len(webhook_data.get('text', ''))}")
    
    attachments = webhook_data.get("attachments", [])
    logger.info(f"Attachments count: {len(attachments)}")
    for i, att in enumerate(attachments):
        logger.info(f"Attachment {i}: filename={att.get('filename')}, content_type={att.get('content_type', att.get('contentType'))}, size={len(att.get('content', ''))}")
    
    # ... rest of function
    from_name, from_email = parseaddr(from_field)
    
    message_id = webhook_data.get("messageId", webhook_data.get("message_id", ""))
    subject = webhook_data.get("subject", "")
    html = webhook_data.get("html", "")
    text = webhook_data.get("text", "")
    attachments = webhook_data.get("attachments", [])
    
    # If no HTML, wrap text in basic HTML
    if not html and text:
        html = text_to_html(text)
    
    # Resolve CID images (inline attachments)
    if attachments and html:
        html = resolve_cid_images(html, attachments)
    
    # Clean and normalize HTML
    html = normalize_html(html, subject, from_email, from_name)
    
    return ParsedEmail(
        message_id=message_id,
        from_email=from_email.lower(),
        from_name=from_name or None,
        subject=subject or None,
        html=html,
        text=text or None,
        has_attachments=bool(attachments),
    )


def text_to_html(text: str) -> str:
    """Convert plain text email to HTML."""
    # Escape HTML entities
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    
    # Convert newlines to <br>
    text = text.replace("\n", "<br>\n")
    
    return f"""
    <html>
    <body style="font-family: monospace; white-space: pre-wrap; padding: 20px;">
    {text}
    </body>
    </html>
    """


def resolve_cid_images(html: str, attachments: list) -> str:
    """
    Replace cid: references with base64 data URIs.
    
    Email clients embed images with Content-ID (CID) references.
    For PDF rendering, we convert these to inline base64.
    """
    for attachment in attachments:
        content_id = attachment.get("contentId", attachment.get("content_id", ""))
        content_type = attachment.get("contentType", attachment.get("content_type", ""))
        content = attachment.get("content", "")
        
        if not content_id or not content:
            continue
        
        # Remove angle brackets from CID
        cid = content_id.strip("<>")
        
        # Check if content is already base64
        try:
            base64.b64decode(content)
            b64_content = content
        except:
            b64_content = base64.b64encode(content.encode()).decode()
        
        # Build data URI
        data_uri = f"data:{content_type};base64,{b64_content}"
        
        # Replace all CID references
        html = html.replace(f"cid:{cid}", data_uri)
        html = html.replace(f"CID:{cid}", data_uri)
    
    return html


def normalize_html(html: str, subject: str = None, from_email: str = None, from_name: str = None) -> str:
    """
    Normalize HTML for consistent PDF rendering.
    
    - Adds proper DOCTYPE and charset
    - Adds email header (subject, from)
    - Fixes common issues
    """
    # Check if HTML already has proper structure
    has_html_tag = bool(re.search(r'<html', html, re.IGNORECASE))
    has_head = bool(re.search(r'<head', html, re.IGNORECASE))
    has_body = bool(re.search(r'<body', html, re.IGNORECASE))
    
    # Build email header
    header_html = ""
    if subject or from_email:
        from_display = from_name if from_name else from_email
        header_html = f"""
        <div style="border-bottom: 1px solid #e0e0e0; padding-bottom: 16px; margin-bottom: 20px;">
            <div style="font-size: 18px; font-weight: bold; color: #333; margin-bottom: 8px;">
                {subject or '(No Subject)'}
            </div>
            <div style="font-size: 14px; color: #666;">
                From: {from_display or 'Unknown'}
            </div>
        </div>
        """
    
    # Base styles for consistent rendering
    base_styles = """
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            line-height: 1.5;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
        }
        img {
            max-width: 100%;
            height: auto;
        }
        a {
            color: #0066cc;
        }
        table {
            border-collapse: collapse;
        }
        td, th {
            padding: 8px;
        }
    </style>
    """
    
    if not has_html_tag:
        # Wrap in full HTML structure
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            {base_styles}
        </head>
        <body>
            {header_html}
            {html}
        </body>
        </html>
        """
    elif not has_head:
        # Add head section
        html = html.replace(
            "<html>",
            f"""<html>
            <head>
                <meta charset="UTF-8">
                {base_styles}
            </head>""",
            1
        )
        # Add header after body tag
        if has_body:
            html = re.sub(
                r'(<body[^>]*>)',
                f'\\1{header_html}',
                html,
                count=1,
                flags=re.IGNORECASE
            )
    else:
        # Just inject header after body
        if has_body:
            html = re.sub(
                r'(<body[^>]*>)',
                f'\\1{header_html}',
                html,
                count=1,
                flags=re.IGNORECASE
            )
    
    return html
