"""Main-content extraction.

Architecture reference: SS8, "Per-page extraction".

trafilatura is the primary extractor.  It is synchronous and CPU-bound, so it
runs in a worker thread -- a blocked event loop under a 32-second deadline would
starve every concurrent fetch.

Nothing in this module summarizes, rewrites or interprets the text (invariant 6).
Extraction selects and cleans; it never generates.
"""

from __future__ import annotations

import re

import anyio
import structlog
import trafilatura
from selectolax.lexbor import LexborHTMLParser

from app.fetch.sufficiency import count_words

log = structlog.get_logger(__name__)

FALLBACK_WORD_THRESHOLD = 80

_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "button",
    "select",
    "video",
    "audio",
    "canvas",
)

#: Common chrome selectors that survive tag-level pruning.
_CHROME_SELECTORS = (
    "[role=navigation]",
    "[role=banner]",
    "[role=contentinfo]",
    "[role=search]",
    "[aria-hidden=true]",
    "[hidden]",
    ".cookie",
    ".cookies",
    ".cookie-banner",
    ".cookie-consent",
    ".gdpr",
    ".breadcrumb",
    ".breadcrumbs",
    ".skip-link",
    ".screen-reader-text",
    ".sr-only",
    ".visually-hidden",
    "#cookie-banner",
    "#onetrust-consent-sdk",
    "#CybotCookiebotDialog",
)

_MAIN_SELECTORS = ("main", "[role=main]", "article", "#main", "#content", ".main-content")

_ZERO_WIDTH = re.compile(r"[​-‏  ﻿­]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+(\n|$)")
_PUNCT_ONLY = re.compile(r"^[\W_]{1,3}$")


async def extract_content(html: str, url: str) -> str:
    """Extract readable main content as markdown-ish text.

    Falls back to a DOM heuristic when trafilatura under-extracts, which it can
    do on short marketing pages -- it is tuned for articles.
    """
    if not html or not html.strip():
        return ""

    primary = await anyio.to_thread.run_sync(_trafilatura_extract, html, url)
    if primary and count_words(primary) >= FALLBACK_WORD_THRESHOLD:
        return normalize_text(primary)

    fallback = _dom_extract(html)
    if count_words(fallback) > count_words(primary or ""):
        return normalize_text(fallback)
    return normalize_text(primary or fallback)


def _trafilatura_extract(html: str, url: str) -> str:
    try:
        return (
            trafilatura.extract(
                html,
                url=url,
                output_format="markdown",
                include_comments=False,
                include_tables=True,
                include_links=False,
                include_images=False,
                include_formatting=True,
                favor_precision=False,
                favor_recall=True,
                no_fallback=False,
                with_metadata=False,
            )
            or ""
        )
    except Exception as exc:  # pragma: no cover - trafilatura is defensive already
        log.debug("trafilatura.failed", url=url, error=str(exc))
        return ""


def _dom_extract(html: str) -> str:
    """Heuristic extraction: prune chrome, prefer <main>, else densest block."""
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # pragma: no cover
        return ""

    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    for selector in _CHROME_SELECTORS:
        try:
            for node in tree.css(selector):
                node.decompose()
        except Exception:  # pragma: no cover - invalid selector for this doc
            continue

    container = None
    for selector in _MAIN_SELECTORS:
        container = tree.css_first(selector)
        if container is not None:
            break
    if container is None:
        container = _densest_block(tree)
    if container is None:
        container = tree.body or tree.root
    if container is None:
        return ""

    return _structured_text(container)


def _densest_block(tree: LexborHTMLParser):
    best = None
    best_length = 0
    for node in tree.css("div, section"):
        text = node.text(separator=" ", strip=True) or ""
        if len(text) > best_length:
            best_length = len(text)
            best = node
    return best if best_length > 200 else None


def _structured_text(container) -> str:
    """Serialise a subtree, preserving heading structure.

    Headings carry meaning: "Industries We Serve" is a signal, not decoration,
    and a flat text dump destroys the distinction between a heading and a
    sentence.
    """
    parts: list[str] = []
    for node in container.css(
        "h1, h2, h3, h4, h5, h6, p, li, blockquote, dt, dd, td, th, figcaption"
    ):
        text = (node.text(separator=" ", strip=True) or "").strip()
        if not text:
            continue
        tag = (node.tag or "").lower()
        if tag in ("h1", "h2"):
            parts.append(f"## {text}")
        elif tag in ("h3", "h4", "h5", "h6"):
            parts.append(f"### {text}")
        elif tag == "li":
            parts.append(f"- {text}")
        else:
            parts.append(text)

    if not parts:
        return container.text(separator="\n", strip=True) or ""

    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(part)
    return "\n\n".join(deduped)


def normalize_text(text: str) -> str:
    """Whitespace and control-character normalisation (SS8, step 5)."""
    if not text:
        return ""
    cleaned = _ZERO_WIDTH.sub("", text)
    cleaned = _CONTROL.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _TRAILING_WS.sub(r"\1", cleaned)

    lines = []
    for line in cleaned.split("\n"):
        stripped = line.strip()
        if _PUNCT_ONLY.match(stripped):
            continue
        lines.append(stripped)
    cleaned = "\n".join(lines)
    cleaned = _MANY_NEWLINES.sub("\n\n", cleaned)
    return cleaned.strip()


def split_blocks(text: str) -> list[str]:
    """Split extracted text into comparable blocks for boilerplate detection."""
    if not text:
        return []
    blocks: list[str] = []
    for chunk in text.split("\n"):
        stripped = chunk.strip()
        if stripped:
            blocks.append(stripped)
    return blocks


def truncate_at_paragraph(text: str, limit: int) -> tuple[str, bool]:
    """Truncate to ``limit`` characters at a paragraph boundary where possible."""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False

    window = text[:limit]
    for separator in ("\n\n", "\n", ". "):
        index = window.rfind(separator)
        if index > limit * 0.5:
            return window[:index].rstrip(), True
    return window.rstrip(), True
