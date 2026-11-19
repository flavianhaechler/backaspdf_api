"""
PDF rendering - convert HTML to PDF using Playwright.

This is a pure function: HTML in, PDF bytes out.
Used by both consumer flow and developer API.
"""
import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Browser, BrowserContext, Playwright, Route

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_ALLOWED_RENDER_RESOURCE_TYPES = {"image", "stylesheet", "font"}

_RENDER_TIMEOUT_GRACE_SECONDS = 15


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


@dataclass
class _BrowserSession:
    playwright_manager: Optional[object] = None
    playwright: Optional[Playwright] = None
    browser: Optional[Browser] = None


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


async def _start_browser(session: _BrowserSession) -> None:
    logger.info("Starting Playwright browser")
    session.playwright_manager = async_playwright()
    session.playwright = await session.playwright_manager.start()
    session.browser = await session.playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--no-sandbox",
        ],
    )
    logger.info("Playwright browser started")


async def _close_browser_session(session: _BrowserSession) -> None:
    try:
        if session.browser:
            await session.browser.close()
    finally:
        if session.playwright:
            await session.playwright.stop()


def _force_kill_playwright_driver(session: _BrowserSession) -> bool:
    manager = session.playwright_manager
    connection = getattr(manager, "_connection", None)
    transport = getattr(connection, "_transport", None)
    process = getattr(transport, "_proc", None)
    if process is None or process.returncode is not None:
        return False
    process.kill()
    return True


async def _terminate_browser_session(session: _BrowserSession) -> None:
    if session.browser:
        try:
            await asyncio.wait_for(session.browser.close(), timeout=1)
        except asyncio.TimeoutError:
            logger.warning("Browser did not close after render timeout")
        except Exception as error:
            logger.warning("Browser close after render timeout failed (%s)", type(error).__name__)

    if not _force_kill_playwright_driver(session):
        logger.error("Could not force-stop Playwright driver after render timeout")


def _render_timeout_seconds() -> float:
    return settings.max_render_time_seconds + _RENDER_TIMEOUT_GRACE_SECONDS


async def _render_once(render: Callable[[Browser], Awaitable[bytes]]) -> bytes:
    session = _BrowserSession()

    async def render_and_close() -> bytes:
        try:
            await _start_browser(session)
            assert session.browser is not None
            return await render(session.browser)
        finally:
            await _close_browser_session(session)

    render_task = asyncio.create_task(render_and_close())
    timeout_seconds = _render_timeout_seconds()
    done, _ = await asyncio.wait({render_task}, timeout=timeout_seconds)
    if render_task not in done:
        await _terminate_browser_session(session)
        render_task.cancel()
        cleanup_done, _ = await asyncio.wait({render_task}, timeout=1)
        if cleanup_done:
            try:
                render_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as error:
                logger.warning("Render cleanup after timeout failed (%s)", type(error).__name__)
        else:
            logger.error("Playwright cleanup did not finish after forced termination")
        raise asyncio.TimeoutError("PDF rendering exceeded its time limit")

    return render_task.result()


async def _render_with_retry(render: Callable[[Browser], Awaitable[bytes]]) -> bytes:
    for attempt in range(2):
        try:
            return await _render_once(render)
        except Exception as error:
            if attempt == 0:
                logger.warning("Browser error, retrying (%s)", type(error).__name__)
            else:
                logger.error("PDF render failed (%s)", type(error).__name__)
                raise RuntimeError("PDF rendering failed") from error
    raise RuntimeError("PDF rendering failed")


async def close_browser():
    """Keep the shutdown hook harmless; render sessions close their own browsers."""
    logger.info("No shared Playwright browser to close")


async def render_pdf(
    html: str,
    options: Optional[PDFOptions] = None,
) -> bytes:
    """Render HTML to PDF."""
    if options is None:
        options = PDFOptions()

    async def render(browser: Browser) -> bytes:
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            device_scale_factor=2,
            java_script_enabled=False,
        )
        try:
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
            logger.info("PDF rendered: %s bytes", len(pdf_bytes))
            return pdf_bytes
        finally:
            await context.close()

    return await _render_with_retry(render)


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

    async def render(browser: Browser) -> bytes:
        context: Optional[BrowserContext] = None
        try:
            context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            device_scale_factor=2,
            java_script_enabled=False,
            )
            context.set_default_timeout(settings.max_render_time_seconds * 1000)

            page = await context.new_page()
            await page.goto(url, wait_until="networkidle")

            if wait_for_selector:
                await page.wait_for_selector(wait_for_selector)

            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)

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

            logger.info("URL PDF rendered: %s bytes", len(pdf_bytes))
            return pdf_bytes
        finally:
            if context:
                await context.close()

    return await _render_with_retry(render)
