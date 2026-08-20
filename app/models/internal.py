"""Internal dataclasses shared between crawler stages.

These never leave the orchestrator.  They are plain dataclasses rather than
Pydantic models because they are constructed in hot paths and never validated
against untrusted input.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.security.reasons import ReasonCode


class LinkRegion(StrEnum):
    """Where in the DOM a link was found (SS7 structural signals)."""

    NAV = "nav"
    HEADER = "header"
    MAIN = "main"
    FOOTER = "footer"
    BODY = "body"


class FetchMethod(StrEnum):
    HTTP = "http"
    BROWSER = "browser"


@dataclass(slots=True)
class Deadline:
    """A hard wall-clock deadline.

    Architecture reference: SS2 and invariant 3.  Every stage consults this
    before starting new work; nothing in the crawler may outlive it.
    """

    total_ms: int
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    @property
    def remaining_ms(self) -> int:
        return max(0, self.total_ms - self.elapsed_ms)

    @property
    def remaining_s(self) -> float:
        return self.remaining_ms / 1000.0

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0

    def has(self, milliseconds: int) -> bool:
        """True when at least ``milliseconds`` of budget remain."""
        return self.remaining_ms >= milliseconds

    def budget_for(self, requested_s: float, reserve_ms: int = 0) -> float:
        """Clamp a per-operation timeout to what the deadline actually allows."""
        available = max(0.0, (self.remaining_ms - reserve_ms) / 1000.0)
        return min(requested_s, available)


@dataclass(slots=True)
class Candidate:
    """A discovered URL under consideration."""

    url: str
    anchor_texts: list[str] = field(default_factory=list)
    regions: set[LinkRegion] = field(default_factory=set)
    depth: int = 1
    discovered_via: str = ""
    in_sitemap: bool = False
    lastmod: str | None = None
    inbound_links: int = 0
    #: Set when this candidate came from a hub page in wave 2.
    hub_parent_score: float = 0.0
    hub_parent_concept: str | None = None

    # Filled by the ranker.
    score: float = 0.0
    page_type: str = "other"
    category: str = "other"
    concept_key: str | None = None
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def merge(self, other: Candidate) -> None:
        """Fold a duplicate discovery of the same URL into this candidate."""
        self.anchor_texts.extend(other.anchor_texts)
        self.regions |= other.regions
        self.inbound_links += max(1, other.inbound_links)
        self.in_sitemap = self.in_sitemap or other.in_sitemap
        self.lastmod = self.lastmod or other.lastmod
        self.depth = min(self.depth, other.depth)
        if other.hub_parent_score > self.hub_parent_score:
            self.hub_parent_score = other.hub_parent_score
            self.hub_parent_concept = other.hub_parent_concept


@dataclass(slots=True)
class FetchResult:
    """Outcome of one page fetch (HTTP or rendered)."""

    url: str
    final_url: str
    status_code: int
    html: str
    method: FetchMethod
    redirect_chain: list[str] = field(default_factory=list)
    truncated_bytes: bool = False
    elapsed_ms: int = 0


@dataclass(slots=True)
class FetchFailure:
    url: str
    reason: ReasonCode
    status_code: int | None = None
    detail: str = ""
    methods_tried: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExtractedPage:
    """A fetched page after extraction, before budgeting and assembly."""

    url: str
    final_url: str
    page_type: str
    category: str
    score: float
    depth: int
    discovered_via: str
    method: FetchMethod
    status_code: int
    title: str | None
    meta_description: str | None
    h1: str | None
    headings: list[str]
    language: str | None
    text: str
    #: Content blocks used for cross-page boilerplate detection.
    blocks: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    outlinks: list[Candidate] = field(default_factory=list)
    word_count: int = 0
    truncated: bool = False
    is_hub: bool = False

    @property
    def is_homepage(self) -> bool:
        return self.page_type == "homepage"


@dataclass(slots=True)
class SufficiencyVerdict:
    """Result of the content sufficiency test (SS4)."""

    sufficient: bool
    word_count: int
    reason: str = ""

    @property
    def needs_render(self) -> bool:
        return not self.sufficient and self.reason != "parked"
