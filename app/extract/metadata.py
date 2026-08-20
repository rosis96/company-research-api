"""Structural page metadata extraction.

Architecture reference: SS8, step 3.  Title, description, headings and language
only -- no interpretation, no synthesis (invariant 6).
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

_WS = re.compile(r"\s+")


def _clean(text: str | None, limit: int = 300) -> str | None:
    if not text:
        return None
    collapsed = _WS.sub(" ", text).strip()
    if not collapsed:
        return None
    return collapsed[:limit]


class PageMetadata:
    """Extracted metadata for one page."""

    __slots__ = ("title", "meta_description", "h1", "headings", "language", "canonical")

    def __init__(
        self,
        title: str | None = None,
        meta_description: str | None = None,
        h1: str | None = None,
        headings: list[str] | None = None,
        language: str | None = None,
        canonical: str | None = None,
    ) -> None:
        self.title = title
        self.meta_description = meta_description
        self.h1 = h1
        self.headings = headings or []
        self.language = language
        self.canonical = canonical


def extract_metadata(html: str, *, max_headings: int, max_heading_chars: int) -> PageMetadata:
    """Pull title, description, headings, language and canonical URL.

    ``headings`` is the page's own skeleton of claims (h1 plus h2s), deduped and
    capped, because it is genuinely useful evidence but must not be allowed to
    consume the character budget.
    """
    if not html:
        return PageMetadata()
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # pragma: no cover
        return PageMetadata()

    title_node = tree.css_first("title")
    title = _clean(title_node.text(strip=True) if title_node else None, 300)

    description = None
    for selector in (
        'meta[name="description"]',
        'meta[property="og:description"]',
        'meta[name="twitter:description"]',
    ):
        node = tree.css_first(selector)
        if node is not None:
            description = _clean(node.attributes.get("content"), 400)
            if description:
                break

    h1_node = tree.css_first("h1")
    h1 = _clean(h1_node.text(strip=True) if h1_node else None, max_heading_chars * 3)

    headings: list[str] = []
    seen: set[str] = set()
    for node in tree.css("h1, h2"):
        text = _clean(node.text(strip=True), max_heading_chars)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        headings.append(text)
        if len(headings) >= max_headings:
            break

    language = None
    html_node = tree.css_first("html")
    if html_node is not None:
        language = _clean(html_node.attributes.get("lang"), 12)
    if not language:
        node = tree.css_first('meta[http-equiv="content-language"]')
        if node is not None:
            language = _clean(node.attributes.get("content"), 12)
    if language:
        language = language.split(",")[0].strip().lower() or None

    canonical = None
    canonical_node = tree.css_first('link[rel="canonical"]')
    if canonical_node is not None:
        canonical = _clean(canonical_node.attributes.get("href"), 2048)

    return PageMetadata(
        title=title,
        meta_description=description,
        h1=h1,
        headings=headings,
        language=language,
        canonical=canonical,
    )


def extract_tagline(html: str, metadata: PageMetadata) -> str | None:
    """Best-effort first-party tagline from the homepage.

    Prefers the ``og:site_name``-adjacent hero heading, then h1, then the meta
    description.  This is selection of existing text, never generation.
    """
    if metadata.h1:
        return metadata.h1
    return metadata.meta_description


def extract_site_name(html: str, metadata: PageMetadata) -> str | None:
    if not html:
        return None
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # pragma: no cover
        return None
    node = tree.css_first('meta[property="og:site_name"]')
    if node is not None:
        value = _clean(node.attributes.get("content"), 120)
        if value:
            return value
    if metadata.title:
        # Titles are commonly "Page Name | Company" or "Company - Page Name".
        for separator in ("|", "–", "—", "-", "::"):
            if separator in metadata.title:
                parts = [p.strip() for p in metadata.title.split(separator) if p.strip()]
                if len(parts) >= 2:
                    return parts[-1][:120]
        return metadata.title[:120]
    return None
