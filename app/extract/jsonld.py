"""JSON-LD extraction, narrowed to an explicit field allowlist.

Architecture reference: SS8, step 4.

``aggregateRating`` and ``review`` are excluded as a *correctness* requirement
rather than a size optimisation: they are almost always third-party review data,
and this system is first-party-only.  ``sameAs``, ``logo``, ``image`` and
``contactPoint`` are excluded because they are not evidence for personalized
copy.
"""

from __future__ import annotations

import json
import re
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from app.models.response import Organization

ACCEPTED_TYPES = frozenset({"organization", "corporation", "localbusiness"})

#: Never read, even if present.  Listed explicitly so the exclusion is auditable.
DISCARDED_FIELDS = frozenset(
    {
        "sameas",
        "logo",
        "image",
        "contactpoint",
        "employee",
        "founder",
        "makesoffer",
        "aggregaterating",
        "review",
        "potentialaction",
    }
)

_WS = re.compile(r"\s+")

MAX_LOCATIONS = 3
MAX_AREA_SERVED = 3
MAX_KNOWS_ABOUT = 5


def _clean(value: Any, limit: int) -> str | None:
    """Coerce a JSON-LD value to a bounded string.

    Handles the wrapped forms schema.org allows, e.g. ``numberOfEmployees`` as a
    ``QuantitativeValue`` object rather than a bare number.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("name", "value", "@value", "minValue"):
            if key in value:
                value = value[key]
                break
        else:
            return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(int(value) if float(value).is_integer() else value)
    if not isinstance(value, str):
        return None
    collapsed = _WS.sub(" ", value).strip()
    return collapsed[:limit] or None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _type_names(node: dict) -> set[str]:
    return {
        str(t).lower().split("/")[-1]
        for t in _as_list(node.get("@type"))
        if isinstance(t, (str, int))
    }


def iter_jsonld(html: str) -> list[dict]:
    """Return every JSON-LD object found in the document.

    Malformed JSON-LD is skipped silently -- it must never be fatal.
    """
    if not html:
        return []
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # pragma: no cover
        return []

    found: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(deep=True, strip=False)
        if not raw or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        found.extend(_flatten(parsed))
    return found


def _flatten(node: Any, depth: int = 0) -> list[dict]:
    if depth > 6:
        return []
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out.extend(_flatten(item, depth + 1))
    elif isinstance(node, dict):
        out.append(node)
        for key in ("@graph", "mainEntity", "about", "publisher", "provider"):
            if key in node:
                out.extend(_flatten(node[key], depth + 1))
    return out


def _address_strings(value: Any) -> list[str]:
    """Locality / region / country only -- never a street address."""
    results: list[str] = []
    for entry in _as_list(value):
        if isinstance(entry, str):
            cleaned = _clean(entry, 120)
            if cleaned:
                results.append(cleaned)
            continue
        if not isinstance(entry, dict):
            continue
        parts = [
            _clean(entry.get("addressLocality"), 60),
            _clean(entry.get("addressRegion"), 60),
            _clean(entry.get("addressCountry"), 60),
        ]
        joined = ", ".join(p for p in parts if p)
        if joined:
            results.append(joined)
    return results


def extract_organization(
    documents: list[dict],
    *,
    description_cap: int,
    total_cap: int,
) -> tuple[Organization | None, str | None, str | None]:
    """Return ``(organization, site_name, tagline)`` from JSON-LD.

    Only the nine allowlisted fields are read; everything else in the document
    is ignored regardless of what it contains.
    """
    organization = Organization()
    site_name: str | None = None
    tagline: str | None = None

    for node in documents:
        if not _type_names(node) & ACCEPTED_TYPES:
            continue

        site_name = site_name or _clean(node.get("name"), 120)
        tagline = tagline or _clean(node.get("slogan"), 200)

        organization.legal_name = organization.legal_name or _clean(node.get("legalName"), 120)
        organization.description = organization.description or _clean(
            node.get("description"), description_cap
        )
        organization.founding_date = organization.founding_date or _clean(
            node.get("foundingDate"), 20
        )
        organization.employees = organization.employees or _clean(
            node.get("numberOfEmployees"), 20
        )

        if not organization.locations:
            organization.locations = _address_strings(node.get("address"))[:MAX_LOCATIONS]

        if not organization.area_served:
            areas = [_clean(a, 60) for a in _as_list(node.get("areaServed"))]
            organization.area_served = [a for a in areas if a][:MAX_AREA_SERVED]

        if not organization.knows_about:
            topics = [_clean(a, 60) for a in _as_list(node.get("knowsAbout"))]
            organization.knows_about = [a for a in topics if a][:MAX_KNOWS_ABOUT]

    if organization.is_empty():
        return None, site_name, tagline

    _enforce_total_cap(organization, total_cap)
    return organization, site_name, tagline


def _enforce_total_cap(organization: Organization, total_cap: int) -> None:
    """Trim the block until it fits the hard character cap from SS8."""
    def size() -> int:
        return len(json.dumps(organization.model_dump(exclude_none=True), ensure_ascii=False))

    if size() <= total_cap:
        return
    for trim in ("knows_about", "area_served", "locations"):
        while getattr(organization, trim) and size() > total_cap:
            getattr(organization, trim).pop()
    if size() > total_cap and organization.description:
        overflow = size() - total_cap
        organization.description = organization.description[: max(0, len(organization.description) - overflow - 4)] or None
    if size() > total_cap:
        organization.description = None
