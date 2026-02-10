"""
PDF tools - merge and split PDFs using pypdf.
"""
import logging
from pypdf import PdfReader, PdfWriter
from io import BytesIO

logger = logging.getLogger(__name__)


def merge_pdfs(pdf_list: list[bytes]) -> bytes:
    """
    Merge multiple PDFs into one.

    Args:
        pdf_list: List of PDF files as bytes.

    Returns:
        Merged PDF as bytes.
    """
    writer = PdfWriter()

    for i, pdf_bytes in enumerate(pdf_list):
        reader = PdfReader(BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)
        logger.info(f"Added PDF {i+1}: {len(reader.pages)} pages")

    output = BytesIO()
    writer.write(output)
    result = output.getvalue()
    logger.info(f"Merged PDF: {len(writer.pages)} pages, {len(result)} bytes")
    return result


def split_pdf(pdf_bytes: bytes) -> list[tuple[str, bytes]]:
    """
    Split a PDF into individual pages.

    Args:
        pdf_bytes: PDF file as bytes.

    Returns:
        List of (filename, pdf_bytes) tuples, one per page.
    """
    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []

    for i, page in enumerate(reader.pages):
        writer = PdfWriter()
        writer.add_page(page)
        output = BytesIO()
        writer.write(output)
        page_bytes = output.getvalue()
        filename = f"page_{i+1}.pdf"
        pages.append((filename, page_bytes))

    logger.info(f"Split PDF: {len(pages)} pages")
    return pages
