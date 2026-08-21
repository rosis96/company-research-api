"""Batch research job models.

Architecture reference: SS9 (request surface kept minimal for the GPT), SS10
(payload budgeting), SS11 (status semantics).

The batch layer adds orchestration only. Every company inside a job is researched
by exactly the same code path as ``POST /v1/research``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.request import FocusArea, ResearchDepth
from app.models.response import Coverage, PageResult, SiteStatus

JobStatus = Literal["queued", "running", "completed", "failed"]


class JobCreateRequest(BaseModel):
    """Start a batch research job."""

    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Company website URLs to research, in the order you want the results. "
            "Duplicates are kept and each gets its own result row."
        ),
        examples=[["https://company1.com", "https://company2.com"]],
    )
    research_depth: ResearchDepth = Field(
        default="standard",
        description="How much of each site to read. Applies to every URL in the job.",
    )
    focus: list[FocusArea] | None = Field(
        default=None,
        max_length=7,
        description="Optional bias toward particular page types, applied to every URL.",
    )
    force_refresh: bool = Field(
        default=False, description="Bypass cached results and re-crawl every site."
    )
    callback_url: str | None = Field(
        default=None,
        description=(
            "HTTPS URL to POST a signed event to as each company finishes and "
            "when the job completes. The event names the company and its "
            "outcome; fetch the evidence from the results endpoint."
        ),
    )


class JobCreated(BaseModel):
    """Returned immediately; the crawling happens in the background."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    total: int


class JobProgress(BaseModel):
    """Poll this until status is 'completed'."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    total: int
    completed: int = 0
    successful: int = 0
    partial: int = 0
    failed: int = 0
    created_at: str | None = None
    finished_at: str | None = None
    #: Set when a deploy or restart interrupted the job and it was resumed.
    notes: list[str] = Field(default_factory=list)


class JobResultRow(BaseModel):
    """One company's evidence inside a batch result page.

    Carries the same attribution guarantees as a single research response:
    ``pages[]`` entries each keep their own URL, and nothing is merged across
    companies (invariant 2).
    """

    model_config = ConfigDict(extra="forbid")

    input_index: int
    requested_url: str
    canonical_domain: str | None = None
    status: SiteStatus
    evidence_sufficient: bool = False
    status_reason: str | None = None
    site_name: str | None = None
    tagline: str | None = None
    pages: list[PageResult] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    #: True when page text was shortened to fit this response's character budget.
    content_truncated: bool = False


class JobResultsPage(BaseModel):
    """A bounded slice of a job's results.

    Never returns every company at once: 30 companies of full evidence is several
    hundred thousand characters, and GPT Actions cap a response at 100,000 (SS0).
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    total: int
    completed: int
    start: int
    limit: int
    returned: int
    #: Always present. False means this slice reached the end of the job.
    #: ``next_start`` alone is not enough: it is null at the end, and null fields
    #: are stripped from the response, so the caller would see no field at all.
    has_more: bool = False
    #: Index to pass as ``start`` next. Absent once ``has_more`` is false.
    next_start: int | None = None
    results: list[JobResultRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
