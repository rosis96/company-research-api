from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from ad_research.models import (
    ActivityEvidenceRecord,
    ActivitySummary,
    AnalysisResult,
    CombinedActivityConfidence,
    DomainHistorySummary,
    IdentifierHistory,
)


def apply_infrastructure_freshness(result: AnalysisResult) -> AnalysisResult:
    """Describe how current the website evidence is without implying ad spend."""
    if result.status != "COMPLETED":
        result.infrastructure_freshness = "INCONCLUSIVE"
        result.runtime_platforms = []
        return result

    runtime_platforms: list[str] = []
    has_loaded_tag = False
    has_source_marker = False
    has_cookie = False
    for key, platform in result.platforms.items():
        runtime = any(
            item.kind == "network" and item.strength == "strong"
            for item in platform.evidence
        )
        loaded = any(
            item.kind in {"network", "script", "global"} and item.strength == "medium"
            for item in platform.evidence
        )
        source = any(item.kind in {"dom", "identifier"} for item in platform.evidence)
        cookie = any(item.kind == "cookie" for item in platform.evidence)
        platform.runtime_event_observed = runtime
        platform.pages_observed = sorted({item.page_url for item in platform.evidence})
        if runtime:
            platform.evidence_tier = "RUNTIME_EVENT"
            runtime_platforms.append(key)
        elif loaded:
            platform.evidence_tier = "LOADED_TAG"
        elif source:
            platform.evidence_tier = "SOURCE_MARKER"
        elif cookie:
            platform.evidence_tier = "COOKIE_ONLY"
        else:
            platform.evidence_tier = "NOT_OBSERVED"
        has_loaded_tag = has_loaded_tag or loaded
        has_source_marker = has_source_marker or source
        has_cookie = has_cookie or cookie

    result.runtime_platforms = sorted(runtime_platforms)
    if runtime_platforms:
        result.infrastructure_freshness = "RUNTIME_ACTIVE"
    elif has_loaded_tag:
        result.infrastructure_freshness = "TAG_LOADED"
    elif has_source_marker:
        result.infrastructure_freshness = "SOURCE_ONLY"
    elif has_cookie:
        result.infrastructure_freshness = "COOKIE_ONLY"
    else:
        result.infrastructure_freshness = "NOT_OBSERVED"
    return result


def build_history(scans: list[dict]) -> DomainHistorySummary:
    if not scans:
        return DomainHistorySummary()
    ordered = sorted(scans, key=lambda item: _aware(item["observed_at"]))
    completed = [item for item in ordered if item["status"] == "COMPLETED"]
    detected = [
        item for item in ordered if item["infrastructure_result"] == "AD INFRA DETECTED"
    ]
    consecutive = 0
    for item in reversed(ordered):
        if item["infrastructure_result"] != "AD INFRA DETECTED":
            break
        consecutive += 1

    runtime_dates = [
        _aware(item["observed_at"])
        for item in ordered
        if item["infrastructure_freshness"] == "RUNTIME_ACTIVE"
    ]
    identifier_dates: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for item in ordered:
        observed_at = _aware(item["observed_at"])
        for platform, identifiers in item.get("identifiers", {}).items():
            for identifier in identifiers:
                identifier_dates[(platform, identifier)].append(observed_at)

    identifiers = [
        IdentifierHistory(
            platform=platform,
            identifier=identifier,
            first_seen_at=min(dates),
            last_seen_at=max(dates),
            scans_seen=len(dates),
        )
        for (platform, identifier), dates in sorted(identifier_dates.items())
    ]
    return DomainHistorySummary(
        total_scans=len(ordered),
        completed_scans=len(completed),
        scans_with_infrastructure=len(detected),
        consecutive_scans_with_infrastructure=consecutive,
        first_scan_at=_aware(ordered[0]["observed_at"]),
        last_scan_at=_aware(ordered[-1]["observed_at"]),
        last_runtime_signal_at=max(runtime_dates) if runtime_dates else None,
        identifiers=identifiers,
    )


def summarize_activity(
    evidence: list[ActivityEvidenceRecord], *, now: datetime | None = None
) -> ActivitySummary:
    now = _aware(now or datetime.now(UTC))
    if not evidence:
        return ActivitySummary(
            confidence="INCONCLUSIVE",
            reasons=["No external ad-library evidence has been recorded yet."],
        )

    active_sources: set[str] = set()
    recent_sources: set[str] = set()
    possible_sources: set[str] = set()
    for item in evidence:
        age = now - _aware(item.observed_at)
        last_ad_age = _date_age(item.last_ad_seen_at, now.date())
        if item.status == "ACTIVE" and (age <= timedelta(days=45) or last_ad_age <= 45):
            active_sources.add(item.source)
        elif item.status in {"ACTIVE", "RECENT"} and (
            age <= timedelta(days=120) or last_ad_age <= 120
        ):
            recent_sources.add(item.source)
        elif item.status in {"ACTIVE", "RECENT", "HISTORICAL"}:
            possible_sources.add(item.source)

    positive_sources = active_sources | recent_sources | possible_sources
    counts = [item.active_ads_count for item in evidence if item.active_ads_count is not None]
    newest_dates = [item.last_ad_seen_at for item in evidence if item.last_ad_seen_at]
    reasons: list[str] = []
    if active_sources:
        confidence = "CONFIRMED_CURRENT"
        reasons.append(
            f"Current activity was recorded from {len(active_sources)} external source(s)."
        )
    elif recent_sources:
        confidence = "RECENT_ACTIVITY"
        reasons.append(
            f"Recent activity was recorded from {len(recent_sources)} external source(s)."
        )
    elif possible_sources:
        confidence = "POSSIBLE"
        reasons.append("Only historical or stale external activity evidence is available.")
    elif all(item.status == "NOT_FOUND" for item in evidence):
        confidence = "NOT_FOUND"
        reasons.append("No ads were found in the checked sources; this is not proof of no ads.")
    else:
        confidence = "INCONCLUSIVE"
        reasons.append("The external activity checks were inconclusive.")

    return ActivitySummary(
        confidence=confidence,
        sources_checked=len({item.source for item in evidence}),
        sources_with_activity=len(positive_sources),
        active_ads_count=max(counts) if counts else None,
        newest_ad_seen_at=max(newest_dates) if newest_dates else None,
        last_checked_at=max(_aware(item.observed_at) for item in evidence),
        evidence=sorted(evidence, key=lambda item: _aware(item.observed_at), reverse=True),
        reasons=reasons,
    )


def attach_research_context(
    result: AnalysisResult,
    history: DomainHistorySummary,
    activity: ActivitySummary,
) -> AnalysisResult:
    apply_infrastructure_freshness(result)
    result.history = history
    result.external_activity = activity
    confidence, reasons = combine_confidence(result, activity)
    result.combined_activity_confidence = confidence
    result.combined_confidence_reasons = reasons
    result.recommendation = "MAY_NOT_NEED_TO_ASK" if confidence == "HIGH" else "ASK_SPEND"
    return result


def combine_confidence(
    result: AnalysisResult, activity: ActivitySummary
) -> tuple[CombinedActivityConfidence, list[str]]:
    infra = result.advertising_infrastructure_confidence
    freshness = result.infrastructure_freshness
    current = activity.confidence == "CONFIRMED_CURRENT"
    recent = activity.confidence == "RECENT_ACTIVITY"
    reasons: list[str] = []

    if current and (infra in {"HIGH", "MEDIUM"} or freshness == "RUNTIME_ACTIVE"):
        reasons.extend(
            [
                "Current external advertising activity was found.",
                "Advertising-specific website infrastructure is operational now.",
            ]
        )
        return "HIGH", reasons
    if current:
        return "MEDIUM", [
            "Current external advertising activity was found, but website evidence is weak or unavailable."
        ]
    if recent and (infra in {"HIGH", "MEDIUM"} or freshness == "RUNTIME_ACTIVE"):
        return "MEDIUM", [
            "Recent external activity and operational website infrastructure agree."
        ]
    if infra == "HIGH" and freshness == "RUNTIME_ACTIVE":
        return "MEDIUM", [
            "Strong advertising infrastructure fired during this crawl, but current ads were not externally confirmed."
        ]
    if recent or freshness == "RUNTIME_ACTIVE" or infra in {"MEDIUM", "LOW"}:
        return "LOW", [
            "Some current or recent advertising evidence exists, but activity is not confirmed strongly enough."
        ]
    if activity.confidence == "POSSIBLE" or freshness in {
        "TAG_LOADED",
        "SOURCE_ONLY",
        "COOKIE_ONLY",
    }:
        return "LOW", ["Only supporting or potentially stale advertising evidence was found."]
    return "INCONCLUSIVE", [
        "Current advertising activity cannot be determined from the available evidence."
    ]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _date_age(value: date | None, today: date) -> int:
    return (today - value).days if value else 10_000
