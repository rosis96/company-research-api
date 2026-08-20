"""Typed access to the YAML configuration files.

Architecture reference: SS17 build order step 5 -- "lexicon as a standalone
config file".  Application code reads tunables through these dataclasses and
never hard-codes a weight, threshold or exclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).parent


def _load_yaml(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):  # pragma: no cover - defensive
        raise ValueError(f"{name} did not parse to a mapping")
    return data


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Concept:
    key: str
    weight: int
    category: str
    index_type: str
    detail_type: str
    index_category: str
    hub: bool
    terms: tuple[str, ...]
    single_segment_only: bool = False


@dataclass(frozen=True, slots=True)
class Quota:
    category: str
    target: int
    max: int


@dataclass(frozen=True, slots=True)
class Lexicon:
    version: int
    concepts: tuple[Concept, ...]
    structural: dict[str, Any]
    penalties: dict[str, Any]
    soft_noise_terms: frozenset[str]
    quotas: tuple[Quota, ...]
    fallback_only_categories: frozenset[str]
    min_score_default: int
    max_locale_mismatch_pages: int
    term_index: dict[str, Concept] = field(default_factory=dict)

    def concept_for_term(self, term: str) -> Concept | None:
        return self.term_index.get(term)

    def quota_for(self, category: str) -> Quota | None:
        for quota in self.quotas:
            if quota.category == category:
                return quota
        return None


@lru_cache(maxsize=1)
def get_lexicon() -> Lexicon:
    raw = _load_yaml("lexicon.yaml")
    concepts = tuple(
        Concept(
            key=entry["key"],
            weight=int(entry["weight"]),
            category=entry["category"],
            index_type=entry["index_type"],
            detail_type=entry["detail_type"],
            index_category=entry["index_category"],
            hub=bool(entry.get("hub", False)),
            terms=tuple(entry["terms"]),
            single_segment_only=bool(entry.get("single_segment_only", False)),
        )
        for entry in raw["concepts"]
    )
    # Highest weight wins when two concepts share a term.
    term_index: dict[str, Concept] = {}
    for concept in sorted(concepts, key=lambda c: c.weight):
        for term in concept.terms:
            term_index[term] = concept

    return Lexicon(
        version=int(raw["version"]),
        concepts=concepts,
        structural=dict(raw["structural"]),
        penalties=dict(raw["penalties"]),
        soft_noise_terms=frozenset(raw.get("soft_noise_terms", [])),
        quotas=tuple(
            Quota(category=q["category"], target=int(q["target"]), max=int(q["max"]))
            for q in raw["quotas"]
        ),
        fallback_only_categories=frozenset(raw.get("fallback_only_categories", [])),
        min_score_default=int(raw["min_score_default"]),
        max_locale_mismatch_pages=int(raw.get("max_locale_mismatch_pages", 1)),
        term_index=term_index,
    )


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Exclusions:
    version: int
    blocked_schemes: frozenset[str]
    blocked_path_segments: frozenset[str]
    path_substrings: tuple[str, ...]
    careers_overview_segments: frozenset[str]
    careers_overview_prefixes: tuple[str, ...]
    blocked_extensions: frozenset[str]
    blocked_domains: frozenset[str]
    subdomain_primary: frozenset[str]
    subdomain_content: frozenset[str]
    subdomain_low_value: frozenset[str]
    subdomain_excluded: frozenset[str]
    locale_codes: frozenset[str]
    tracking_params: frozenset[str]
    index_filenames: frozenset[str]

    def subdomain_tier(self, label: str) -> str:
        """Classify the leftmost subdomain label into a tier (SS6)."""
        normalized = (label or "").lower()
        if not normalized or normalized in self.subdomain_primary:
            return "primary"
        head = normalized.split(".")[0]
        if head in self.subdomain_excluded:
            return "excluded"
        if head in self.subdomain_low_value:
            return "low_value"
        if head in self.subdomain_content:
            return "content"
        if head in self.locale_codes:
            return "locale"
        return "content"


@lru_cache(maxsize=1)
def get_exclusions() -> Exclusions:
    raw = _load_yaml("exclusions.yaml")
    subs = raw["subdomains"]
    return Exclusions(
        version=int(raw["version"]),
        blocked_schemes=frozenset(raw["blocked_schemes"]),
        blocked_path_segments=frozenset(raw["blocked_path_segments"]),
        path_substrings=tuple(raw["path_substrings"]),
        careers_overview_segments=frozenset(raw["careers_overview_segments"]),
        careers_overview_prefixes=tuple(raw["careers_overview_prefixes"]),
        blocked_extensions=frozenset(raw["blocked_extensions"]),
        blocked_domains=frozenset(raw["blocked_domains"]),
        subdomain_primary=frozenset(subs["primary"]),
        subdomain_content=frozenset(subs["content"]),
        subdomain_low_value=frozenset(subs["low_value"]),
        subdomain_excluded=frozenset(subs["excluded"]),
        locale_codes=frozenset(raw["locale_codes"]),
        tracking_params=frozenset(raw["tracking_params"]),
        index_filenames=frozenset(raw["index_filenames"]),
    )


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlBudget:
    """Resolved budget for one request (SS13)."""

    deadline_ms: int
    max_pages: int
    max_depth: int
    max_renders: int
    max_renders_js_mode: int
    http_concurrency_per_host: int
    render_concurrency: int
    connect_timeout_s: float
    read_timeout_s: float
    total_timeout_s: float
    render_timeout_ms: int
    max_response_bytes: int
    page_content_cap_top: int
    page_content_cap_rest: int
    page_content_cap_top_ranks: int
    global_content_budget: int
    sitemap_url_cap: int
    min_words_keep_page: int
    min_score_select: int
    js_mode_max_pages: int

    def with_overrides(self, **overrides: Any) -> CrawlBudget:
        clean = {k: v for k, v in overrides.items() if v is not None}
        if not clean:
            return self
        current = {f: getattr(self, f) for f in self.__slots__}
        current.update(clean)
        return CrawlBudget(**current)


@dataclass(frozen=True, slots=True)
class Limits:
    version: int
    presets: dict[str, CrawlBudget]
    sufficiency: dict[str, int]
    evidence: dict[str, int]
    thin_site_fallback: dict[str, Any]
    boilerplate: dict[str, Any]
    response: dict[str, Any]

    def budget(self, depth: str) -> CrawlBudget:
        try:
            return self.presets[depth]
        except KeyError as exc:  # pragma: no cover - guarded by pydantic enum
            raise ValueError(f"unknown research_depth {depth!r}") from exc


@lru_cache(maxsize=1)
def get_limits() -> Limits:
    raw = _load_yaml("limits.yaml")
    presets = {
        name: CrawlBudget(
            deadline_ms=int(values["deadline_ms"]),
            max_pages=int(values["max_pages"]),
            max_depth=int(values["max_depth"]),
            max_renders=int(values["max_renders"]),
            max_renders_js_mode=int(values["max_renders_js_mode"]),
            http_concurrency_per_host=int(values["http_concurrency_per_host"]),
            render_concurrency=int(values["render_concurrency"]),
            connect_timeout_s=float(values["connect_timeout_s"]),
            read_timeout_s=float(values["read_timeout_s"]),
            total_timeout_s=float(values["total_timeout_s"]),
            render_timeout_ms=int(values["render_timeout_ms"]),
            max_response_bytes=int(values["max_response_bytes"]),
            page_content_cap_top=int(values["page_content_cap_top"]),
            page_content_cap_rest=int(values["page_content_cap_rest"]),
            page_content_cap_top_ranks=int(values["page_content_cap_top_ranks"]),
            global_content_budget=int(values["global_content_budget"]),
            sitemap_url_cap=int(values["sitemap_url_cap"]),
            min_words_keep_page=int(values["min_words_keep_page"]),
            min_score_select=int(values["min_score_select"]),
            js_mode_max_pages=int(values["js_mode_max_pages"]),
        )
        for name, values in raw["presets"].items()
    }
    return Limits(
        version=int(raw["version"]),
        presets=presets,
        sufficiency={k: int(v) for k, v in raw["sufficiency"].items()},
        evidence={k: int(v) for k, v in raw["evidence"].items()},
        thin_site_fallback=dict(raw["thin_site_fallback"]),
        boilerplate=dict(raw["boilerplate"]),
        response=dict(raw["response"]),
    )


def config_fingerprint() -> str:
    """Stable fingerprint of all tunable configuration.

    Folded into the cache key alongside ``CRA_CONFIG_VERSION`` so that editing a
    weight in ``lexicon.yaml`` cannot silently be masked by stale cache entries
    (SS14).
    """
    import hashlib

    digest = hashlib.sha256()
    for name in ("lexicon.yaml", "exclusions.yaml", "limits.yaml"):
        digest.update((CONFIG_DIR / name).read_bytes())
    return digest.hexdigest()[:16]
