"""Deterministic firmographic signals, for ICP scoring.

This is the honest answer to "give us structured fields for enrichment". Every
value here is *read off the page*: a script host, a JSON-LD property, a meta
tag, a number next to the word "employees". Nothing is inferred, nothing is
generated, and a signal that is not present is simply absent.

The alternative -- handing a model a schema and asking it for "pricing signals"
-- would return pricing signals whether or not the site has any. In a scoring
pipeline that failure is both expensive and invisible, which is exactly why it
is not what this module does.

Signals are cheap: one pass over the homepage markup, no extra fetches.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

#: host fragment -> technology name. Matched against every script/link/img host
#: and against inline script text, so a self-hosted embed is still recognised.
_TECH_FINGERPRINTS: tuple[tuple[str, str], ...] = (
    ("webflow.com", "Webflow"),
    ("wp-content", "WordPress"),
    ("wp-includes", "WordPress"),
    ("wpengine", "WP Engine"),
    ("squarespace.com", "Squarespace"),
    ("shopify.com", "Shopify"),
    ("shopifycdn", "Shopify"),
    ("wix.com", "Wix"),
    ("wixstatic", "Wix"),
    ("framerusercontent", "Framer"),
    ("framer.com", "Framer"),
    ("_next/static", "Next.js"),
    ("nuxt", "Nuxt"),
    ("hubspot.com", "HubSpot"),
    ("hs-scripts", "HubSpot"),
    ("hs-analytics", "HubSpot"),
    ("marketo", "Marketo"),
    ("pardot", "Pardot"),
    ("salesforce.com", "Salesforce"),
    ("force.com", "Salesforce"),
    ("segment.com", "Segment"),
    ("segment.io", "Segment"),
    ("googletagmanager", "Google Tag Manager"),
    ("google-analytics", "Google Analytics"),
    ("intercom", "Intercom"),
    ("drift.com", "Drift"),
    ("calendly.com", "Calendly"),
    ("hotjar", "Hotjar"),
    ("clarity.ms", "Microsoft Clarity"),
    ("typeform", "Typeform"),
    ("cal.com", "Cal.com"),
    ("chilipiper", "Chili Piper"),
    ("zendesk", "Zendesk"),
    ("livechatinc", "LiveChat"),
    ("klaviyo", "Klaviyo"),
    ("mailchimp", "Mailchimp"),
    ("stripe.com", "Stripe"),
    ("vercel", "Vercel"),
    ("netlify", "Netlify"),
    ("cloudflareinsights", "Cloudflare"),
)

#: "a team of 40", "40+ employees", "we are 12 people"
_SIZE_PATTERNS = (
    re.compile(r"\bteam of (?:over |more than |about |around |nearly )?(\d{1,5})\b", re.I),
    re.compile(r"\b(\d{1,5})\s*\+?\s*(?:employees|staff|people|specialists|engineers)\b", re.I),
    re.compile(r"\b(?:over|more than)\s+(\d{1,5})\s+(?:employees|staff|people)\b", re.I),
)

_FOUNDED_PATTERNS = (
    re.compile(r"\b(?:founded|established|est\.?|since)\s*(?:in\s*)?(19\d{2}|20\d{2})\b", re.I),
    re.compile(r"\bserving [^.]{0,40}since (19\d{2}|20\d{2})\b", re.I),
)

_MAX_PER_LIST = 12


def _hosts_and_srcs(tree: LexborHTMLParser) -> list[str]:
    found: list[str] = []
    for node in tree.css("script[src], link[href], img[src], iframe[src]"):
        value = node.attributes.get("src") or node.attributes.get("href") or ""
        if value:
            found.append(value.lower())
    for node in tree.css("script:not([src])"):
        text = node.text() or ""
        if text:
            found.append(text[:4000].lower())
    return found


def detect_technologies(html: str) -> list[str]:
    """Which platforms and tools the page loads.

    Useful for ICP scoring in both directions: "runs HubSpot" says something
    about budget and maturity, "runs Wix" says something else.
    """
    if not html:
        return []
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # pragma: no cover - selectolax is defensive
        return []

    haystack = " ".join(_hosts_and_srcs(tree))
    generator = ""
    node = tree.css_first('meta[name="generator"]')
    if node is not None:
        generator = (node.attributes.get("content") or "").lower()

    found: list[str] = []
    for fragment, name in _TECH_FINGERPRINTS:
        if name in found:
            continue
        # The generator meta names the platform outright ("WordPress 6.4"),
        # so match on the product name there as well as the asset-host
        # fragment, which is what a self-hosted install would show instead.
        if fragment in haystack or fragment in generator or name.lower() in generator:
            found.append(name)
    return found[:_MAX_PER_LIST]


def detect_keywords(html: str) -> list[str]:
    """The company's own meta keywords, deduplicated. Not inferred."""
    if not html:
        return []
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # pragma: no cover
        return []
    node = tree.css_first('meta[name="keywords"]')
    if node is None:
        return []
    raw = node.attributes.get("content") or ""
    seen: list[str] = []
    for part in raw.split(","):
        word = " ".join(part.split())[:60]
        if word and word.lower() not in {w.lower() for w in seen}:
            seen.append(word)
    return seen[:_MAX_PER_LIST]


def detect_employee_count(text: str) -> int | None:
    """A headcount the company states about itself, or nothing."""
    if not text:
        return None
    for pattern in _SIZE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                value = int(match.group(1))
            except ValueError:  # pragma: no cover - regex guarantees digits
                continue
            # A four-figure "team of 2024" is a year, not a headcount.
            if 1 <= value <= 500_000 and not (1900 <= value <= 2100):
                return value
    return None


def detect_founded_year(text: str) -> int | None:
    if not text:
        return None
    for pattern in _FOUNDED_PATTERNS:
        match = pattern.search(text)
        if match:
            year = int(match.group(1))
            if 1800 <= year <= 2100:
                return year
    return None
