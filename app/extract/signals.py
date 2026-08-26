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


#: Advertising fingerprints, matched against the raw homepage markup (script
#: hosts, link/iframe srcs, and inline script text). Each row is
#: ``(kind, needle, platform, weight, capability)`` where ``kind`` is "sub" for
#: a plain lowercase substring or "re" for a case-insensitive regex.
#:
#: Weight is not popularity -- it is how hard the tag is to explain away:
#:   3 = conversion tracking. Installed for exactly one reason: to let an ad
#:       platform optimise against a purchase/lead. Nobody adds it speculatively.
#:   2 = pixel / audience tag. Strongly suggests campaigns, but can outlive them.
#:   1 = supporting stack. Measurement maturity or paid-attribution software;
#:       correlates with scale, never proof of spend on its own.
#: The literal ``AW-`` conversion ID and Meta pixel ID are pulled out separately
#: as stable, checkable handles (the same ID across two domains = one operator).
_AD_FINGERPRINTS: tuple[tuple[str, str, str, int, str], ...] = (
    # weight 3 -- conversion tracking
    ("sub", "googleadservices.com/pagead/conversion", "Google Ads", 3, "conversion tracking"),
    ("sub", "/pagead/viewthroughconversion", "Google Ads", 3, "view-through conversion"),
    ("sub", "google_conversion_id", "Google Ads", 3, "conversion tracking"),
    ("re", r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]aw-\d{6,}", "Google Ads", 3, "conversion tracking"),
    ("re", r"fbq\(\s*['\"]track['\"]\s*,\s*['\"]purchase['\"]", "Meta", 3, "purchase conversion event"),
    # weight 2 -- pixels and audience tags
    ("sub", "googleads.g.doubleclick.net", "Google Ads", 2, "remarketing tag"),
    ("sub", "connect.facebook.net", "Meta", 2, "Meta Pixel"),
    ("sub", "fbevents.js", "Meta", 2, "Meta Pixel"),
    ("re", r"fbq\(\s*['\"]init['\"]", "Meta", 2, "Meta Pixel"),
    ("sub", "analytics.tiktok.com", "TikTok", 2, "TikTok Pixel"),
    ("sub", "ttq.load", "TikTok", 2, "TikTok Pixel"),
    ("sub", "bat.bing.com", "Microsoft Ads", 2, "UET tag"),
    ("re", r"\buetq\b", "Microsoft Ads", 2, "UET tag"),
    ("sub", "snap.licdn.com/li.lms-analytics", "LinkedIn Ads", 2, "Insight Tag"),
    ("sub", "s.pinimg.com/ct/core.js", "Pinterest Ads", 2, "Pinterest Tag"),
    ("re", r"pintrk\(", "Pinterest Ads", 2, "Pinterest Tag"),
    ("sub", "sc-static.net/scevent", "Snapchat Ads", 2, "Snap Pixel"),
    ("sub", "static.criteo.net", "Criteo", 2, "retargeting tag"),
    # weight 1 -- supporting stack (attribution software is the best scale proxy)
    ("sub", "triplewhale", "Triple Whale", 1, "paid attribution platform"),
    ("sub", "triplepixel", "Triple Whale", 1, "paid attribution platform"),
    ("sub", "northbeam", "Northbeam", 1, "paid attribution platform"),
    ("sub", "hyros", "Hyros", 1, "paid attribution platform"),
    ("sub", "static.klaviyo.com", "Klaviyo", 1, "lifecycle marketing"),
    ("sub", "cdn.attn.tv", "Attentive", 1, "SMS marketing"),
    ("re", r"gtm\.js\?id=gtm-[^\"'&]+&gtm_auth", "Server-side GTM", 1, "advanced measurement"),
)

_AD_REGEXES = {row[1]: re.compile(row[1], re.I) for row in _AD_FINGERPRINTS if row[0] == "re"}

#: Stable identifiers worth keeping: the same handle across two domains means
#: one operator running both brands.
_CONVERSION_ID_RE = re.compile(r"AW-\d{6,}", re.I)
_META_PIXEL_ID_RE = re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,})['\"]", re.I)

#: Paths that exist only to receive bought traffic. A brochure site has /about
#: and /contact; a site running campaigns has /lp/spring-sale.
_LANDING_PATTERNS = ("/lp/", "/offer", "/promo", "/campaign", "/try-", "/free-trial")


def detect_advertising(html: str) -> dict:
    """Read paid-advertising fingerprints off the homepage markup.

    Returns ``{score, platforms, evidence, identifiers, landing_pages}`` -- the
    shape RevCadence's ``ad_signals.classify`` consumes. ``score`` is the summed
    weight of the *distinct* capabilities found (fbq init and fbevents.js both
    prove one Meta Pixel, so they count once), leaving breadth and landing-page
    bonuses to the consumer. An advertiser nearly always carries this evidence
    in its own markup, because ad platforms require an on-site tag to attribute
    conversions. Absence is a finding, not proof: server-side tagging and
    consent gating hide tags on exactly the biggest advertisers, so a consumer
    must treat "nothing found" as a ranking signal, never a hard disqualifier.
    """
    if not html:
        return {"score": 0, "platforms": [], "evidence": [], "identifiers": {}, "landing_pages": []}
    hay = html.lower()

    platforms: list[str] = []
    evidence: list[str] = []
    weight_by_evidence: dict[str, int] = {}
    for kind, needle, platform, weight, capability in _AD_FINGERPRINTS:
        hit = (needle in hay) if kind == "sub" else bool(_AD_REGEXES[needle].search(html))
        if not hit:
            continue
        label = f"{platform}: {capability}"
        if label not in weight_by_evidence:  # one Meta Pixel is one, not two
            weight_by_evidence[label] = weight
            evidence.append(label)
        if platform not in platforms:
            platforms.append(platform)

    identifiers: dict[str, str] = {}
    conv = _CONVERSION_ID_RE.search(html)
    if conv:
        identifiers["google_conversion_id"] = conv.group(0).upper()
    pixel = _META_PIXEL_ID_RE.search(html)
    if pixel:
        identifiers["meta_pixel_id"] = pixel.group(1)

    return {
        "score": sum(weight_by_evidence.values()),
        "platforms": platforms,
        "evidence": evidence,
        "identifiers": identifiers,
        "landing_pages": [],  # filled from crawled URLs by detect_landing_pages
    }


def detect_landing_pages(urls: list[str]) -> list[str]:
    """Campaign landing-page URLs among the pages we crawled. The count is an
    infrastructure signal (a funded testing programme), never a budget."""
    out: list[str] = []
    for url in urls:
        low = (url or "").lower()
        if any(p in low for p in _LANDING_PATTERNS) and url not in out:
            out.append(url)
    return out[:_MAX_PER_LIST]


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
