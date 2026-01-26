"""
PDF rendering - convert HTML to PDF using WeasyPrint.

This is a pure function: HTML in, PDF bytes out.
Used by both consumer flow and developer API.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from weasyprint import HTML, CSS

logger = logging.getLogger(__name__)


@dataclass
class PDFOptions:
    """PDF generation options."""
    format: str = "A4"
    margin: str = "20mm"


async def render_pdf(
    html: str,
    options: Optional[PDFOptions] = None,
) -> bytes:
    """
    Render HTML to PDF.
    
    Args:
        html: HTML content to render
        options: PDF options (format, margins)
    
    Returns:
        PDF as bytes
    """
    if options is None:
        options = PDFOptions()
    
    # Base CSS for consistent rendering
    base_css = CSS(string=f"""
        @page {{
            size: {options.format};
            margin: {options.margin};
        }}
        body {{
            font-family: sans-serif;
            line-height: 1.5;
        }}
        img {{
            max-width: 100%;
        }}
    """)
    
    try:
        pdf_bytes = HTML(string=html).write_pdf(stylesheets=[base_css])
        logger.info(f"PDF rendered: {len(pdf_bytes)} bytes")
        return pdf_bytes
    except Exception as e:
        logger.error(f"PDF render error: {e}")
        raise RuntimeError(f"PDF rendering failed: {e}")


async def close_browser():
    """No-op for WeasyPrint compatibility."""
    pass