"""Response models for ``POST /v1/research``.

Architecture reference: SS10 (schema v1.1, slimmed) and SS11 (status model).

Governing rule: every field in the default payload must be something the GPT can
act on.  Operational metadata lives in the optional ``debug`` block and is
absent unless an ``advanced``-scoped caller asked for it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.1"

SiteStatus = Literal["success", "partial", "failed"]

PageType = Literal[
    "homepage",
    "about",
    "services",
    "service_detail",
    "products",
    "product_detail",
    "solutions",
    "industries",
    "case_study_index",
    "case_study",
    "customers",
    "testimonials",
    "portfolio",
    "pricing",
    "process",
    "team",
    "contact",
    "careers_overview",
    "blog_index",
    "blog_post",
    "resource",
    "other",
]

StoppedReason = Literal[
    "budget_reached",
    "deadline",
    "candidates_exhausted",
    "char_budget_reached",
]


class Organization(BaseModel):
    """Allowlisted JSON-LD organization facts (SS8).

    Deliberately excludes ``sameAs``, ``logo``, ``contactPoint`` and -- as a
    correctness requirement rather than a size optimisation -- ``aggregateRating``
    and ``review``, which are third-party data.
    """

    model_config = ConfigDict(extra="forbid")

    legal_name: str | None = None
    description: str | None = None
    founding_date: str | None = None
    employees: str | None = None
    locations: list[str] = Field(default_factory=list)
    area_served: list[str] = Field(default_factory=list)
    knows_about: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.legal_name,
                self.description,
                self.founding_date,
                self.employees,
                self.locations,
                self.area_served,
                self.knows_about,
            ]
        )


class SiteInfo(BaseModel):
    """Site-level context (SS10)."""

    model_config = ConfigDict(extra="forbid")

    requested_url: str
    final_url: str | None = None
    canonical_domain: str | None = None
    domain_rebased: bool = False
    language: str | None = None
    site_name: str | None = None
    tagline: str | None = None
    organization: Organization | None = None
    site_wide_content: str | None = Field(
        default=None,
        description=(
            "Text that appeared on most pages of the site, deduplicated and "
            "stated once. Site-level context, not attributable to one page."
        ),
    )


class PageResult(BaseModel):
    """One scraped page.

    Content only ever exists inside one of these objects, each of which carries
    its own ``url`` -- this is the structural guarantee behind invariant 2.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str | None = Field(
        default=None, description="Present only when it differs from url."
    )
    page_type: PageType
    title: str | None = None
    headings: list[str] = Field(default_factory=list)
    word_count: int = 0
    truncated: bool = False
    content: str


class FailedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    reason_code: str
    http_status: int | None = None


class Coverage(BaseModel):
    """What the GPT does and does not have evidence for (SS10)."""

    model_config = ConfigDict(extra="forbid")

    found: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class CrawlSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages_scraped: int = 0
    pages_failed: int = 0
    browser_renders: int = 0
    max_depth_reached: int = 0
    total_words: int = 0
    content_chars: int = 0
    elapsed_ms: int = 0
    stopped_reason: StoppedReason | None = None
    cache_hit: bool = False


class ResearchResponse(BaseModel):
    """The complete result body.

    Always returned with HTTP 200, including for crawl failures (SS11), so that
    GPT Actions do not treat a blocked or dead website as an infrastructure
    error and fall back on model knowledge.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    request_id: str
    status: SiteStatus
    status_reason: str | None = None
    evidence_sufficient: bool = False

    site: SiteInfo
    pages: list[PageResult] = Field(default_factory=list)
    failed_pages: list[FailedPage] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    crawl_summary: CrawlSummary = Field(default_factory=CrawlSummary)
    notes: list[str] = Field(default_factory=list)

    #: Present only when an advanced-scoped caller passed ``advanced.debug``.
    debug: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    version: str
    schema_version: str
    config_version: str
    renderer_available: bool
    cache_available: bool


class ErrorResponse(BaseModel):
    """Body for the real HTTP errors defined in SS11 (401/422/429/500)."""

    model_config = ConfigDict(extra="forbid")

    error: str
    detail: str | None = None
    request_id: str | None = None
