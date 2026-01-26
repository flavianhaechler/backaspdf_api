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
        # Always check if browser is still usable
        if _browser is not None:
            try:
                if not _browser.is_connected():
                    _browser = None
            except:
                _browser = None
        
        if _browser is None:
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
    _retry: bool = True,
) -> bytes:
    """Render HTML to PDF."""
    if options is None:
        options = PDFOptions()
    
    try:
        browser = await get_browser()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            device_scale_factor=2,
        )
        
        context.set_default_timeout(settings.max_render_time_seconds * 1000)
        page = await context.new_page()
        await page.set_content(html, wait_until="networkidle")
        await asyncio.sleep(0.5)
        
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
        
        await context.close()
        logger.info(f"PDF rendered: {len(pdf_bytes)} bytes")
        return pdf_bytes
        
    except Exception as e:
        # Browser crashed - reset and retry once
        global _browser
        _browser = None
        
        if _retry:
            logger.warning(f"Browser error, retrying: {e}")
            return await render_pdf(html, options, _retry=False)
        
        logger.error(f"PDF render error: {e}")
        raise RuntimeError(f"PDF rendering failed: {e}")


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
