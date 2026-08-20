"""Ranking, classification, selection and hub-detection tests (SS7, SS6)."""

from __future__ import annotations

import pytest

from app.config.loader import get_lexicon
from app.crawl.rank import (
    HUB_MIN_CHILDREN,
    RankContext,
    classify,
    hub_children,
    is_hub,
    parse_lastmod,
    score_candidate,
    select,
)
from app.models.internal import Candidate, ExtractedPage, FetchMethod, LinkRegion

DOMAIN = "acme.com"


def ctx(focus: list[str] | None = None, seed: str = "https://acme.com/") -> RankContext:
    return RankContext.build(DOMAIN, seed, focus)


def cand(
    path: str,
    anchor: str = "",
    region: LinkRegion = LinkRegion.BODY,
    **kwargs,
) -> Candidate:
    return Candidate(
        url=f"https://{DOMAIN}{path}",
        anchor_texts=[anchor] if anchor else [],
        regions={region},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "page_type", "category"),
    [
        ("/", "homepage", "homepage"),
        ("/case-studies", "case_study_index", "case_study_index"),
        ("/case-studies/boeing", "case_study", "case_study_detail"),
        ("/success-stories", "case_study_index", "case_study_index"),
        ("/success-stories/acme", "case_study", "case_study_detail"),
        ("/services", "services", "offering"),
        ("/services/machining", "service_detail", "offering"),
        ("/solutions", "solutions", "offering"),
        ("/products/widget", "product_detail", "offering"),
        ("/industries", "industries", "industries"),
        ("/about-us", "about", "about"),
        ("/our-story", "about", "about"),
        ("/pricing", "pricing", "pricing"),
        ("/testimonials", "testimonials", "customers"),
        ("/our-clients", "customers", "customers"),
        ("/portfolio", "portfolio", "case_study_index"),
        ("/work/12345", "case_study", "case_study_detail"),
        ("/how-it-works", "process", "process"),
        ("/careers", "careers_overview", "careers"),
        ("/team", "team", "other"),
        ("/blog/post", "blog_post", "blog"),
        ("/xyzzy", "other", "other"),
    ],
)
def test_classification(path: str, page_type: str, category: str) -> None:
    concept, actual_type, actual_category = classify(cand(path))
    assert (actual_type, actual_category) == (page_type, category)


def test_anchor_text_classifies_opaque_slugs() -> None:
    """A slug with no keywords still classifies from its anchor text."""
    _, page_type, category = classify(cand("/s/12345", anchor="Client Success Stories"))
    assert page_type == "case_study"
    assert category == "case_study_detail"


def test_careers_matches_only_a_single_segment() -> None:
    _, page_type, _ = classify(cand("/careers"))
    assert page_type == "careers_overview"
    # Deeper careers paths are excluded upstream, but classification must not
    # label them as a careers overview either.
    _, deep_type, _ = classify(cand("/careers/engineering/manager"))
    assert deep_type != "careers_overview"


def test_hub_child_inherits_parent_identity() -> None:
    child = cand("/work/48211-a", hub_parent_concept="our-work", hub_parent_score=90.0)
    _, page_type, category = classify(child)
    assert page_type == "case_study"
    assert category == "case_study_detail"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_scoring_is_deterministic() -> None:
    context = ctx()
    first = score_candidate(cand("/case-studies", "Case Studies", LinkRegion.NAV), context)
    second = score_candidate(cand("/case-studies", "Case Studies", LinkRegion.NAV), context)
    assert first.score == second.score
    assert first.score_breakdown == second.score_breakdown


def test_concept_weights_order_categories() -> None:
    context = ctx()
    scores = {
        path: score_candidate(cand(path, region=LinkRegion.NAV), context).score
        for path in ("/case-studies", "/services", "/about", "/pricing", "/team")
    }
    assert scores["/case-studies"] > scores["/services"] > scores["/about"] > scores["/pricing"]
    assert scores["/pricing"] > scores["/team"]


def test_nav_placement_beats_footer_placement() -> None:
    context = ctx()
    nav = score_candidate(cand("/services", region=LinkRegion.NAV), context).score
    footer = score_candidate(cand("/services", region=LinkRegion.FOOTER), context).score
    assert nav - footer == 30  # +20 nav vs -10 footer-only


def test_structural_bonuses_apply() -> None:
    context = ctx()
    plain = score_candidate(cand("/services"), context).score
    boosted = score_candidate(
        cand("/services", in_sitemap=True, inbound_links=5), context
    ).score
    assert boosted - plain == 13  # +5 sitemap, +8 many inbound links


@pytest.mark.parametrize(
    ("path", "expected_penalty"),
    [
        ("/blog/2019/05/post", "date_in_path"),
        ("/services?session=1", "query_string"),
        ("/de/services", "locale_mismatch"),
        ("/events", "soft_noise"),
    ],
)
def test_penalties_apply(path: str, expected_penalty: str) -> None:
    scored = score_candidate(cand(path), ctx())
    assert expected_penalty in scored.score_breakdown
    assert scored.score_breakdown[expected_penalty] < 0


def test_long_path_penalty() -> None:
    long_path = "/services/" + "a" * 90
    scored = score_candidate(cand(long_path), ctx())
    assert "long_path" in scored.score_breakdown


def test_subdomain_tier_penalties() -> None:
    context = ctx()
    apex = Candidate(url="https://acme.com/services")
    content = Candidate(url="https://solutions.acme.com/services")
    low = Candidate(url="https://blog.acme.com/services")
    for candidate in (apex, content, low):
        score_candidate(candidate, context)
    assert apex.score > content.score > low.score


def test_hub_inheritance_rescues_keywordless_slugs() -> None:
    """/work/12345-project has no keywords; the hub bonus is what carries it."""
    context = ctx()
    child = Candidate(
        url="https://acme.com/work/48211-industrial-retrofit",
        depth=2,
        hub_parent_score=90.0,
        hub_parent_concept="our-work",
    )
    score_candidate(child, context)
    assert child.score_breakdown["hub_inheritance"] == pytest.approx(54.0)
    assert child.score >= 25  # above the default selection threshold


def test_focus_adds_a_bonus() -> None:
    plain = score_candidate(cand("/case-studies/x"), ctx()).score
    focused = score_candidate(cand("/case-studies/x"), ctx(focus=["case_studies"])).score
    assert focused - plain == 15


def test_seed_locale_makes_matching_locale_unpenalised() -> None:
    german_seed = ctx(seed="https://acme.com/de/")
    scored = score_candidate(cand("/de/services"), german_seed)
    assert "locale_mismatch" not in scored.score_breakdown


@pytest.mark.parametrize(
    "raw",
    ["2026-07-01", "2026-07-01T10:00:00Z", "2026-07-01T10:00:00+00:00", "2026-07"],
)
def test_lastmod_parsing(raw: str) -> None:
    assert parse_lastmod(raw) is not None


def test_lastmod_parsing_rejects_junk() -> None:
    assert parse_lastmod("not a date") is None
    assert parse_lastmod(None) is None


# ---------------------------------------------------------------------------
# Selection and category diversity
# ---------------------------------------------------------------------------


def test_category_diversity_prevents_a_wall_of_case_studies() -> None:
    """The failure this pass exists to prevent."""
    context = ctx()
    candidates = [cand(f"/case-studies/client-{i}", region=LinkRegion.MAIN) for i in range(40)]
    candidates += [
        cand("/services", "Services", LinkRegion.NAV),
        cand("/about", "About", LinkRegion.NAV),
        cand("/industries", "Industries", LinkRegion.NAV),
        cand("/pricing", "Pricing", LinkRegion.NAV),
    ]
    for candidate in candidates:
        score_candidate(candidate, context)

    chosen = select(candidates, slots=10, min_score=25, context=context)
    categories = [c.category for c in chosen]

    assert categories.count("case_study_detail") <= 3
    assert "offering" in categories
    assert "about" in categories
    assert "industries" in categories
    assert "pricing" in categories


def test_selection_respects_min_score() -> None:
    context = ctx()
    candidates = [cand("/xyzzy"), cand("/services", region=LinkRegion.NAV)]
    for candidate in candidates:
        score_candidate(candidate, context)
    chosen = select(candidates, slots=10, min_score=25, context=context)
    assert [c.url for c in chosen] == [f"https://{DOMAIN}/services"]


def test_selection_is_deterministic_and_sorted() -> None:
    context = ctx()
    candidates = [
        cand("/services", region=LinkRegion.NAV),
        cand("/about", region=LinkRegion.NAV),
        cand("/industries", region=LinkRegion.NAV),
    ]
    for candidate in candidates:
        score_candidate(candidate, context)
    first = [c.url for c in select(candidates, slots=5, min_score=25, context=context)]
    second = [c.url for c in select(list(reversed(candidates)), slots=5, min_score=25, context=context)]
    assert first == second
    scores = [c.score for c in select(candidates, slots=5, min_score=25, context=context)]
    assert scores == sorted(scores, reverse=True)


def test_blog_is_excluded_from_normal_selection() -> None:
    context = ctx()
    candidates = [cand("/blog/some-post", region=LinkRegion.NAV)]
    for candidate in candidates:
        score_candidate(candidate, context)
    assert select(candidates, slots=5, min_score=0, context=context) == []
    allowed = select(
        candidates, slots=5, min_score=0, context=context, allow_fallback_categories=True
    )
    assert len(allowed) == 1


def test_locale_duplicates_are_capped() -> None:
    """SS6: a translated site must not consume the budget in twelve languages."""
    context = ctx()
    candidates = [cand("/services", region=LinkRegion.NAV)]
    candidates += [
        cand(f"/{code}/services", region=LinkRegion.NAV)
        for code in ("de", "fr", "es", "it", "pt", "nl")
    ]
    for candidate in candidates:
        score_candidate(candidate, context)

    chosen = select(candidates, slots=10, min_score=0, context=context)
    mismatched = [c for c in chosen if c.url.split("/")[3] in {"de", "fr", "es", "it", "pt", "nl"}]
    assert len(mismatched) <= get_lexicon().max_locale_mismatch_pages


def test_selection_honours_already_selected_counts() -> None:
    context = ctx()
    candidates = [cand(f"/case-studies/c{i}", region=LinkRegion.MAIN) for i in range(5)]
    for candidate in candidates:
        score_candidate(candidate, context)
    already = [Candidate(url="https://acme.com/case-studies/x", category="case_study_detail")] * 3
    chosen = select(candidates, slots=5, min_score=0, context=context, already_selected=already)
    assert chosen == []


def test_zero_slots_selects_nothing() -> None:
    assert select([cand("/services")], slots=0, min_score=0, context=ctx()) == []


# ---------------------------------------------------------------------------
# Hub detection
# ---------------------------------------------------------------------------


def make_page(url: str, page_type: str, child_paths: list[str]) -> ExtractedPage:
    return ExtractedPage(
        url=url,
        final_url=url,
        page_type=page_type,
        category="case_study_index",
        score=100.0,
        depth=1,
        discovered_via="seed",
        method=FetchMethod.HTTP,
        status_code=200,
        title=None,
        meta_description=None,
        h1=None,
        headings=[],
        language="en",
        text="",
        outlinks=[Candidate(url=f"https://{DOMAIN}{p}") for p in child_paths],
    )


def test_hub_requires_index_type_and_enough_children() -> None:
    children = [f"/case-studies/c{i}" for i in range(HUB_MIN_CHILDREN)]
    hub = make_page(f"https://{DOMAIN}/case-studies", "case_study_index", children)
    assert is_hub(hub)

    too_few = make_page(
        f"https://{DOMAIN}/case-studies", "case_study_index", children[: HUB_MIN_CHILDREN - 1]
    )
    assert not is_hub(too_few)

    wrong_type = make_page(f"https://{DOMAIN}/about", "about", children)
    assert not is_hub(wrong_type)


def test_hub_ignores_links_outside_its_own_prefix() -> None:
    page = make_page(
        f"https://{DOMAIN}/case-studies",
        "case_study_index",
        ["/about", "/services", "/pricing", "/contact"],
    )
    assert not is_hub(page)


def test_hub_children_carry_inheritance_and_depth() -> None:
    page = make_page(
        f"https://{DOMAIN}/case-studies",
        "case_study_index",
        ["/case-studies/a", "/case-studies/b", "/case-studies/c"],
    )
    children = hub_children(page)
    assert len(children) == 3
    assert all(child.depth == 2 for child in children)
    assert all(child.hub_parent_score > 0 for child in children)
    assert all(child.discovered_via == page.final_url for child in children)


def test_homepage_is_never_a_hub() -> None:
    page = make_page(f"https://{DOMAIN}/", "homepage", ["/a", "/b", "/c"])
    assert not is_hub(page)
