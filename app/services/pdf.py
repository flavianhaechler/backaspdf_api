"""
PDF rendering - convert HTML to PDF using Playwright.

This is a pure function: HTML in, PDF bytes out.
Used by both consumer flow and developer API.
"""
import asyncio
import ipaddress
import logging
import socket
from typing import Optional
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Browser, BrowserContext, Playwright, Route

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_ALLOWED_RENDER_RESOURCE_TYPES = {"image", "stylesheet", "font"}

# Browser instance (reused across renders)
_browser: Optional[Browser] = None
_playwright: Optional[Playwright] = None
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


async def _resolve_host_addresses(host: str, port: int) -> list[tuple]:
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def _is_allowed_render_request(url: str, resource_type: str) -> bool:
    if resource_type not in _ALLOWED_RENDER_RESOURCE_TYPES:
        return False

    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme == "data":
            return True
        if scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False

    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        try:
            resolved = await asyncio.wait_for(
                _resolve_host_addresses(parsed.hostname, port),
                timeout=5,
            )
        except (OSError, UnicodeError, asyncio.TimeoutError):
            return False
        if not resolved:
            return False
        try:
            addresses = [
                ipaddress.ip_address(result[4][0].split("%", 1)[0])
                for result in resolved
            ]
        except (IndexError, ValueError):
            return False

    return all(address.is_global for address in addresses)


async def _route_render_request(route: Route) -> None:
    request = route.request
    if await _is_allowed_render_request(request.url, request.resource_type):
        await route.continue_()
    else:
        await route.abort()


async def get_browser() -> Browser:
    """Get or create browser instance."""
    global _browser, _playwright
    
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
            if _playwright is None:
                _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
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
    global _browser, _playwright
    browser = _browser
    playwright = _playwright
    _browser = None
    _playwright = None

    try:
        if browser:
            await browser.close()
    finally:
        if playwright:
            await playwright.stop()

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
            java_script_enabled=False,
        )
        
        context.set_default_timeout(settings.max_render_time_seconds * 1000)
        await context.route("**/*", _route_render_request)
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
            java_script_enabled=False,
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
