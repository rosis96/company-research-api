from __future__ import annotations

from urllib.parse import urlsplit

from ad_research.activity import (
    apply_infrastructure_freshness,
    primary_advertising_confidence,
)
from ad_research.models import AnalysisResult, Evidence

_PLATFORM_NAMES = {
    "meta": "Meta",
    "google_ads": "Google Ads",
    "tiktok": "TikTok",
    "linkedin": "LinkedIn",
    "microsoft_ads": "Microsoft Ads",
    "pinterest": "Pinterest",
    "snapchat": "Snapchat",
}
_STATUS_ORDER = {"CONFIRMED": 0, "LIKELY": 1, "POSSIBLE": 2, "NOT_DETECTED": 3}
_STRENGTH_ORDER = {"strong": 0, "medium": 1, "weak": 2}
_KIND_ORDER = {"network": 0, "script": 1, "global": 2, "dom": 3, "cookie": 4}


def enrich_ad_infrastructure_result(result: AnalysisResult) -> AnalysisResult:
    """Attach the observed-infrastructure result and three distinct reasons."""
    apply_infrastructure_freshness(result)
    result.ad_infrastructure_result = classify_ad_infrastructure(result)
    result.primary_advertising_confidence = primary_advertising_confidence(result)
    result.recommendation = (
        "MAY_NOT_NEED_TO_ASK"
        if result.advertising_infrastructure_confidence == "HIGH"
        else "ASK_SPEND"
    )
    result.evidence_summaries = summarize_evidence(result, limit=3)
    return result


def classify_ad_infrastructure(result: AnalysisResult):
    """Classify browser evidence only, never the company's actual ad activity."""
    if result.status != "COMPLETED":
        return "INCONCLUSIVE"
    if result.advertising_infrastructure_confidence in {"HIGH", "MEDIUM"}:
        return "AD INFRA DETECTED"
    return "NO AD INFRA DETECTED"


def summarize_evidence(result: AnalysisResult, *, limit: int = 3) -> list[str]:
    if result.status != "COMPLETED":
        return [_explain_inconclusive(result)][:limit]

    detected = [
        (key, platform)
        for key, platform in result.platforms.items()
        if platform.detected and platform.evidence
    ]
    detected.sort(key=lambda item: (_STATUS_ORDER[item[1].status], item[0]))
    if not detected:
        if result.google_tag_manager.detected:
            return [
                "Google Tag Manager was present, but no advertising-specific platform signal was observed."
            ]
        return ["No advertising-specific browser signal was observed on the pages inspected."]

    selected: list[tuple[str, Evidence, list[str]]] = []
    used: set[tuple[str, str, str]] = set()
    by_platform: dict[str, list[Evidence]] = {}
    identifiers: dict[str, list[str]] = {}
    for key, platform in detected:
        identifiers[key] = platform.identifiers
        by_platform[key] = sorted(
            platform.evidence,
            key=lambda evidence: (
                _STRENGTH_ORDER[evidence.strength],
                _KIND_ORDER.get(evidence.kind, 9),
                evidence.page_url,
                evidence.value,
            ),
        )

    # First give each detected platform one slot. This is more informative than
    # filling all three columns with variations of the same pixel.
    for key, _platform in detected:
        evidence = _next_distinct(by_platform[key], used, key)
        if evidence:
            selected.append((key, evidence, identifiers[key]))
        if len(selected) >= limit:
            break

    # Then add genuinely different pages/signals from the strongest platforms.
    if len(selected) < limit:
        for key, _platform in detected:
            while len(selected) < limit:
                evidence = _next_distinct(by_platform[key], used, key)
                if not evidence:
                    break
                selected.append((key, evidence, identifiers[key]))

    return [_format_evidence(key, evidence, ids) for key, evidence, ids in selected[:limit]]


def _next_distinct(
    evidence_items: list[Evidence], used: set[tuple[str, str, str]], platform: str
) -> Evidence | None:
    for evidence in evidence_items:
        # Script/network observations of the same named signal on the same page
        # are one reason, not two different evidence columns.
        family = _evidence_family(evidence.value)
        key = (platform, family, _page_label(evidence.page_url))
        if key not in used:
            used.add(key)
            return evidence
    return None


def _format_evidence(platform: str, evidence: Evidence, identifiers: list[str]) -> str:
    name = _PLATFORM_NAMES.get(platform, platform.replace("_", " ").title())
    signal = evidence.value
    signal = signal.replace(" request", " fired").replace(" source marker", " code found")
    if signal.casefold().startswith(f"{name} ".casefold()):
        signal = signal[len(name) + 1 :]
    phase = (
        "initial load, before any crawler consent action"
        if evidence.phase == "before_consent"
        else "the post-consent phase, after the crawler accepted the cookie banner"
    )
    page = _page_label(evidence.page_url)
    endpoint = ""
    if evidence.url:
        parsed = urlsplit(evidence.url)
        endpoint = f" via {parsed.netloc}{parsed.path}"
    identifier = f"; ID {identifiers[0]}" if identifiers else ""
    return f"{name}: {signal} on {page} during {phase}{endpoint}{identifier}."


def _evidence_family(value: str) -> str:
    lowered = value.lower()
    for removable in (" runtime loaded", " script", " source marker", " code found"):
        lowered = lowered.replace(removable, " tag")
    return " ".join(lowered.split())


def _page_label(url: str) -> str:
    path = urlsplit(url).path.rstrip("/") or "/"
    return "homepage" if path == "/" else path


def _clean(value: str) -> str:
    return " ".join(value.split())[:300]


def _explain_inconclusive(result: AnalysisResult) -> str:
    page = next((item for item in result.pages if item.error), None)
    title = (page.title if page else "") or ""
    final_url = (page.final_url if page else result.final_url) or ""
    reason = result.error or (page.error if page else None) or next(
        iter(result.technical_notes), "Website could not be inspected"
    )
    if "Vercel Security Checkpoint" in title:
        return (
            "Vercel Security Checkpoint rate-limited the automated browser "
            "(HTTP 429); no ad-infrastructure conclusion was made."
        )
    if "Just a moment" in title or "__cf_chl_" in final_url:
        return (
            "Cloudflare's browser challenge blocked the automated browser "
            "(HTTP 403); no ad-infrastructure conclusion was made."
        )
    if "ERR_HTTP2_PROTOCOL_ERROR" in reason:
        return (
            "The site or its CDN terminated the browser's HTTP/2 connection before "
            "the page loaded; no ad-infrastructure conclusion was made."
        )
    if "HTTP 403" in reason:
        return (
            "The website denied the automated browser (HTTP 403), commonly due to "
            "bot/WAF protection; no ad-infrastructure conclusion was made."
        )
    if "HTTP 429" in reason:
        return (
            "The website rate-limited the automated browser (HTTP 429); no "
            "ad-infrastructure conclusion was made."
        )
    return f"Could not verify the website: {_clean(reason)}"
