"""URL fetcher + main-content extractor — httpx + trafilatura.

trafilatura is purpose-built for "give me the article text" extraction across
arbitrary news/blog/doc sites; it strips chrome, nav, ads, comments.
"""
from __future__ import annotations

import httpx
import trafilatura


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 AnyKB-KB/1.0"
)


async def parse_url(url: str, *, timeout: float = 30.0) -> tuple[str, str]:
    """Fetch URL → extracted (title, text).

    Raises:
        httpx.HTTPError — network / non-2xx
        ValueError      — content extraction returned nothing
    """
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        html = r.text

    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        output_format="txt",
        no_fallback=False,
    )
    if not extracted or not extracted.strip():
        raise ValueError(f"no readable content extracted from URL: {url}")

    title = url
    try:
        meta = trafilatura.extract_metadata(html)
        if meta and meta.title:
            title = meta.title
    except Exception:  # noqa: BLE001
        pass  # metadata is nice-to-have; URL is fine as fallback

    return title, extracted.strip()
