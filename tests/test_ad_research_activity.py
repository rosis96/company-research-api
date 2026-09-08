from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from ad_research.activity import attach_research_context, summarize_activity
from ad_research.models import (
    ActivityEvidenceCreate,
    AnalysisResult,
    DomainHistorySummary,
    Evidence,
    PlatformResult,
)
from ad_research.qualification import enrich_ad_infrastructure_result
from ad_research.store import CrawlStore


def runtime_result(domain: str) -> AnalysisResult:
    now = datetime.now(UTC)
    result = AnalysisResult(
        domain=domain,
        requested_url=f"https://{domain}",
        final_url=f"https://{domain}/",
        status="COMPLETED",
        started_at=now,
        completed_at=now,
        pages_crawled=2,
        advertising_infrastructure_confidence="HIGH",
        platforms={
            "meta": PlatformResult(
                status="CONFIRMED",
                detected=True,
                identifiers=["123456789"],
                evidence=[
                    Evidence(
                        kind="network",
                        value="Meta pixel event request",
                        url="https://www.facebook.com/tr/?id=123456789&ev=PageView",
                        page_url=f"https://{domain}/",
                        phase="before_consent",
                        strength="strong",
                    )
                ],
            )
        },
    )
    return enrich_ad_infrastructure_result(result)


def test_source_only_code_never_becomes_current_activity() -> None:
    now = datetime.now(UTC)
    result = AnalysisResult(
        domain="stale.test",
        requested_url="https://stale.test",
        status="COMPLETED",
        started_at=now,
        completed_at=now,
        pages_crawled=1,
        advertising_infrastructure_confidence="LOW",
        platforms={
            "meta": PlatformResult(
                status="LIKELY",
                detected=True,
                evidence=[
                    Evidence(
                        kind="dom",
                        value="Meta Pixel source marker",
                        page_url="https://stale.test/",
                        phase="before_consent",
                        strength="medium",
                    )
                ],
            )
        },
    )
    enrich_ad_infrastructure_result(result)
    attach_research_context(result, DomainHistorySummary(), summarize_activity([]))
    assert result.infrastructure_freshness == "SOURCE_ONLY"
    assert result.external_activity.confidence == "INCONCLUSIVE"
    assert result.combined_activity_confidence == "LOW"


def test_history_and_external_activity_raise_confidence_auditably(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'activity.db'}")
    store.create_schema()
    store.save_result(runtime_result("brand.test"))
    store.save_result(runtime_result("www.brand.test"))
    store.add_activity_evidence(
        ActivityEvidenceCreate(
            domain="brand.test",
            source="meta_ad_library",
            status="ACTIVE",
            active_ads_count=12,
            last_ad_seen_at=date.today(),
            landing_page_urls=["https://offers.brand.test/summer"],
            source_url="https://www.facebook.com/ads/library/",
        )
    )

    research = store.get_domain_research("https://www.brand.test")
    assert research.history.total_scans == 2
    assert research.history.consecutive_scans_with_infrastructure == 2
    assert research.history.last_runtime_signal_at is not None
    assert research.external_activity.confidence == "CONFIRMED_CURRENT"
    assert research.external_activity.active_ads_count == 12
    assert research.combined_activity_confidence == "HIGH"
    assert store.get_landing_urls("brand.test") == ["https://offers.brand.test/summer"]
    store.dispose()


def test_old_active_claim_decays_instead_of_staying_current() -> None:
    old = datetime.now(UTC) - timedelta(days=200)
    evidence = ActivityEvidenceCreate(
        domain="brand.test",
        source="manual_verified",
        status="ACTIVE",
        observed_at=old,
    )
    # A stored record adds identity metadata but preserves the old observation date.
    from ad_research.models import ActivityEvidenceRecord

    summary = summarize_activity(
        [
            ActivityEvidenceRecord(
                **evidence.model_dump(),
                id="act_old",
                domain_key="brand.test",
                created_at=old,
            )
        ]
    )
    assert summary.confidence == "POSSIBLE"
