from __future__ import annotations

from ad_research.models import OverallConfidence, PlatformResult, Recommendation


def classify_overall(
    platforms: dict[str, PlatformResult], *, crawl_conclusive: bool
) -> tuple[OverallConfidence, Recommendation]:
    """Classify browser evidence; GTM is deliberately absent."""
    if not crawl_conclusive:
        return "INCONCLUSIVE", "ASK_SPEND"

    confirmed = [result for result in platforms.values() if result.status == "CONFIRMED"]
    likely = [result for result in platforms.values() if result.status == "LIKELY"]
    possible = [result for result in platforms.values() if result.status == "POSSIBLE"]

    if len(confirmed) >= 2 or any(_mature_single_platform(item) for item in confirmed):
        return "HIGH", "MAY_NOT_NEED_TO_ASK"
    if confirmed or len(likely) >= 2:
        return "MEDIUM", "ASK_SPEND"
    if likely or possible:
        return "LOW", "ASK_SPEND"
    return "NONE", "ASK_SPEND"


def _mature_single_platform(result: PlatformResult) -> bool:
    runtime_pages = {
        item.page_url
        for item in result.evidence
        if item.kind == "network" and item.strength == "strong"
    }
    # Repeated firing across strategically different pages is stronger than a
    # single homepage beacon, while still never being converted into spend.
    return len(runtime_pages) >= 2
