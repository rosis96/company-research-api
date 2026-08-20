"""Request model for ``POST /v1/research``.

Architecture reference: SS9.  The model-facing surface is deliberately minimal:
``max_pages``, ``max_depth``, ``timeout`` and ``max_content_length`` are *not*
exposed, because a language model will supply plausible numbers with no basis
and a large value turns into a timeout the user reads as a broken tool.

The advanced fields at the bottom are stripped from the OpenAPI document given
to the GPT and are rejected for keys without the ``advanced`` scope.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ResearchDepth = Literal["quick", "standard", "deep"]

FocusArea = Literal[
    "case_studies",
    "services",
    "products",
    "industries",
    "customers",
    "about",
    "pricing",
]

#: Maps a ``focus`` value onto the quota categories it biases.
FOCUS_CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "case_studies": ("case_study_detail", "case_study_index"),
    "services": ("offering",),
    "products": ("offering",),
    "industries": ("industries",),
    "customers": ("customers",),
    "about": ("about",),
    "pricing": ("pricing",),
}


class AdvancedOptions(BaseModel):
    """Server-side overrides. Requires an ``advanced``-scoped API key."""

    model_config = ConfigDict(extra="forbid")

    max_pages: int | None = Field(default=None, ge=1, le=40)
    max_depth: int | None = Field(default=None, ge=1, le=2)
    max_renders: int | None = Field(default=None, ge=0, le=20)
    deadline_ms: int | None = Field(default=None, ge=1000, le=180_000)
    global_content_budget: int | None = Field(default=None, ge=1000, le=200_000)
    robots_enabled: bool | None = None
    robots_apply_to_seed_url: bool | None = None
    debug: bool = False


class ResearchRequest(BaseModel):
    """One company website to research."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        ...,
        min_length=3,
        max_length=2048,
        description=(
            "The company's website URL, for example https://example.com. "
            "The scheme may be omitted. Only this domain is crawled."
        ),
        examples=["https://example.com"],
    )
    research_depth: ResearchDepth = Field(
        default="standard",
        description=(
            "How much of the site to read. 'quick' is faster and shallower, "
            "'standard' is the default, 'deep' reads more pages."
        ),
    )
    focus: list[FocusArea] | None = Field(
        default=None,
        max_length=7,
        description=(
            "Optional bias toward particular page types. Does not restrict the "
            "crawl to only those types."
        ),
    )
    force_refresh: bool = Field(
        default=False,
        description="Bypass the cached result for this domain and re-crawl.",
    )

    #: Excluded from the GPT-facing OpenAPI document by scripts/export_openapi.py.
    advanced: AdvancedOptions | None = Field(default=None, exclude=False)
