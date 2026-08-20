"""The content sufficiency test.

Architecture reference: SS4, "The content sufficiency test".

This is the decision function for the whole fetch tier.  A JavaScript SPA
returns a perfectly valid 200 with an empty ``<div id="root">``, so "did we get
a 200" is the wrong question -- the question is "did we get text".
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from app.models.internal import SufficiencyVerdict

#: Elements removed before counting visible words.
_CHROME_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "nav",
    "header",
    "footer",
    "iframe",
    "form",
)

#: Bot-challenge and interstitial signatures (SS4, condition 4).
_CHALLENGE_PATTERNS = (
    re.compile(r"checking your browser", re.I),
    re.compile(r"just a moment\.\.\.", re.I),
    re.compile(r"enable javascript and cookies to continue", re.I),
    re.compile(r"please enable (?:javascript|js) to", re.I),
    re.compile(r"attention required!\s*\|\s*cloudflare", re.I),
    re.compile(r"ddos protection by", re.I),
    re.compile(r"access denied", re.I),
    re.compile(r"you (?:have been|are) blocked", re.I),
    re.compile(r"request unsuccessful.*incapsula", re.I | re.S),
    re.compile(r"<title>\s*403 forbidden\s*</title>", re.I),
    re.compile(r"verify you are (?:a )?human", re.I),
    re.compile(r"perimeterx|px-captcha|hcaptcha|g-recaptcha", re.I),
)

#: Parked-domain and registrar placeholder signatures (SS4, condition 5).
_PARKED_PATTERNS = (
    re.compile(r"this domain (?:name )?is for sale", re.I),
    re.compile(r"buy this domain", re.I),
    re.compile(r"domain (?:name )?(?:is )?parked", re.I),
    re.compile(r"parked (?:free )?(?:courtesy of|by)", re.I),
    re.compile(r"the domain .{0,60} may be for sale", re.I),
    re.compile(r"future home of something quite cool", re.I),
    re.compile(r"godaddy\.com.*parked", re.I | re.S),
    re.compile(r"sedoparking|parkingcrew|bodis\.com|afternic", re.I),
    re.compile(r"<title>\s*(?:welcome to )?nginx!?\s*</title>", re.I),
    re.compile(r"<title>\s*apache2? (?:ubuntu |debian )?default page", re.I),
    re.compile(r"index of /", re.I),
    re.compile(r"account suspended", re.I),
    re.compile(r"website coming soon", re.I),
    re.compile(r"under construction", re.I),
)

_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def visible_text(html: str) -> str:
    """Return page text with obvious chrome removed.

    Fast path used both by the sufficiency test and by the DOM extraction
    fallback in :mod:`app.extract.content`.
    """
    if not html:
        return ""
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # pragma: no cover - selectolax is very tolerant
        return ""
    for tag in _CHROME_TAGS:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    if body is None:
        return ""
    return body.text(separator=" ", strip=True) or ""


def count_words(text: str) -> int:
    """Count word-like tokens.

    Digits and single characters are excluded so that a page of phone numbers,
    prices or nav abbreviations cannot pass the threshold on its own.
    """
    return len(_WORD_RE.findall(text or ""))


def looks_like_challenge(html: str) -> bool:
    head = html[:20000] if html else ""
    return any(pattern.search(head) for pattern in _CHALLENGE_PATTERNS)


def looks_parked(html: str) -> bool:
    head = html[:20000] if html else ""
    return any(pattern.search(head) for pattern in _PARKED_PATTERNS)


def assess(
    html: str,
    *,
    is_homepage: bool,
    min_words_homepage: int,
    min_words_internal: int,
) -> SufficiencyVerdict:
    """Decide whether an HTTP response gave us usable content.

    Returns a verdict whose :attr:`~app.models.internal.SufficiencyVerdict.needs_render`
    property drives the single browser-fallback attempt permitted by SS5.
    """
    if not html or not html.strip():
        return SufficiencyVerdict(sufficient=False, word_count=0, reason="empty_body")

    if looks_like_challenge(html):
        return SufficiencyVerdict(sufficient=False, word_count=0, reason="challenge")

    if looks_parked(html):
        # Parked domains are a terminal condition, not a rendering problem.
        return SufficiencyVerdict(sufficient=False, word_count=0, reason="parked")

    text = visible_text(html)
    words = count_words(text)
    threshold = min_words_homepage if is_homepage else min_words_internal
    if words < threshold:
        return SufficiencyVerdict(
            sufficient=False,
            word_count=words,
            reason="thin_dom" if words else "js_shell",
        )
    return SufficiencyVerdict(sufficient=True, word_count=words, reason="")
