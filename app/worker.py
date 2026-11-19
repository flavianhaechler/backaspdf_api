"""
BackasPDF Worker - processes email-to-PDF jobs.

Run with: python -m app.worker
"""
import asyncio
import logging
import re
import signal
import sys
from datetime import datetime

from app.config import get_settings
from app import db
from app.services import (
    parse_email,
    render_pdf,
    send_pdf_reply,
    send_pdf_reply_multi,
    send_upgrade_email,
    send_fair_use_email,
    send_rejection_email,
    check_quota,
    QuotaResult,
    record_usage,
)
from app.services.parse import extract_pdf_attachments
from app.services.pdf import close_browser
from app.services.pdf_tools import merge_pdfs, split_pdf
from app.services.receipt import extract_receipt

settings = get_settings()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Shutdown flag
shutdown_requested = False


def handle_shutdown(signum, frame):
    """Handle shutdown signals."""
    global shutdown_requested
    logger.info("Shutdown requested")
    shutdown_requested = True


async def process_job(job: dict) -> bool:
    """
    Process a single conversion job.

    Routes to render, merge, or split based on job_type.
    Returns True if successful.
    """
    import json

    job_id = str(job["id"])
    from_email = job["from_email"]
    job_type = job.get("job_type", "render")
    payload = job["email_payload"]

    # Parse JSON if stored as string
    if isinstance(payload, str):
        payload = json.loads(payload)

    logger.info("Processing job %s type=%s", job_id, job_type)

    # Check if rejected at queue time
    if payload.get("rejected"):
        reason = payload.get("reason", "Quota exceeded")
        await send_rejection_email(from_email, reason)
        await db.update_job_status(job_id, "rejected", "quota_exceeded")
        logger.info("Job %s rejected", job_id)
        return True

    try:
        await db.update_job_status(job_id, "processing")

        if job_type == "merge":
            await _process_merge(job_id, from_email, payload)
        elif job_type == "split":
            await _process_split(job_id, from_email, payload)
        else:
            await _process_render(job_id, from_email, payload)

        await db.update_job_status(job_id, "completed")
        logger.info(f"Job {job_id} completed successfully")
        return True

    except Exception as e:
        error_code = type(e).__name__[:50]
        logger.error("Job %s failed (%s)", job_id, error_code)
        await db.update_job_status(job_id, "failed", error_code)
        return False


async def _process_render(job_id: str, from_email: str, payload: dict):
    """Process a render job: HTML email -> PDF."""
    parsed = parse_email(payload)

    pdf_bytes = await render_pdf(parsed.html)
    logger.info(f"PDF rendered: {len(pdf_bytes)} bytes")

    receipt = extract_receipt(
        subject=parsed.subject,
        from_email=parsed.from_email,
        html=parsed.html,
    )

    filename = generate_filename(subject=parsed.subject)

    new_count = await record_usage(from_email)
    quota_result = await check_quota(from_email)

    await send_pdf_reply(
        to_email=from_email,
        subject=parsed.subject or "(No Subject)",
        pdf_bytes=pdf_bytes,
        pdf_filename=filename,
    )

    if quota_result.action.value == "allow_warn_upgrade":
        await send_upgrade_email(from_email, quota_result.used, quota_result.limit)
    elif quota_result.action.value == "allow_warn_fair_use":
        await send_fair_use_email(from_email, quota_result.used, quota_result.limit)

    await db.save_document(
        owner_email=from_email,
        source_message_id=parsed.message_id,
        subject=parsed.subject,
        from_email=parsed.from_email,
        pdf_filename=filename,
        pdf_size_bytes=len(pdf_bytes),
        merchant_name=receipt.merchant_name if receipt else None,
        amount=receipt.amount if receipt else None,
        currency=receipt.currency if receipt else None,
        category=receipt.category if receipt else None,
    )


async def _process_merge(job_id: str, from_email: str, payload: dict):
    """Process a merge job: multiple PDF attachments -> one merged PDF."""
    email_id = payload.get("email_id", "")
    subject = payload.get("subject", "Merged PDF")

    if not email_id:
        raise ValueError("No email_id in payload, cannot fetch attachments")

    pdfs = await extract_pdf_attachments(email_id)
    if len(pdfs) < 2:
        raise ValueError(f"Need at least 2 PDF attachments to merge, got {len(pdfs)}")

    merged = merge_pdfs(pdfs)
    filename = generate_filename(subject=subject)

    await record_usage(from_email)

    await send_pdf_reply(
        to_email=from_email,
        subject=subject,
        pdf_bytes=merged,
        pdf_filename=filename,
    )


async def _process_split(job_id: str, from_email: str, payload: dict):
    """Process a split job: one PDF attachment -> individual pages."""
    email_id = payload.get("email_id", "")
    subject = payload.get("subject", "Split PDF")

    if not email_id:
        raise ValueError("No email_id in payload, cannot fetch attachments")

    pdfs = await extract_pdf_attachments(email_id)
    if len(pdfs) == 0:
        raise ValueError("No PDF attachments found to split")

    # Split the first PDF attachment
    pages = split_pdf(pdfs[0])

    await record_usage(from_email)

    await send_pdf_reply_multi(
        to_email=from_email,
        subject=subject,
        attachments=pages,
    )


def generate_filename(
    subject: str = None,
    merchant: str = None,
    amount: float = None,
    currency: str = None,
) -> str:
    """
    Generate PDF filename.
    
    Format: YYYY-MM-DD_Merchant_AmountCUR.pdf
    Fallback: YYYY-MM-DD_Subject.pdf
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    # If we have receipt data, use structured name
    if merchant:
        name = merchant.replace(" ", "-")
        if amount and currency:
            name = f"{name}_{amount:.0f}{currency}"
        return f"{date_str}_{name}.pdf"
    
    # Fallback to subject
    if subject:
        # Clean subject for filename
        clean = re.sub(r'[^\w\s-]', '', subject)
        clean = re.sub(r'\s+', '-', clean.strip())
        clean = clean[:50]  # Limit length
        return f"{date_str}_{clean}.pdf"
    
    # Default
    return f"{date_str}_document.pdf"


async def worker_loop():
    """Main worker loop."""
    logger.info("Worker started")
    
    while not shutdown_requested:
        try:
            # Get pending jobs
            jobs = await db.get_pending_jobs(limit=settings.worker_batch_size)
            
            if not jobs:
                # No jobs, sleep
                await asyncio.sleep(settings.worker_poll_interval)
                continue
            
            # Process jobs
            for job in jobs:
                if shutdown_requested:
                    break
                await process_job(job)
                
        except Exception as e:
            logger.error("Worker loop error (%s)", type(e).__name__)
            await asyncio.sleep(5)  # Back off on error
    
    logger.info("Worker stopped")


async def main():
    """Worker entry point."""
    # Setup signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    
    # Initialize database
    await db.init_db()
    
    try:
        await worker_loop()
    finally:
        await close_browser()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
