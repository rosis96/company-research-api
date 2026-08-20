"""Extraction tests: sufficiency, content, metadata, JSON-LD, boilerplate (SS4, SS8)."""

from __future__ import annotations

import pytest

from app.extract.boilerplate import remove_boilerplate
from app.extract.content import (
    extract_content,
    normalize_text,
    split_blocks,
    truncate_at_paragraph,
)
from app.extract.jsonld import DISCARDED_FIELDS, extract_organization, iter_jsonld
from app.extract.metadata import extract_metadata, extract_site_name
from app.fetch.sufficiency import (
    assess,
    count_words,
    looks_like_challenge,
    looks_parked,
    visible_text,
)

PROSE = (
    "Acme Precision manufactures components for aerospace and medical customers "
    "under a documented quality system that is audited every year. "
) * 12


def page(body: str, head: str = "") -> str:
    return f"<html lang='en'><head><title>T</title>{head}</head><body>{body}</body></html>"


# ---------------------------------------------------------------------------
# Sufficiency (SS4)
# ---------------------------------------------------------------------------


def test_sufficient_page_passes() -> None:
    verdict = assess(
        page(f"<main><p>{PROSE}</p></main>"),
        is_homepage=True,
        min_words_homepage=200,
        min_words_internal=120,
    )
    assert verdict.sufficient
    assert verdict.word_count > 200


def test_js_shell_is_detected_not_treated_as_success() -> None:
    """A 200 with an empty root div is the failure mode a status check misses."""
    shell = page("<div id='root'></div><script>render()</script>")
    verdict = assess(shell, is_homepage=True, min_words_homepage=200, min_words_internal=120)
    assert not verdict.sufficient
    assert verdict.reason == "js_shell"
    assert verdict.needs_render


def test_thin_dom_is_distinguished_from_js_shell() -> None:
    verdict = assess(
        page("<main><p>Ten short words here about a company that makes things.</p></main>"),
        is_homepage=True,
        min_words_homepage=200,
        min_words_internal=120,
    )
    assert not verdict.sufficient
    assert verdict.reason == "thin_dom"
    assert verdict.needs_render


def test_internal_pages_use_a_lower_threshold() -> None:
    body = page("<main><p>" + ("word " * 150) + "</p></main>")
    assert not assess(body, is_homepage=True, min_words_homepage=200, min_words_internal=120).sufficient
    assert assess(body, is_homepage=False, min_words_homepage=200, min_words_internal=120).sufficient


@pytest.mark.parametrize(
    "marker",
    [
        "Checking your browser before accessing",
        "Just a moment...",
        "Please enable JavaScript to continue",
        "Attention Required! | Cloudflare",
        "Access denied",
        "Verify you are human",
    ],
)
def test_challenge_pages_detected(marker: str) -> None:
    html = page(f"<main><p>{marker}</p><p>{PROSE}</p></main>")
    assert looks_like_challenge(html)
    verdict = assess(html, is_homepage=True, min_words_homepage=200, min_words_internal=120)
    assert verdict.reason == "challenge"


@pytest.mark.parametrize(
    "marker",
    [
        "This domain is for sale",
        "Buy this domain",
        "Website coming soon",
        "Account Suspended",
        "Under construction",
    ],
)
def test_parked_domains_detected_and_are_terminal(marker: str) -> None:
    html = page(f"<main><p>{marker}</p></main>")
    assert looks_parked(html)
    verdict = assess(html, is_homepage=True, min_words_homepage=200, min_words_internal=120)
    assert verdict.reason == "parked"
    assert not verdict.needs_render  # rendering cannot fix a parked domain


def test_empty_body() -> None:
    verdict = assess("", is_homepage=True, min_words_homepage=200, min_words_internal=120)
    assert verdict.reason == "empty_body"


def test_word_count_ignores_digits_and_single_characters() -> None:
    assert count_words("1 2 3 a b c") == 0
    assert count_words("alpha beta gamma") == 3


def test_visible_text_strips_chrome() -> None:
    html = page(
        "<nav>Home Services About</nav><script>var x=1</script>"
        "<style>.a{}</style><main><p>Real content here.</p></main>"
        "<footer>Copyright</footer>"
    )
    text = visible_text(html)
    assert "Real content here." in text
    assert "Home Services About" not in text
    assert "var x=1" not in text


# ---------------------------------------------------------------------------
# Content extraction (SS8)
# ---------------------------------------------------------------------------


async def test_trafilatura_extracts_and_preserves_headings() -> None:
    html = page(
        "<main><h1>What We Make</h1><p>" + PROSE + "</p>"
        "<h2>Industries We Serve</h2><ul><li>Aerospace</li><li>Medical</li></ul></main>"
    )
    text = await extract_content(html, "https://acme.com/services")
    assert "What We Make" in text
    assert "Industries We Serve" in text
    assert "#" in text  # heading structure survives
    assert "Aerospace" in text


async def test_dom_fallback_handles_pages_trafilatura_under_extracts() -> None:
    """Short marketing pages inside unusual markup still yield content."""
    html = page(
        "<div class='hero'><span>Precision components for aerospace teams.</span></div>"
        "<div class='cards'>"
        + "".join(f"<div><b>Capability {i}</b><span>Detail {i}</span></div>" for i in range(12))
        + "</div>"
    )
    text = await extract_content(html, "https://acme.com/")
    assert "Precision components" in text or "Capability" in text
    assert text.strip()


async def test_extraction_never_invents_content() -> None:
    """Invariant 6: every returned token must appear in the source."""
    html = page("<main><h1>Alpha Beta</h1><p>Gamma delta epsilon zeta.</p></main>")
    text = await extract_content(html, "https://acme.com/")
    source = visible_text(html).lower()
    for word in text.replace("#", " ").replace("-", " ").split():
        assert word.lower().strip(".,:;") in source or not word.isalpha()


async def test_empty_html_yields_empty_text() -> None:
    assert await extract_content("", "https://acme.com/") == ""
    assert await extract_content("   ", "https://acme.com/") == ""


def test_normalize_text_collapses_whitespace_and_control_chars() -> None:
    messy = "Line one   \n\n\n\n   Line two\x00\x07​\r\nLine three"
    cleaned = normalize_text(messy)
    assert "\n\n\n" not in cleaned
    assert "\x00" not in cleaned
    assert "​" not in cleaned
    assert cleaned.startswith("Line one")


def test_truncate_at_paragraph_boundary() -> None:
    text = "Para one is here.\n\nPara two is here.\n\nPara three is here."
    truncated, was_truncated = truncate_at_paragraph(text, 25)
    assert was_truncated
    assert truncated == "Para one is here."
    assert len(truncated) <= 25


def test_truncate_returns_original_when_within_limit() -> None:
    text = "Short."
    truncated, was_truncated = truncate_at_paragraph(text, 100)
    assert truncated == text
    assert not was_truncated


def test_split_blocks() -> None:
    assert split_blocks("a\n\nb\n c \n") == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_metadata_extraction() -> None:
    html = (
        "<html lang='en-GB'><head><title>Services | Acme Ltd</title>"
        "<meta name='description' content='We machine things.'/>"
        "<link rel='canonical' href='https://acme.com/services'/></head>"
        "<body><h1>Our Services</h1><h2>Machining</h2><h2>Inspection</h2>"
        "<h2>Machining</h2></body></html>"
    )
    metadata = extract_metadata(html, max_headings=8, max_heading_chars=60)
    assert metadata.title == "Services | Acme Ltd"
    assert metadata.meta_description == "We machine things."
    assert metadata.h1 == "Our Services"
    assert metadata.headings == ["Our Services", "Machining", "Inspection"]  # deduped
    assert metadata.language == "en-gb"
    assert metadata.canonical == "https://acme.com/services"


def test_headings_are_capped() -> None:
    html = "<html><body>" + "".join(f"<h2>Heading {i}</h2>" for i in range(30)) + "</body></html>"
    metadata = extract_metadata(html, max_headings=8, max_heading_chars=60)
    assert len(metadata.headings) == 8


def test_heading_length_is_capped() -> None:
    html = "<html><body><h2>" + ("x" * 400) + "</h2></body></html>"
    metadata = extract_metadata(html, max_headings=8, max_heading_chars=60)
    assert len(metadata.headings[0]) <= 60


def test_site_name_from_title_separator() -> None:
    metadata = extract_metadata(
        "<html><head><title>Services | Acme Ltd</title></head><body></body></html>",
        max_headings=8,
        max_heading_chars=60,
    )
    assert extract_site_name("<html></html>", metadata) == "Acme Ltd"


def test_site_name_prefers_og_site_name() -> None:
    html = "<html><head><meta property='og:site_name' content='Acme Precision'/></head></html>"
    metadata = extract_metadata(html, max_headings=8, max_heading_chars=60)
    assert extract_site_name(html, metadata) == "Acme Precision"


# ---------------------------------------------------------------------------
# JSON-LD allowlist (SS8)
# ---------------------------------------------------------------------------


FULL_JSONLD = """<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization",
 "name":"Acme Ltd","legalName":"Acme Limited","slogan":"Precision, delivered",
 "description":"A contract manufacturer.","foundingDate":"1998",
 "numberOfEmployees":{"@type":"QuantitativeValue","value":120},
 "address":{"@type":"PostalAddress","streetAddress":"1200 Secret Road",
            "addressLocality":"Austin","addressRegion":"TX","addressCountry":"US"},
 "areaServed":["North America","EMEA","APAC","LATAM"],
 "knowsAbout":["CNC","AS9100","ISO13485","Anodising","Inspection","Extra"],
 "sameAs":["https://linkedin.com/company/acme"],
 "logo":"https://cdn/logo.png","image":"https://cdn/x.png",
 "contactPoint":{"telephone":"+1"},
 "aggregateRating":{"ratingValue":"4.9","reviewCount":"210"},
 "review":[{"author":"Someone","reviewBody":"Great"}]}
</script>"""


def test_jsonld_allowlisted_fields_only() -> None:
    organization, name, tagline = extract_organization(
        iter_jsonld(FULL_JSONLD), description_cap=300, total_cap=500
    )
    assert name == "Acme Ltd"
    assert tagline == "Precision, delivered"
    assert organization is not None
    assert organization.legal_name == "Acme Limited"
    assert organization.founding_date == "1998"
    assert organization.employees == "120"  # QuantitativeValue unwrapped


def test_jsonld_third_party_review_data_is_excluded() -> None:
    """A correctness requirement, not a size optimisation: reviews are not first-party."""
    organization, _, _ = extract_organization(
        iter_jsonld(FULL_JSONLD), description_cap=300, total_cap=500
    )
    dumped = organization.model_dump_json()
    for banned in ("4.9", "210", "Great", "Someone", "linkedin.com", "logo.png"):
        assert banned not in dumped
    assert "aggregaterating" in DISCARDED_FIELDS
    assert "review" in DISCARDED_FIELDS
    assert "sameas" in DISCARDED_FIELDS


def test_jsonld_street_address_is_not_extracted() -> None:
    organization, _, _ = extract_organization(
        iter_jsonld(FULL_JSONLD), description_cap=300, total_cap=500
    )
    assert organization.locations == ["Austin, TX, US"]
    assert "Secret Road" not in organization.model_dump_json()


def test_jsonld_list_fields_are_capped() -> None:
    organization, _, _ = extract_organization(
        iter_jsonld(FULL_JSONLD), description_cap=300, total_cap=500
    )
    assert len(organization.area_served) <= 3
    assert len(organization.knows_about) <= 5


def test_jsonld_total_cap_enforced() -> None:
    organization, _, _ = extract_organization(
        iter_jsonld(FULL_JSONLD), description_cap=300, total_cap=120
    )
    assert len(organization.model_dump_json(exclude_none=True)) <= 200


def test_jsonld_ignores_non_organization_types() -> None:
    html = """<script type="application/ld+json">
    {"@type":"Product","name":"Widget","description":"A widget."}</script>"""
    organization, name, _ = extract_organization(
        iter_jsonld(html), description_cap=300, total_cap=500
    )
    assert organization is None
    assert name is None


def test_jsonld_graph_is_traversed() -> None:
    html = """<script type="application/ld+json">
    {"@graph":[{"@type":"WebSite"},{"@type":"Organization","name":"Graph Co"}]}</script>"""
    _, name, _ = extract_organization(iter_jsonld(html), description_cap=300, total_cap=500)
    assert name == "Graph Co"


def test_malformed_jsonld_is_skipped_silently() -> None:
    html = '<script type="application/ld+json">{not json,,,}</script>'
    assert iter_jsonld(html) == []
    organization, _, _ = extract_organization([], description_cap=300, total_cap=500)
    assert organization is None


# ---------------------------------------------------------------------------
# Cross-page boilerplate (SS8)
# ---------------------------------------------------------------------------


def test_boilerplate_removed_and_preserved_once() -> None:
    pages = [
        ["Unique to page one.", "Copyright Acme Ltd.", "Book a call today."],
        ["Unique to page two.", "Copyright Acme Ltd.", "Book a call today."],
        ["Unique to page three.", "Copyright Acme Ltd.", "Book a call today."],
        ["Unique to page four.", "Copyright Acme Ltd.", "Book a call today."],
    ]
    result = remove_boilerplate(
        pages, min_pages=4, document_frequency=0.6, max_block_chars=400, site_wide_cap=1500
    )
    assert result.removed_block_count == 2
    for text in result.pages:
        assert "Copyright Acme Ltd." not in text
        assert "Book a call today." not in text
        assert "Unique to page" in text
    assert result.site_wide_content.count("Copyright Acme Ltd.") == 1
    assert "Book a call today." in result.site_wide_content


def test_boilerplate_skipped_below_minimum_page_count() -> None:
    pages = [["Shared.", "One."], ["Shared.", "Two."]]
    result = remove_boilerplate(
        pages, min_pages=4, document_frequency=0.6, max_block_chars=400, site_wide_cap=1500
    )
    assert result.removed_block_count == 0
    assert result.site_wide_content is None
    assert "Shared." in result.pages[0]


def test_long_shared_blocks_are_kept_in_place() -> None:
    """A substantial repeated passage is more likely content than chrome."""
    long_block = "A" * 500
    pages = [[long_block, f"Unique {i}"] for i in range(5)]
    result = remove_boilerplate(
        pages, min_pages=4, document_frequency=0.6, max_block_chars=400, site_wide_cap=1500
    )
    assert all(long_block in text for text in result.pages)


def test_boilerplate_threshold_respected() -> None:
    """A block on 2 of 5 pages (40%) is below the 60% threshold and stays."""
    pages = [
        ["Sometimes here.", "Unique one."],
        ["Sometimes here.", "Unique two."],
        ["Unique three."],
        ["Unique four."],
        ["Unique five."],
    ]
    result = remove_boilerplate(
        pages, min_pages=4, document_frequency=0.6, max_block_chars=400, site_wide_cap=1500
    )
    assert "Sometimes here." in result.pages[0]


def test_site_wide_content_is_capped() -> None:
    shared = [f"Shared line number {i} with some padding text." for i in range(80)]
    pages = [[*shared, f"Unique {i}"] for i in range(5)]
    result = remove_boilerplate(
        pages, min_pages=4, document_frequency=0.6, max_block_chars=400, site_wide_cap=300
    )
    assert len(result.site_wide_content) <= 300
