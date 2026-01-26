"""
PDF rendering - convert HTML to PDF using Playwright.

This is a pure function: HTML in, PDF bytes out.
Used by both consumer flow and developer API.
"""
import asyncio
import logging
from typing import Optional
from dataclasses import dataclass

from playwright.async_api import async_playwright, Browser, BrowserContext

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Browser instance (reused across renders)
_browser: Optional[Browser] = None
_browser_lock = asyncio.Lock()


@dataclass
class PDFOptions:
    """PDF generation options."""
    format: str = "A4"
    landscape: bool = False
    margin_top: str = "20mm"
    margin_bottom: str = "20mm"
    margin_left: str = "20mm"
    margin_right: str = "20mm"
    print_background: bool = True
    scale: float = 1.0


async def get_browser() -> Browser:
    """Get or create browser instance."""
    global _browser
    
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            logger.info("Starting Playwright browser")
            playwright = await async_playwright().start()
            _browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                    "--no-sandbox",
                    "--single-process",
                ]
            )
            logger.info("Playwright browser started")
        
        return _browser


async def close_browser():
    """Close browser instance (call on shutdown)."""
    global _browser
    if _browser:
        await _browser.close()
        _browser = None
        logger.info("Playwright browser closed")


async def render_pdf(
    html: str,
    options: Optional[PDFOptions] = None,
) -> bytes:
    """
    Render HTML to PDF.
    
    Args:
        html: HTML content to render
        options: PDF options (format, margins, etc.)
    
    Returns:
        PDF as bytes
    
    This is the core function used by:
    - Consumer: worker.py renders email HTML
    - Developer API: /v1/convert/html endpoint
    """
    if options is None:
        options = PDFOptions()
    
    browser = await get_browser()
    context: Optional[BrowserContext] = None
    
    try:
        # Create isolated context for this render
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            device_scale_factor=2,
        )
        
        # Set timeout
        context.set_default_timeout(settings.max_render_time_seconds * 1000)
        
        page = await context.new_page()
        
        # Load HTML content
        await page.set_content(html, wait_until="networkidle")
        
        # Wait a bit for any lazy-loaded content
        await asyncio.sleep(0.5)
        
        # Generate PDF
        pdf_bytes = await page.pdf(
            format=options.format,
            landscape=options.landscape,
            margin={
                "top": options.margin_top,
                "bottom": options.margin_bottom,
                "left": options.margin_left,
                "right": options.margin_right,
            },
            print_background=options.print_background,
            scale=options.scale,
        )
        
        logger.info(f"PDF rendered: {len(pdf_bytes)} bytes")
        return pdf_bytes
        
    except asyncio.TimeoutError:
        logger.error("PDF render timeout")
        raise RuntimeError("PDF rendering timed out")
    
    except Exception as e:
        logger.error(f"PDF render error: {e}")
        raise RuntimeError(f"PDF rendering failed: {e}")
    
    finally:
        if context:
            await context.close()


async def render_url_to_pdf(
    url: str,
    options: Optional[PDFOptions] = None,
    wait_for_selector: Optional[str] = None,
    delay_ms: int = 0,
) -> bytes:
    """
    Render URL to PDF (screenshot to PDF).
    
    For Developer API: /v1/convert/url endpoint
    Not used by consumer flow.
    """
    if options is None:
        options = PDFOptions()
    
    browser = await get_browser()
    context: Optional[BrowserContext] = None
    
    try:
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            device_scale_factor=2,
        )
        context.set_default_timeout(settings.max_render_time_seconds * 1000)
        
        page = await context.new_page()
        
        # Navigate to URL
        await page.goto(url, wait_until="networkidle")
        
        # Optional: wait for specific selector
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector)
        
        # Optional: additional delay
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)
        
        # Generate PDF
        pdf_bytes = await page.pdf(
            format=options.format,
            landscape=options.landscape,
            margin={
                "top": options.margin_top,
                "bottom": options.margin_bottom,
                "left": options.margin_left,
                "right": options.margin_right,
            },
            print_background=options.print_background,
            scale=options.scale,
        )
        
        logger.info(f"URL PDF rendered: {len(pdf_bytes)} bytes from {url}")
        return pdf_bytes
        
    finally:
        if context:
            await context.close()
