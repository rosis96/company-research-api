"""Deterministic page ranking and selection.

Architecture reference: SS7.  No model, no embeddings, no external service --
every score is reproducible from ``lexicon.yaml`` and the URL/anchor text alone
(invariant 1).  ``score_breakdown`` records each contribution so any ranking
decision can be explained after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from app.config.loader import Concept, Lexicon, get_exclusions, get_lexicon
from app.crawl.normalize import (
    has_date_in_path,
    is_careers_overview,
    locale_of,
    path_segments,
    term_variants,
    variant_tiers,
)
from app.models.internal import Candidate, ExtractedPage, LinkRegion
from app.models.request import FOCUS_CATEGORY_MAP
from app.security.domains import subdomain_label

#: Page types that can act as a hub for depth-2 expansion (SS6).
HUB_PAGE_TYPES = frozenset(
    {
        "case_study_index",
        "portfolio",
        "services",
        "solutions",
        "products",
        "industries",
    }
)

FOCUS_SCORE_BONUS = 15.0
HUB_MIN_CHILDREN = 3


@dataclass(frozen=True, slots=True)
class RankContext:
    """Everything ranking needs beyond the candidate itself."""

    scope_domain: str
    seed_locale: str | None = None
    focus_categories: frozenset[str] = frozenset()

    @classmethod
    def build(cls, scope_domain: str, seed_url: str, focus: list[str] | None) -> RankContext:
        categories: set[str] = set()
        for item in focus or []:
            categories.update(FOCUS_CATEGORY_MAP.get(item, ()))
        return cls(
            scope_domain=scope_domain,
            seed_locale=locale_of(seed_url),
            focus_categories=frozenset(categories),
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _match_concept(text: str, lexicon: Lexicon) -> Concept | None:
    """Match ``text`` against the lexicon, longest n-gram first.

    Tier order matters: the longest matching phrase wins, and only within a tier
    does concept weight break the tie.  See ``variant_tiers`` for why.
    """
    for tier in variant_tiers(text):
        best: Concept | None = None
        for variant in tier:
            concept = lexicon.concept_for_term(variant)
            if concept is not None and (best is None or concept.weight > best.weight):
                best = concept
        if best is not None:
            return best
    return None


def classify(candidate: Candidate, lexicon: Lexicon | None = None) -> tuple[Concept | None, str, str]:
    """Return ``(concept, page_type, category)`` for a candidate URL.

    A concept matched on the *last* path segment means the URL is that section's
    index page; matched earlier, it is a detail page below that section.
    """
    lexicon = lexicon or get_lexicon()
    segments = path_segments(candidate.url)

    if not segments:
        return None, "homepage", "homepage"

    if is_careers_overview(candidate.url):
        careers = lexicon.concept_for_term("career")
        if careers is not None:
            return careers, careers.index_type, careers.category

    # Path segments take precedence, scanned outside-in.
    for index, segment in enumerate(segments):
        concept = _match_concept(segment, lexicon)
        if concept is None:
            continue
        if concept.single_segment_only and len(segments) != 1:
            continue
        is_index = index == len(segments) - 1
        if is_index:
            return concept, concept.index_type, concept.index_category
        return concept, concept.detail_type, concept.category

    # Fall back to anchor text, which is often more informative than a slug.
    for anchor in candidate.anchor_texts:
        concept = _match_concept(anchor, lexicon)
        if concept is None:
            continue
        if concept.single_segment_only and len(segments) != 1:
            continue
        if len(segments) <= 1:
            return concept, concept.index_type, concept.index_category
        return concept, concept.detail_type, concept.category

    # Hub children inherit their parent's identity: /work/12345-project carries
    # no keywords at all, but we know what it is from what linked to it.
    if candidate.hub_parent_concept:
        parent = lexicon.concept_for_term(candidate.hub_parent_concept)
        if parent is not None:
            return parent, parent.detail_type, parent.category

    return None, "other", "other"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_candidate(
    candidate: Candidate,
    context: RankContext,
    lexicon: Lexicon | None = None,
) -> Candidate:
    """Score one candidate in place and return it."""
    lexicon = lexicon or get_lexicon()
    exclusions = get_exclusions()
    structural = lexicon.structural
    penalties = lexicon.penalties
    breakdown: dict[str, float] = {}

    concept, page_type, category = classify(candidate, lexicon)
    candidate.page_type = page_type
    candidate.category = category
    candidate.concept_key = concept.key if concept else None

    base = float(concept.weight) if concept else 0.0
    breakdown["concept"] = base
    total = base

    # --- structural bonuses -------------------------------------------------
    if candidate.hub_parent_score > 0:
        inherited = candidate.hub_parent_score * float(structural["hub_inheritance_ratio"])
        breakdown["hub_inheritance"] = inherited
        total += inherited

    if LinkRegion.NAV in candidate.regions or LinkRegion.HEADER in candidate.regions:
        total += structural["region_nav"]
        breakdown["region"] = float(structural["region_nav"])
    elif LinkRegion.MAIN in candidate.regions or LinkRegion.BODY in candidate.regions:
        total += structural["region_main"]
        breakdown["region"] = float(structural["region_main"])
    elif candidate.regions == {LinkRegion.FOOTER}:
        total += structural["region_footer"]
        breakdown["region"] = float(structural["region_footer"])

    segments = path_segments(candidate.url)
    depth = len(segments)
    if depth == 1:
        total += structural["path_depth_1"]
        breakdown["path_depth"] = float(structural["path_depth_1"])
    elif depth == 2:
        total += structural["path_depth_2"]
        breakdown["path_depth"] = float(structural["path_depth_2"])
    elif depth >= 4:
        total += structural["path_depth_4_plus"]
        breakdown["path_depth"] = float(structural["path_depth_4_plus"])

    if candidate.in_sitemap:
        total += structural["in_sitemap"]
        breakdown["sitemap"] = float(structural["in_sitemap"])

    if candidate.inbound_links >= structural["linked_from_many_threshold"]:
        total += structural["linked_from_many"]
        breakdown["inbound_links"] = float(structural["linked_from_many"])

    if _is_recent(candidate.lastmod, int(structural["recent_lastmod_months"])):
        total += structural["recent_lastmod"]
        breakdown["recent"] = float(structural["recent_lastmod"])

    # --- penalties ----------------------------------------------------------
    if has_date_in_path(candidate.url):
        total += penalties["date_in_path"]
        breakdown["date_in_path"] = float(penalties["date_in_path"])

    split = urlsplit(candidate.url)
    if split.query:
        total += penalties["query_string"]
        breakdown["query_string"] = float(penalties["query_string"])

    if len(split.path) > penalties["long_path_chars"]:
        total += penalties["long_path"]
        breakdown["long_path"] = float(penalties["long_path"])

    candidate_locale = locale_of(candidate.url)
    if candidate_locale is not None and candidate_locale != context.seed_locale:
        total += penalties["locale_mismatch"]
        breakdown["locale_mismatch"] = float(penalties["locale_mismatch"])

    noise_hits = {
        variant
        for segment in segments
        for variant in term_variants(segment)
        if variant in lexicon.soft_noise_terms
    }
    if noise_hits:
        total += penalties["soft_noise"]
        breakdown["soft_noise"] = float(penalties["soft_noise"])

    tier = exclusions.subdomain_tier(subdomain_label(split.hostname or ""))
    if tier == "content" and subdomain_label(split.hostname or ""):
        total += penalties["subdomain_content"]
        breakdown["subdomain"] = float(penalties["subdomain_content"])
    elif tier in ("low_value", "locale"):
        total += penalties["subdomain_low_value"]
        breakdown["subdomain"] = float(penalties["subdomain_low_value"])

    # --- focus bias ---------------------------------------------------------
    if category in context.focus_categories:
        total += FOCUS_SCORE_BONUS
        breakdown["focus"] = FOCUS_SCORE_BONUS

    candidate.score = round(total, 2)
    candidate.score_breakdown = breakdown
    return candidate


def _is_recent(lastmod: str | None, months: int) -> bool:
    parsed = parse_lastmod(lastmod)
    if parsed is None:
        return False
    age_days = (datetime.now(UTC) - parsed).days
    return 0 <= age_days <= months * 31


def parse_lastmod(lastmod: str | None) -> datetime | None:
    """Parse a sitemap ``<lastmod>`` value; ``None`` when unusable."""
    if not lastmod:
        return None
    text = lastmod.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _is_locale_mismatch(url: str, context: RankContext) -> bool:
    candidate_locale = locale_of(url)
    return candidate_locale is not None and candidate_locale != context.seed_locale


def sort_key(candidate: Candidate) -> tuple[float, str]:
    """Deterministic ordering: score descending, then URL ascending."""
    return (-candidate.score, candidate.url)


def select(
    candidates: list[Candidate],
    *,
    slots: int,
    min_score: int,
    context: RankContext,
    lexicon: Lexicon | None = None,
    allow_fallback_categories: bool = False,
    already_selected: list[Candidate] | None = None,
) -> list[Candidate]:
    """Choose pages using category quotas, not raw top-N.

    A naive top-N on a site with forty case studies returns forty case studies
    and nothing else; the GPT then knows the clients intimately and has no idea
    what the company sells (SS7).
    """
    lexicon = lexicon or get_lexicon()
    if slots <= 0:
        return []

    counts: dict[str, int] = {}
    locale_mismatches = 0
    for existing in already_selected or []:
        counts[existing.category] = counts.get(existing.category, 0) + 1
        if _is_locale_mismatch(existing.url, context):
            locale_mismatches += 1

    eligible = [
        candidate
        for candidate in candidates
        if candidate.score >= min_score
        and (
            allow_fallback_categories
            or candidate.category not in lexicon.fallback_only_categories
        )
    ]
    eligible.sort(key=sort_key)

    chosen: list[Candidate] = []
    taken: set[str] = set()

    def admissible(candidate: Candidate) -> bool:
        """Enforce the hard cap on locale-duplicated pages (SS6)."""
        nonlocal locale_mismatches
        if not _is_locale_mismatch(candidate.url, context):
            return True
        if locale_mismatches >= lexicon.max_locale_mismatch_pages:
            return False
        locale_mismatches += 1
        return True

    def remaining_for(category: str) -> int:
        quota = lexicon.quota_for(category)
        limit = quota.max if quota else 1
        if category in context.focus_categories and quota is not None:
            limit = max(limit, quota.max + 1)
        return limit - counts.get(category, 0)

    # Pass 1: fill each category's target, in the order quotas are declared.
    for quota in lexicon.quotas:
        if len(chosen) >= slots:
            break
        target = quota.target
        if quota.category in context.focus_categories and target > 0:
            target = min(quota.max, target + 1)
        if target <= 0:
            continue
        for candidate in eligible:
            if len(chosen) >= slots:
                break
            if candidate.url in taken or candidate.category != quota.category:
                continue
            if counts.get(quota.category, 0) >= target or remaining_for(quota.category) <= 0:
                break
            if not admissible(candidate):
                continue
            chosen.append(candidate)
            taken.add(candidate.url)
            counts[quota.category] = counts.get(quota.category, 0) + 1

    # Pass 2: spend what is left on the highest remaining scores, still
    # respecting per-category maxima.
    for candidate in eligible:
        if len(chosen) >= slots:
            break
        if candidate.url in taken:
            continue
        if remaining_for(candidate.category) <= 0:
            continue
        if not admissible(candidate):
            continue
        chosen.append(candidate)
        taken.add(candidate.url)
        counts[candidate.category] = counts.get(candidate.category, 0) + 1

    chosen.sort(key=sort_key)
    return chosen


# ---------------------------------------------------------------------------
# Hub detection (depth-2 expansion, SS6)
# ---------------------------------------------------------------------------


def is_hub(page: ExtractedPage) -> bool:
    """A fetched page is a hub if it indexes several children of its own section.

    Both conditions are required: an index-shaped page type, and at least
    ``HUB_MIN_CHILDREN`` in-scope outlinks sharing its own path prefix.
    """
    if page.page_type not in HUB_PAGE_TYPES:
        return False

    prefix = urlsplit(page.final_url or page.url).path.rstrip("/")
    if not prefix or prefix == "":
        return False

    children = 0
    for link in page.outlinks:
        link_path = urlsplit(link.url).path.rstrip("/")
        if link_path == prefix:
            continue
        if link_path.startswith(prefix + "/"):
            children += 1
    return children >= HUB_MIN_CHILDREN


def hub_children(page: ExtractedPage, lexicon: Lexicon | None = None) -> list[Candidate]:
    """Return the depth-2 candidates a hub page contributes."""
    lexicon = lexicon or get_lexicon()
    prefix = urlsplit(page.final_url or page.url).path.rstrip("/")
    concept_key = page.page_type
    parent_concept = None
    for concept in lexicon.concepts:
        if page.page_type in (concept.index_type, concept.detail_type):
            parent_concept = concept
            break

    children: list[Candidate] = []
    for link in page.outlinks:
        link_path = urlsplit(link.url).path.rstrip("/")
        if link_path == prefix or not link_path.startswith(prefix + "/"):
            continue
        child = Candidate(
            url=link.url,
            anchor_texts=list(link.anchor_texts),
            regions=set(link.regions),
            depth=2,
            discovered_via=page.final_url or page.url,
            inbound_links=link.inbound_links,
            hub_parent_score=float(parent_concept.weight) if parent_concept else 0.0,
            hub_parent_concept=parent_concept.terms[0] if parent_concept else concept_key,
        )
        children.append(child)
    return children
