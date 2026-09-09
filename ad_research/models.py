from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PlatformConfidence = Literal["CONFIRMED", "LIKELY", "POSSIBLE", "NOT_DETECTED"]
OverallConfidence = Literal["HIGH", "MEDIUM", "LOW", "NONE", "INCONCLUSIVE"]
CrawlStatus = Literal["PENDING", "RUNNING", "COMPLETED", "INCONCLUSIVE", "CRAWL_FAILED"]
Recommendation = Literal["MAY_NOT_NEED_TO_ASK", "ASK_SPEND", "LOW_PRIORITY"]
AdInfrastructureResult = Literal[
    "AD INFRA DETECTED", "NO AD INFRA DETECTED", "INCONCLUSIVE"
]
JobRowStatus = Literal["PENDING", "RUNNING", "COMPLETED", "INCONCLUSIVE", "CRAWL_FAILED"]
InfrastructureFreshness = Literal[
    "RUNTIME_ACTIVE",
    "TAG_LOADED",
    "SOURCE_ONLY",
    "COOKIE_ONLY",
    "NOT_OBSERVED",
    "INCONCLUSIVE",
]
EvidenceTier = Literal[
    "RUNTIME_EVENT", "LOADED_TAG", "SOURCE_MARKER", "COOKIE_ONLY", "NOT_OBSERVED"
]
ActivityEvidenceSource = Literal[
    "meta_ad_library",
    "google_ads_transparency",
    "linkedin_ad_library",
    "tiktok_creative_center",
    "manual_verified",
    "other",
]
ReportedActivityStatus = Literal[
    "ACTIVE", "RECENT", "HISTORICAL", "NOT_FOUND", "INCONCLUSIVE"
]
ActivityConfidence = Literal[
    "NOT_CHECKED",
    "CONFIRMED_CURRENT",
    "RECENT_ACTIVITY",
    "POSSIBLE",
    "NOT_FOUND",
    "INCONCLUSIVE",
]
CombinedActivityConfidence = Literal["HIGH", "MEDIUM", "LOW", "INCONCLUSIVE"]
PrimaryAdvertisingConfidence = Literal["HIGH", "MEDIUM", "LOW", "INCONCLUSIVE"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["network", "script", "cookie", "global", "dom", "identifier"]
    value: str
    url: str | None = None
    page_url: str
    phase: Literal["before_consent", "after_consent"]
    strength: Literal["strong", "medium", "weak"]


class PlatformResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PlatformConfidence = "NOT_DETECTED"
    detected: bool = False
    identifiers: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    detected_before_consent: bool = False
    detected_after_consent: bool = False
    evidence_tier: EvidenceTier = "NOT_OBSERVED"
    runtime_event_observed: bool = False
    pages_observed: list[str] = Field(default_factory=list)


class TagManagerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detected: bool = False
    container_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class PageDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str | None = None
    status_code: int | None = None
    title: str | None = None
    consent_clicked: bool = False
    consent_control: str | None = None
    requests_observed: int = 0
    scripts_observed: int = 0
    error: str | None = None
    page_role: Literal["homepage", "internal", "ad_landing"] = "internal"


class IdentifierHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    identifier: str
    first_seen_at: datetime
    last_seen_at: datetime
    scans_seen: int = 1


class DomainHistorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_scans: int = 0
    completed_scans: int = 0
    scans_with_infrastructure: int = 0
    consecutive_scans_with_infrastructure: int = 0
    first_scan_at: datetime | None = None
    last_scan_at: datetime | None = None
    last_runtime_signal_at: datetime | None = None
    identifiers: list[IdentifierHistory] = Field(default_factory=list)


class ActivityEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=2048)
    source: ActivityEvidenceSource
    status: ReportedActivityStatus
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    advertiser_name: str | None = Field(default=None, max_length=300)
    active_ads_count: int | None = Field(default=None, ge=0)
    total_ads_count: int | None = Field(default=None, ge=0)
    first_ad_seen_at: date | None = None
    last_ad_seen_at: date | None = None
    landing_page_urls: list[str] = Field(default_factory=list, max_length=10)
    source_url: str | None = Field(default=None, max_length=2048)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("landing_page_urls")
    @classmethod
    def clean_landing_urls(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item and item.strip()))


class ActivityEvidenceRecord(ActivityEvidenceCreate):
    id: str
    domain_key: str
    created_at: datetime


class ActivitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence: ActivityConfidence = "NOT_CHECKED"
    sources_checked: int = 0
    sources_with_activity: int = 0
    active_ads_count: int | None = None
    newest_ad_seen_at: date | None = None
    last_checked_at: datetime | None = None
    evidence: list[ActivityEvidenceRecord] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    evidence_source: Literal["website_browser"] = "website_browser"
    requested_url: str
    final_url: str | None = None
    status: CrawlStatus
    started_at: datetime
    completed_at: datetime
    pages_crawled: int = 0
    pages: list[PageDiagnostic] = Field(default_factory=list)
    platforms: dict[str, PlatformResult] = Field(default_factory=dict)
    google_tag_manager: TagManagerResult = Field(default_factory=TagManagerResult)
    advertising_infrastructure_confidence: OverallConfidence = "INCONCLUSIVE"
    recommendation: Recommendation = "ASK_SPEND"
    ad_infrastructure_result: AdInfrastructureResult = "INCONCLUSIVE"
    infrastructure_freshness: InfrastructureFreshness = "INCONCLUSIVE"
    runtime_platforms: list[str] = Field(default_factory=list)
    landing_pages_requested: list[str] = Field(default_factory=list)
    landing_pages_crawled: int = 0
    history: DomainHistorySummary = Field(default_factory=DomainHistorySummary)
    external_activity: ActivitySummary = Field(default_factory=ActivitySummary)
    primary_advertising_confidence: PrimaryAdvertisingConfidence = "INCONCLUSIVE"
    combined_activity_confidence: CombinedActivityConfidence = "INCONCLUSIVE"
    combined_confidence_reasons: list[str] = Field(default_factory=list)
    # Read old locally persisted result JSON without exposing the misleading
    # account-level label in new API responses.
    icp_classification: str | None = Field(default=None, exclude=True, repr=False)
    evidence_summaries: list[str] = Field(default_factory=list, max_length=3)
    technical_notes: list[str] = Field(default_factory=list)
    error: str | None = None


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    landing_page_urls: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("landing_page_urls")
    @classmethod
    def clean_landing_pages(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item and item.strip()))


class BatchRequest(BaseModel):
    domains: list[str] = Field(min_length=1)

    @field_validator("domains")
    @classmethod
    def clean_domains(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("at least one domain is required")
        return cleaned


class JobCreated(BaseModel):
    job_id: str
    status: CrawlStatus
    total_domains: int
    website_column: str = "Website"
    source_columns: list[str] = Field(default_factory=list)


class JobSummary(BaseModel):
    job_id: str
    created_at: datetime
    total_domains: int
    completed_domains: int
    failed_domains: int
    status: CrawlStatus
    result_ids: list[str] = Field(default_factory=list)


class StoredResult(BaseModel):
    result_id: str
    job_id: str | None = None
    result: AnalysisResult


class JobRow(BaseModel):
    row_id: str
    row_index: int
    website: str
    original_data: dict[str, str] = Field(default_factory=dict)
    status: JobRowStatus
    result_id: str | None = None
    ad_infrastructure_result: AdInfrastructureResult = "INCONCLUSIVE"
    infrastructure_confidence: OverallConfidence = "INCONCLUSIVE"
    infrastructure_freshness: InfrastructureFreshness = "INCONCLUSIVE"
    primary_advertising_confidence: PrimaryAdvertisingConfidence = "INCONCLUSIVE"
    current_ad_activity: ActivityConfidence = "NOT_CHECKED"
    combined_activity_confidence: CombinedActivityConfidence = "INCONCLUSIVE"
    detected_platforms: list[str] = Field(default_factory=list)
    runtime_platforms: list[str] = Field(default_factory=list)
    pages_crawled: int = 0
    scan_count: int = 0
    last_runtime_signal_at: datetime | None = None
    evidence_1: str = ""
    evidence_2: str = ""
    evidence_3: str = ""
    error: str | None = None


class JobRowsPage(BaseModel):
    job_id: str
    website_column: str
    source_columns: list[str] = Field(default_factory=list)
    total_rows: int
    rows: list[JobRow] = Field(default_factory=list)


class DomainResearchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    infrastructure_result: AdInfrastructureResult = "INCONCLUSIVE"
    infrastructure_confidence: OverallConfidence = "INCONCLUSIVE"
    infrastructure_freshness: InfrastructureFreshness = "INCONCLUSIVE"
    history: DomainHistorySummary = Field(default_factory=DomainHistorySummary)
    external_activity: ActivitySummary = Field(default_factory=ActivitySummary)
    primary_advertising_confidence: PrimaryAdvertisingConfidence = "INCONCLUSIVE"
    combined_activity_confidence: CombinedActivityConfidence = "INCONCLUSIVE"
    combined_confidence_reasons: list[str] = Field(default_factory=list)


class SignaturePattern(BaseModel):
    pattern: str
    strength: Literal["strong", "medium", "weak"] = "medium"
    label: str


class PlatformSignature(BaseModel):
    key: str
    display_name: str
    category: Literal["advertising", "tag_manager"] = "advertising"
    network: list[SignaturePattern] = Field(default_factory=list)
    scripts: list[SignaturePattern] = Field(default_factory=list)
    cookies: list[SignaturePattern] = Field(default_factory=list)
    globals: list[SignaturePattern] = Field(default_factory=list)
    dom: list[SignaturePattern] = Field(default_factory=list)
    identifiers: list[SignaturePattern] = Field(default_factory=list)


class Observation(BaseModel):
    kind: Literal["network", "script", "cookie", "global", "dom"]
    value: str
    page_url: str
    phase: Literal["before_consent", "after_consent"]
    url: str | None = None


def jsonable_result(result: AnalysisResult) -> dict[str, Any]:
    return result.model_dump(mode="json")
