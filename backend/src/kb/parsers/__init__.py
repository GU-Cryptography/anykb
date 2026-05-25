"""Document parsers — bytes/URL → clean text.

One module per supported source. All return plain str (UTF-8) with paragraph
boundaries preserved as double newlines (so chunker can split cleanly).

Dispatch by file extension is in this package's `dispatch()`.
"""
from __future__ import annotations

from .docx import parse_docx
from .markdown import parse_markdown
from .pdf import parse_pdf
from .webpage import parse_url

__all__ = ["dispatch", "parse_markdown", "parse_pdf", "parse_docx", "parse_url"]


SUPPORTED_EXTS = {"md", "markdown", "txt", "pdf", "docx"}


def dispatch(filename: str, content: bytes) -> tuple[str, str]:
    """Pick the right parser by file extension.

    Returns (mime, text). `mime` is a coarse label we store on the Document row
    (not the raw HTTP Content-Type — that's caller's responsibility).
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("md", "markdown"):
        return "text/markdown", parse_markdown(content)
    if ext == "txt":
        return "text/plain", parse_markdown(content)  # txt uses same code path
    if ext == "pdf":
        return "application/pdf", parse_pdf(content)
    if ext == "docx":
        return (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            parse_docx(content),
        )
    raise ValueError(
        f"Unsupported file extension '.{ext}'. Supported: {sorted(SUPPORTED_EXTS)}"
    )
