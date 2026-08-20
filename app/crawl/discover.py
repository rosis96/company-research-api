"""Internal link discovery.

Architecture reference: SS6.  Four sources, cheapest first: homepage anchors,
robots.txt sitemap declarations, ``/sitemap.xml``, and wave-1 hub outlinks.

Anchors carry their DOM region, because a link in the primary navigation is a
page the company considers important -- one of the strongest ranking signals we
get for free.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree

from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.crawl.normalize import evaluate_exclusions, normalize_url
from app.models.internal import Candidate, LinkRegion
from app.security.domains import registrable_domain

_REGION_TAGS = {
    "nav": LinkRegion.NAV,
    "header": LinkRegion.HEADER,
    "footer": LinkRegion.FOOTER,
    "main": LinkRegion.MAIN,
    "article": LinkRegion.MAIN,
}
_REGION_ROLE = {
    "navigation": LinkRegion.NAV,
    "banner": LinkRegion.HEADER,
    "contentinfo": LinkRegion.FOOTER,
    "main": LinkRegion.MAIN,
}
_CLASS_HINTS = (
    ("footer", LinkRegion.FOOTER),
    ("site-footer", LinkRegion.FOOTER),
    ("nav", LinkRegion.NAV),
    ("menu", LinkRegion.NAV),
    ("header", LinkRegion.HEADER),
    ("masthead", LinkRegion.HEADER),
)
_SITEMAP_NS = re.compile(r"\{[^}]+\}")
_WS = re.compile(r"\s+")


def _region_of(node: LexborNode) -> LinkRegion:
    """Walk up the DOM to classify where a link lives."""
    current = node.parent
    depth = 0
    while current is not None and depth < 25:
        tag = (current.tag or "").lower()
        if tag in _REGION_TAGS:
            return _REGION_TAGS[tag]

        role = (current.attributes.get("role") or "").lower()
        if role in _REGION_ROLE:
            return _REGION_ROLE[role]

        identifiers = " ".join(
            filter(
                None,
                (
                    current.attributes.get("class") or "",
                    current.attributes.get("id") or "",
                ),
            )
        ).lower()
        if identifiers:
            for hint, region in _CLASS_HINTS:
                if hint in identifiers:
                    return region

        current = current.parent
        depth += 1
    return LinkRegion.BODY


def extract_links(
    html: str,
    base_url: str,
    *,
    scope_domain: str,
    depth: int,
    discovered_via: str,
) -> list[Candidate]:
    """Extract in-scope, non-excluded links from a page.

    Off-domain links are dropped here rather than at fetch time, so third-party
    URLs never even enter the candidate pool (invariant 4).
    """
    if not html:
        return []
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # pragma: no cover - selectolax is tolerant
        return []

    merged: dict[str, Candidate] = {}
    for node in tree.css("a[href]"):
        href = node.attributes.get("href")
        if not href:
            continue

        normalized = normalize_url(href, base=base_url)
        if normalized is None:
            continue
        if normalized == base_url:
            continue

        host = _host_of(normalized)
        if not host or registrable_domain(host) != scope_domain:
            continue
        if evaluate_exclusions(normalized).excluded:
            continue

        anchor = _WS.sub(" ", node.text(deep=True, strip=True) or "").strip()
        if not anchor:
            anchor = (node.attributes.get("title") or node.attributes.get("aria-label") or "").strip()

        candidate = merged.get(normalized)
        if candidate is None:
            candidate = Candidate(
                url=normalized,
                depth=depth,
                discovered_via=discovered_via,
                inbound_links=1,
            )
            merged[normalized] = candidate
        else:
            candidate.inbound_links += 1

        if anchor and anchor not in candidate.anchor_texts:
            candidate.anchor_texts.append(anchor[:200])
        candidate.regions.add(_region_of(node))

    return list(merged.values())


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


def parse_sitemap(xml_text: str, cap: int) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Parse a sitemap or sitemap index.

    Returns ``(page_urls_with_lastmod, nested_sitemap_urls)``.  Malformed XML
    yields empty lists rather than raising -- a broken sitemap must never fail a
    crawl.
    """
    if not xml_text or not xml_text.strip():
        return [], []
    try:
        root = ElementTree.fromstring(xml_text.strip())
    except ElementTree.ParseError:
        return [], []

    tag = _SITEMAP_NS.sub("", root.tag).lower()
    pages: list[tuple[str, str | None]] = []
    nested: list[str] = []

    for child in root:
        child_tag = _SITEMAP_NS.sub("", child.tag).lower()
        loc = None
        lastmod = None
        for element in child:
            element_tag = _SITEMAP_NS.sub("", element.tag).lower()
            if element_tag == "loc":
                loc = (element.text or "").strip()
            elif element_tag == "lastmod":
                lastmod = (element.text or "").strip()
        if not loc:
            continue
        if tag == "sitemapindex" or child_tag == "sitemap":
            nested.append(loc)
        else:
            pages.append((loc, lastmod))
        if len(pages) >= cap:
            break

    return pages, nested


def candidates_from_sitemap(
    entries: list[tuple[str, str | None]],
    *,
    scope_domain: str,
    base_url: str,
) -> list[Candidate]:
    """Convert sitemap entries into scoped, deduplicated candidates."""
    merged: dict[str, Candidate] = {}
    for loc, lastmod in entries:
        normalized = normalize_url(loc, base=base_url)
        if normalized is None:
            continue
        host = _host_of(normalized)
        if not host or registrable_domain(host) != scope_domain:
            continue
        if evaluate_exclusions(normalized).excluded:
            continue
        existing = merged.get(normalized)
        if existing is None:
            merged[normalized] = Candidate(
                url=normalized,
                depth=1,
                discovered_via="sitemap",
                in_sitemap=True,
                lastmod=lastmod,
            )
        else:
            existing.in_sitemap = True
            existing.lastmod = existing.lastmod or lastmod
    return list(merged.values())


def merge_candidates(*groups: list[Candidate]) -> dict[str, Candidate]:
    """Fold multiple discovery sources into one candidate pool."""
    pool: dict[str, Candidate] = {}
    for group in groups:
        for candidate in group:
            existing = pool.get(candidate.url)
            if existing is None:
                pool[candidate.url] = candidate
            else:
                existing.merge(candidate)
    return pool
