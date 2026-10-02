from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ad_research.classifier import classify_overall
from ad_research.detector import (
    AdvertisingDetector,
    _accept_cookie_consent_in_root,
    _blocked_after_homepage,
    _candidate_seed_urls,
    _looks_like_bot_challenge,
)
from ad_research.models import (
    AnalysisResult,
    Evidence,
    Observation,
    PageDiagnostic,
    PlatformResult,
)
from ad_research.qualification import enrich_ad_infrastructure_result
from ad_research.settings import AdResearchSettings
from ad_research.signature_engine import SignatureEngine
from app.security.reasons import ReasonCode
from app.security.url_guard import GuardError, GuardPolicy, UrlGuard

SIGNATURES = Path(__file__).parents[1] / "ad_research" / "signatures"


def obs(kind: str, value: str, phase: str = "before_consent", page: str = "https://site.test/"):
    return Observation(
        kind=kind,
        value=value,
        page_url=page,
        phase=phase,
        url=value if kind in {"network", "script"} else None,
    )


def test_ga4_only_is_not_google_ads() -> None:
    engine = SignatureEngine(SIGNATURES)
    platforms, gtm = engine.detect(
        [
            obs("script", "https://www.googletagmanager.com/gtag/js?id=G-ABC123"),
            obs("network", "https://www.google-analytics.com/g/collect?v=2&tid=G-ABC123"),
            obs("dom", "gtag('config', 'G-ABC123')"),
        ]
    )
    assert platforms["google_ads"].status == "NOT_DETECTED"
    assert not gtm.detected


def test_gtm_only_is_tag_management_not_paid_advertising() -> None:
    engine = SignatureEngine(SIGNATURES)
    platforms, gtm = engine.detect(
        [obs("script", "https://www.googletagmanager.com/gtm.js?id=GTM-AB12CD")]
    )
    confidence, _ = classify_overall(platforms, crawl_conclusive=True)
    assert gtm.detected
    assert gtm.container_ids == ["GTM-AB12CD"]
    assert confidence == "NONE"


def test_google_ads_aw_identifier_is_likely_without_runtime_event() -> None:
    engine = SignatureEngine(SIGNATURES)
    platforms, _ = engine.detect(
        [obs("script", "https://www.googletagmanager.com/gtag/js?id=AW-123456789")]
    )
    assert platforms["google_ads"].status == "LIKELY"
    assert platforms["google_ads"].identifiers == ["AW-123456789"]


def test_generic_linkedin_session_cookies_are_not_ad_evidence() -> None:
    engine = SignatureEngine(SIGNATURES)
    platforms, _ = engine.detect([obs("cookie", "bcookie"), obs("cookie", "lidc")])
    assert platforms["linkedin"].status == "NOT_DETECTED"


def test_meta_runtime_event_is_confirmed_and_phase_is_preserved() -> None:
    engine = SignatureEngine(SIGNATURES)
    platforms, _ = engine.detect(
        [
            obs(
                "network",
                "https://www.facebook.com/tr?id=123456789&ev=PageView",
                "after_consent",
            ),
            obs("cookie", "_fbp", "after_consent"),
        ]
    )
    meta = platforms["meta"]
    assert meta.status == "CONFIRMED"
    assert meta.identifiers == ["123456789"]
    assert not meta.detected_before_consent
    assert meta.detected_after_consent
    assert any(item.phase == "after_consent" for item in meta.evidence)


def test_repeated_confirmed_runtime_across_pages_can_be_high() -> None:
    evidence = [
        Evidence(
            kind="network",
            value="event",
            url="https://x",
            page_url=page,
            phase="before_consent",
            strength="strong",
        )
        for page in ("https://site.test/", "https://site.test/pricing")
    ]
    platforms = {"meta": PlatformResult(status="CONFIRMED", detected=True, evidence=evidence)}
    assert classify_overall(platforms, crawl_conclusive=True) == (
        "HIGH",
        "MAY_NOT_NEED_TO_ASK",
    )


def test_failure_can_never_become_none() -> None:
    assert classify_overall({}, crawl_conclusive=False) == ("INCONCLUSIVE", "ASK_SPEND")


def test_no_browser_signal_still_recommends_spend_verification() -> None:
    assert classify_overall({}, crawl_conclusive=True) == ("NONE", "ASK_SPEND")


def test_confirmed_linkedin_evidence_becomes_concise_infrastructure_reasons() -> None:
    now = datetime.now(UTC)
    result = AnalysisResult(
        domain="futuretodayinc.com",
        requested_url="https://futuretodayinc.com/",
        final_url="https://futuretodayinc.com/",
        status="COMPLETED",
        started_at=now,
        completed_at=now,
        pages_crawled=2,
        advertising_infrastructure_confidence="MEDIUM",
        recommendation="ASK_SPEND",
        platforms={
            "linkedin": PlatformResult(
                status="CONFIRMED",
                detected=True,
                evidence=[
                    Evidence(
                        kind="network",
                        value="LinkedIn Insight event request",
                        url="https://px.ads.linkedin.com/collect",
                        page_url="https://futuretodayinc.com/",
                        phase="before_consent",
                        strength="strong",
                    ),
                    Evidence(
                        kind="network",
                        value="LinkedIn Insight event request",
                        url="https://px.ads.linkedin.com/collect",
                        page_url="https://futuretodayinc.com/contact-us",
                        phase="before_consent",
                        strength="strong",
                    ),
                    Evidence(
                        kind="script",
                        value="LinkedIn Insight script",
                        url="https://snap.licdn.com/li.lms-analytics/insight.min.js",
                        page_url="https://futuretodayinc.com/",
                        phase="before_consent",
                        strength="medium",
                    ),
                ],
            )
        },
    )
    enrich_ad_infrastructure_result(result)
    assert result.ad_infrastructure_result == "AD INFRA DETECTED"
    assert len(result.evidence_summaries) == 3
    assert "px.ads.linkedin.com/collect" in result.evidence_summaries[0]
    assert "homepage" in result.evidence_summaries[0]
    assert "/contact-us" in result.evidence_summaries[1]
    assert "before any crawler consent action" in result.evidence_summaries[0]


def test_cloudflare_failure_is_explained_and_remains_inconclusive() -> None:
    now = datetime.now(UTC)
    result = AnalysisResult(
        domain="site.test",
        requested_url="https://site.test",
        final_url="https://site.test/?__cf_chl_rt_tk=abc",
        status="INCONCLUSIVE",
        started_at=now,
        completed_at=now,
        pages=[
            PageDiagnostic(
                url="https://site.test/",
                final_url="https://site.test/?__cf_chl_rt_tk=abc",
                status_code=403,
                title="Just a moment...",
                error="HTTP 403",
            )
        ],
        error="HTTP 403",
    )
    enrich_ad_infrastructure_result(result)
    assert result.ad_infrastructure_result == "INCONCLUSIVE"
    assert "Cloudflare" in result.evidence_summaries[0]


async def test_invalid_or_private_url_is_structured_failure() -> None:
    detector = AdvertisingDetector(
        AdResearchSettings(),
        browser=None,  # validation fails before a browser can be used
        guard=UrlGuard(GuardPolicy()),
        signatures=SignatureEngine(SIGNATURES),
    )
    result = await detector.analyze("http://127.0.0.1")
    assert result.status == "CRAWL_FAILED"
    assert result.advertising_infrastructure_confidence == "INCONCLUSIVE"
    assert result.recommendation == "ASK_SPEND"


def test_homepage_candidates_cover_protocol_and_www_fallbacks() -> None:
    candidates = _candidate_seed_urls("example.com/some-page")
    assert candidates == [
        "https://example.com/some-page",
        "https://example.com/",
        "http://example.com/",
        "https://www.example.com/",
        "http://www.example.com/",
    ]


async def test_missing_dns_is_reported_as_no_website() -> None:
    class MissingDnsGuard:
        async def validate(self, raw_url: str, scope=None):
            host = urlsplit(raw_url).hostname or ""
            raise GuardError(ReasonCode.DNS_FAILURE, f"{host} did not resolve")

    detector = AdvertisingDetector(
        AdResearchSettings(),
        browser=None,
        guard=MissingDnsGuard(),
        signatures=SignatureEngine(SIGNATURES),
    )
    result = await detector.analyze("missing-example.test")
    assert result.status == "NO_WEBSITE"
    assert result.ad_infrastructure_result == "NO WEBSITE"
    assert result.primary_advertising_confidence == "NO WEBSITE"
    assert result.recommendation == "LOW_PRIORITY"


async def test_blank_website_is_reported_as_no_website() -> None:
    detector = AdvertisingDetector(
        AdResearchSettings(),
        browser=None,
        guard=UrlGuard(GuardPolicy()),
        signatures=SignatureEngine(SIGNATURES),
    )
    result = await detector.analyze(" ")
    assert result.status == "NO_WEBSITE"


async def test_generic_consent_click_requires_cookie_context() -> None:
    unsafe = _FakeRoot(context_is_consent=False)
    safe = _FakeRoot(context_is_consent=True)
    assert await _accept_cookie_consent_in_root(unsafe) is None
    assert not unsafe.button.clicked
    assert await _accept_cookie_consent_in_root(safe) == "Accept All"
    assert safe.button.clicked


class _MissingLocator:
    first = None

    def __init__(self) -> None:
        self.first = self

    async def is_visible(self, timeout=0):
        return False


class _FakeButton:
    def __init__(self, context_is_consent: bool) -> None:
        self.context_is_consent = context_is_consent
        self.clicked = False

    async def inner_text(self):
        return "Accept All"

    async def evaluate(self, _script):
        return self.context_is_consent

    async def is_visible(self, timeout=0):
        return True

    async def click(self, timeout=0):
        self.clicked = True


class _FakeButtons:
    def __init__(self, button: _FakeButton) -> None:
        self.button = button

    def filter(self, **_kwargs):
        return self

    async def count(self):
        return 1

    def nth(self, _index):
        return self.button


class _FakeRoot:
    def __init__(self, context_is_consent: bool) -> None:
        self.button = _FakeButton(context_is_consent)

    def locator(self, _selector):
        return _MissingLocator()

    def get_by_role(self, _role):
        return _FakeButtons(self.button)


def test_heavy_resource_filter_keeps_advertising_pixels() -> None:
    engine = SignatureEngine(SIGNATURES)
    assert engine.matches_network("https://www.facebook.com/tr?id=123&ev=PageView")
    assert engine.matches_network("https://googleads.g.doubleclick.net/pagead/viewthroughconversion/1/")
    assert not engine.matches_network("https://cdn.site.test/hero.jpg")


def test_cdn_block_pages_served_with_200_are_bot_challenges() -> None:
    assert _looks_like_bot_challenge("ERROR: The request could not be satisfied", "")
    assert _looks_like_bot_challenge("Access Denied", "<p>https://errors.edgesuite.net/18.1</p>")
    assert not _looks_like_bot_challenge("Eyewear Online", "<html>shop</html>")


def test_homepage_only_crawl_with_refused_pages_is_detected() -> None:
    home = PageDiagnostic(url="https://site.test/", page_role="homepage")
    refused = [PageDiagnostic(url=f"https://site.test/{n}", error="HTTP 403") for n in range(3)]
    missing = [PageDiagnostic(url=f"https://site.test/{n}", error="HTTP 404") for n in range(3)]
    assert _blocked_after_homepage([home, *refused])
    # A missing /pricing page is not a block, and one refusal is not a pattern.
    assert not _blocked_after_homepage([home, *missing])
    assert not _blocked_after_homepage([home, refused[0]])
    assert not _blocked_after_homepage([home, refused[0], PageDiagnostic(url="https://site.test/ok")])
