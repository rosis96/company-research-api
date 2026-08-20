"""URL normalization, deduplication and exclusion tests (SS6)."""

from __future__ import annotations

import pytest

from app.crawl.normalize import (
    deduplicate,
    evaluate_exclusions,
    has_date_in_path,
    is_careers_overview,
    locale_of,
    normalize_url,
    path_segments,
    term_variants,
)

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM/", "http://example.com/"),
        ("https://EXAMPLE.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com//a///b//", "https://example.com/a/b"),
        ("https://example.com/a/index.html", "https://example.com/a"),
        ("https://example.com/index.php", "https://example.com/"),
        ("https://example.com/Default.ASPX", "https://example.com/"),
        ("https://example.com/a?", "https://example.com/a"),
        ("https://example.com/dir/./", "https://example.com/dir/."),
    ],
)
def test_normalization_rules(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "base", "expected"),
    [
        ("/services", "https://example.com/a/b", "https://example.com/services"),
        ("services", "https://example.com/a/", "https://example.com/a/services"),
        ("../about", "https://example.com/a/b/c", "https://example.com/a/about"),
        ("//cdn.example.com/x", "https://example.com/", "https://cdn.example.com/x"),
        ("?page=2", "https://example.com/list", "https://example.com/list?page=2"),
    ],
)
def test_relative_resolution(raw: str, base: str, expected: str) -> None:
    assert normalize_url(raw, base=base) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/a?utm_source=x", "https://example.com/a"),
        ("https://example.com/a?utm_medium=x&id=7", "https://example.com/a?id=7"),
        ("https://example.com/a?gclid=x&fbclid=y", "https://example.com/a"),
        ("https://example.com/a?mc_cid=1&_hsenc=2&msclkid=3", "https://example.com/a"),
        ("https://example.com/a?ref=partner", "https://example.com/a"),
        ("https://example.com/a?id=1&kind=2", "https://example.com/a?id=1&kind=2"),
    ],
)
def test_tracking_parameters_removed(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_query_parameter_order_is_canonical() -> None:
    """?b=2&a=1 and ?a=1&b=2 must dedupe to one URL."""
    assert normalize_url("https://e.com/x?b=2&a=1") == normalize_url("https://e.com/x?a=1&b=2")


@pytest.mark.parametrize(
    "raw",
    [
        "mailto:hello@example.com",
        "tel:+15125550134",
        "sms:+15125550134",
        "javascript:void(0)",
        "data:text/html,<h1>x</h1>",
        "ftp://example.com/f",
        "file:///etc/passwd",
        "#anchor",
        "",
        "   ",
    ],
)
def test_unfetchable_urls_return_none(raw: str) -> None:
    assert normalize_url(raw) is None


def test_credentials_rejected_during_normalization() -> None:
    assert normalize_url("https://user:pw@example.com/") is None


def test_deduplicate_preserves_order() -> None:
    urls = ["https://a.com/1", "https://a.com/2", "https://a.com/1", "https://a.com/3"]
    assert deduplicate(urls) == ["https://a.com/1", "https://a.com/2", "https://a.com/3"]


def test_trailing_slash_variants_collapse() -> None:
    variants = [
        "https://example.com/about",
        "https://example.com/about/",
        "https://example.com/about/index.html",
        "https://example.com//about//",
    ]
    assert len({normalize_url(v) for v in variants}) == 1


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason_fragment"),
    [
        ("https://e.com/login", "blocked_segment"),
        ("https://e.com/sign-up", "blocked_segment"),
        ("https://e.com/my-account/settings", "blocked_segment"),
        ("https://e.com/cart", "blocked_segment"),
        ("https://e.com/checkout", "blocked_segment"),
        ("https://e.com/privacy-policy", "blocked_segment"),
        ("https://e.com/terms", "blocked_segment"),
        ("https://e.com/cookie-policy", "blocked_segment"),
        ("https://e.com/gdpr", "blocked_segment"),
        ("https://e.com/search", "blocked_segment"),
        ("https://e.com/tag/manufacturing", "blocked_segment"),
        ("https://e.com/category/news", "blocked_segment"),
        ("https://e.com/author/jane", "blocked_segment"),
        ("https://e.com/wp-admin/", "blocked_path_substring"),
        ("https://e.com/wp-json/wp/v2", "blocked_path_substring"),
        ("https://e.com/feed/", "blocked_path_substring"),
        ("https://e.com/brochure.pdf", "blocked_extension"),
        ("https://e.com/logo.svg", "blocked_extension"),
        ("https://e.com/app.js", "blocked_extension"),
        ("https://e.com/blog/page/3", "blocked_segment"),
        ("https://e.com/insights?page=3", "pagination"),
        ("https://app.e.com/dashboard", "excluded_subdomain"),
        ("https://careers.e.com/roles", "excluded_subdomain"),
        ("https://cdn.e.com/x", "excluded_subdomain"),
        ("https://linkedin.com/company/e", "blocked_domain"),
        ("https://greenhouse.io/e/jobs", "blocked_domain"),
        ("https://g2.com/products/e", "blocked_domain"),
        ("https://calendly.com/e/demo", "blocked_domain"),
    ],
)
def test_excluded_urls(url: str, reason_fragment: str) -> None:
    verdict = evaluate_exclusions(url)
    assert verdict.excluded, url
    assert reason_fragment in verdict.reason


@pytest.mark.parametrize(
    "url",
    [
        "https://e.com/services",
        "https://e.com/case-studies",
        "https://e.com/case-studies/boeing",
        "https://e.com/about-us",
        "https://e.com/industries/aerospace",
        "https://e.com/pricing",
        "https://e.com/work/12345-project",
        "https://solutions.e.com/manufacturing",
        "https://e.com/blog/why-tolerances-matter",
    ],
)
def test_allowed_urls(url: str) -> None:
    assert not evaluate_exclusions(url).excluded, url


# ---------------------------------------------------------------------------
# Careers: overview allowed, listings excluded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://e.com/careers",
        "https://e.com/career",
        "https://e.com/culture",
        "https://e.com/our-culture",
        "https://e.com/life-at-acme",
        "https://e.com/working-at-acme",
        "https://e.com/join-us",
        "https://e.com/work-with-us",
        "https://e.com/why-work-here",
    ],
)
def test_careers_overview_allowed(url: str) -> None:
    assert is_careers_overview(url)
    assert not evaluate_exclusions(url).excluded


@pytest.mark.parametrize(
    "url",
    [
        "https://e.com/careers/senior-machinist",
        "https://e.com/careers/openings/12345",
        "https://e.com/culture/team/jane",
        "https://e.com/jobs",
        "https://e.com/jobs/2345",
        "https://e.com/positions/engineer",
        "https://e.com/apply/12345",
        "https://e.com/life-at-acme/engineering",
    ],
)
def test_careers_listings_excluded(url: str) -> None:
    assert not is_careers_overview(url)
    assert evaluate_exclusions(url).excluded, url


# ---------------------------------------------------------------------------
# Locale and date detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://e.com/de/services", "de"),
        ("https://e.com/fr-fr/about", "fr-fr"),
        ("https://de.e.com/services", "de"),
        ("https://e.com/services", None),
        ("https://e.com/en/services", "en"),
    ],
)
def test_locale_detection(url: str, expected: str | None) -> None:
    assert locale_of(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://e.com/blog/2019/05/post", True),
        ("https://e.com/news/2024/update", True),
        ("https://e.com/2020/", True),
        ("https://e.com/services", False),
        ("https://e.com/product-2000x", False),
    ],
)
def test_date_in_path(url: str, expected: bool) -> None:
    assert has_date_in_path(url) is expected


def test_path_segments() -> None:
    assert path_segments("https://e.com/a/b/c") == ["a", "b", "c"]
    assert path_segments("https://e.com/") == []


# ---------------------------------------------------------------------------
# Term variants (variation-tolerant matching, SS7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("case-studies", "case-study"),
        ("Case Studies", "case-study"),
        ("case_studies", "case-study"),
        ("industries", "industry"),
        ("services", "service"),
        ("our-services", "our-service"),
        ("Success Stories", "success-story"),
        ("SOLUTIONS", "solution"),
    ],
)
def test_term_variants_are_variation_tolerant(text: str, expected: str) -> None:
    assert expected in term_variants(text)


def test_term_variants_of_empty_text() -> None:
    assert term_variants("") == set()
    assert term_variants("   ") == set()
