"""PDF parser — PyMuPDF (fitz).

PyMuPDF is fastest of the Python options and gets text layout right for most
born-digital PDFs. For scanned PDFs (image-only) you'd add OCR; out of scope
for v1.
"""
from __future__ import annotations

import pymupdf


def parse_pdf(content: bytes) -> str:
    """Extract concatenated page text from PDF bytes.

    Pages are separated by a double newline so the chunker can treat them as
    natural paragraph boundaries.
    """
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        pages = []
        for page in doc:
            text = page.get_text("text") or ""  # type: ignore[attr-defined]
            text = text.strip()
            if text:
                pages.append(text)
        return "\n\n".join(pages)
    finally:
        doc.close()
