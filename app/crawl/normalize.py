"""URL normalization, deduplication and exclusion rules.

Architecture reference: SS6, "URL normalization and dedup" and "Hard exclusions".

All rule data comes from ``exclusions.yaml``; this module contains only the
mechanics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from app.config.loader import Exclusions, get_exclusions
from app.security.domains import registrable_domain, subdomain_label

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MULTI_SLASH = re.compile(r"/{2,}")
_DATE_IN_PATH = re.compile(r"/(?:19|20)\d{2}(?:/\d{1,2})?(?:/|$)")
_PAGINATION = re.compile(r"/page[/-]?\d+/?$|[?&]page=\d+")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ExclusionVerdict:
    excluded: bool
    reason: str = ""


def normalize_url(raw: str, base: str | None = None) -> str | None:
    """Return a canonical form of ``raw``, or ``None`` if it is not fetchable.

    Handles: relative resolution, scheme/host lowercasing, default-port
    stripping, fragment removal, tracking-parameter removal, duplicate-slash
    collapsing, index-filename removal and trailing-slash canonicalisation.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or candidate.startswith("#"):
        return None

    exclusions = get_exclusions()

    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", candidate)
    if scheme_match and scheme_match.group(1).lower() in exclusions.blocked_schemes:
        return None

    if base:
        candidate = urljoin(base, candidate)

    try:
        split = urlsplit(candidate)
    except ValueError:
        return None

    scheme = (split.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    if not split.hostname:
        return None
    if split.username or split.password:
        return None

    host = split.hostname.lower().rstrip(".")
    try:
        port = split.port
    except ValueError:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"

    path = _MULTI_SLASH.sub("/", split.path or "/")
    if not path.startswith("/"):
        path = "/" + path

    # Drop index filenames so /about/ and /about/index.html collapse together.
    tail = path.rsplit("/", 1)[-1]
    if tail.lower() in exclusions.index_filenames:
        path = path[: -len(tail)] or "/"

    # Canonical trailing slash: only the root keeps one.
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    query = _clean_query(split.query, exclusions)
    return urlunsplit((scheme, netloc, path, query, ""))


def _clean_query(query: str, exclusions: Exclusions) -> str:
    if not query:
        return ""
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in exclusions.tracking_params
    ]
    if not kept:
        return ""
    kept.sort()  # deterministic ordering so ?a=1&b=2 and ?b=2&a=1 dedupe
    return urlencode(kept, doseq=True)


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def path_segments(url: str) -> list[str]:
    path = urlsplit(url).path
    return [segment for segment in path.split("/") if segment]


def is_careers_overview(url: str) -> bool:
    """True for a top-level careers/culture overview page (SS7).

    Exactly one path segment, so ``/careers`` matches but
    ``/careers/senior-engineer`` does not.
    """
    exclusions = get_exclusions()
    segments = path_segments(url)
    if len(segments) != 1:
        return False
    segment = segments[0].lower()
    if segment in exclusions.careers_overview_segments:
        return True
    return any(segment.startswith(prefix) for prefix in exclusions.careers_overview_prefixes)


def evaluate_exclusions(url: str) -> ExclusionVerdict:
    """Apply the hard exclusion rules from SS6."""
    exclusions = get_exclusions()
    split = urlsplit(url)
    host = (split.hostname or "").lower()
    path = split.path or "/"
    lowered_path = path.lower()

    if registrable_domain(host) in exclusions.blocked_domains:
        return ExclusionVerdict(True, "blocked_domain")

    tier = exclusions.subdomain_tier(subdomain_label(host))
    if tier == "excluded":
        return ExclusionVerdict(True, "excluded_subdomain")

    for substring in exclusions.path_substrings:
        if substring in lowered_path:
            return ExclusionVerdict(True, "blocked_path_substring")

    segments = [s.lower() for s in path.split("/") if s]

    # Careers: the single-segment overview page is allowed at low weight, but
    # anything below it is a job listing and is excluded (SS7).
    careers_overview = is_careers_overview(url)
    if segments and not careers_overview:
        first = segments[0]
        is_careers_root = first in exclusions.careers_overview_segments or any(
            first.startswith(prefix) for prefix in exclusions.careers_overview_prefixes
        )
        if is_careers_root and len(segments) > 1:
            return ExclusionVerdict(True, "careers_detail")

    for segment in segments:
        if segment in exclusions.blocked_path_segments:
            if careers_overview:
                continue
            return ExclusionVerdict(True, f"blocked_segment:{segment}")

    if _PAGINATION.search(url):
        return ExclusionVerdict(True, "pagination")

    if segments and "." in segments[-1]:
        extension = segments[-1].rsplit(".", 1)[-1].lower()
        if extension in exclusions.blocked_extensions:
            return ExclusionVerdict(True, f"blocked_extension:{extension}")

    return ExclusionVerdict(False)


def has_date_in_path(url: str) -> bool:
    return bool(_DATE_IN_PATH.search(urlsplit(url).path))


def locale_of(url: str) -> str | None:
    """Return the locale code this URL is scoped to, if any.

    Looks at both the first path segment (``/de/services``) and the leftmost
    subdomain (``de.example.com``).
    """
    exclusions = get_exclusions()
    split = urlsplit(url)

    segments = path_segments(url)
    if segments:
        first = segments[0].lower()
        if first in exclusions.locale_codes:
            return first

    label = subdomain_label(split.hostname or "").split(".")[0].lower()
    if label in exclusions.locale_codes:
        return label
    return None


# ---------------------------------------------------------------------------
# Tokenisation for ranking
# ---------------------------------------------------------------------------


def _singular_variants(token: str) -> set[str]:
    out = {token}
    if len(token) > 4 and token.endswith("ies"):
        out.add(token[:-3] + "y")
        out.add(token[:-1])
    elif len(token) > 3 and token.endswith("ses"):
        out.add(token[:-2])
    elif len(token) > 3 and token.endswith("es"):
        out.add(token[:-2])
        out.add(token[:-1])
    elif len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
        out.add(token[:-1])
    return out


def term_variants(text: str) -> set[str]:
    """Produce the match keys for a path segment or anchor phrase.

    ``"Case Studies"`` and ``"/case_studies/"`` both yield ``case-studies``,
    ``case-studie`` and ``case-study``, so lexicon terms match reasonable
    variations without needing an exact URL name (SS7).
    """
    if not text:
        return set()
    lowered = text.lower().strip()
    tokens = [t for t in _TOKEN_SPLIT.split(lowered) if t]
    if not tokens:
        return set()

    variants: set[str] = set()
    for tier in variant_tiers(text):
        variants |= tier
    return variants


def variant_tiers(text: str) -> list[set[str]]:
    """Return match keys grouped by n-gram length, longest first.

    Matching must prefer the longest phrase that matches, not the
    highest-weighted token inside it.  Without this, ``/how-it-works`` matches
    the single token ``work`` (portfolio, weight 90) instead of the phrase
    ``how-it-works`` (process, weight 50), and ``"Client Success Stories"``
    matches ``client`` (customers) instead of ``success-stories`` (case study).

    Contiguous n-grams are generated so that a concept term appearing anywhere
    inside a longer phrase is still found: ``"Client Success Stories"`` yields
    ``success-stories`` -> ``success-story``.
    """
    if not text:
        return []
    lowered = text.lower().strip()
    tokens = [t for t in _TOKEN_SPLIT.split(lowered) if t]
    if not tokens:
        return []

    tiers: list[set[str]] = []
    for size in range(min(len(tokens), 4), 0, -1):
        tier: set[str] = set()
        for start in range(len(tokens) - size + 1):
            gram = tokens[start : start + size]
            joined = "-".join(gram)
            tier |= _singular_variants(joined)
            if size == 1:
                tier.add(joined)
            else:
                tier.add("".join(gram))
                # Singularise the final word of the n-gram: "our-services" also
                # needs to produce "our-service".
                for tail in _singular_variants(gram[-1]):
                    tier.add("-".join(gram[:-1] + [tail]))
        if tier:
            tiers.append(tier)
    return tiers


def deduplicate(urls: list[str]) -> list[str]:
    """Order-preserving deduplication of already-normalized URLs."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out
