"""Cross-page boilerplate removal.

Architecture reference: SS8, "Cross-page boilerplate removal -- the
highest-leverage cleaning step".

Per-page cleaning always leaves residue: a repeated CTA, a floating "Book a
call" strip, a footer address block trafilatura kept.  But we hold N pages from
one site, which makes the problem trivially solvable without any model: remove
blocks that appear on most pages.

The removed set is preserved *once* in ``site_wide_content`` rather than being
deleted, because a recurring tagline or positioning statement is genuine
first-party evidence -- it simply should be stated once as site-level context
instead of eleven times inside per-page content.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")


@dataclass(slots=True)
class BoilerplateResult:
    """Cleaned page texts plus the boilerplate that was lifted out of them."""

    pages: list[str]
    site_wide_content: str | None
    removed_block_count: int
    chars_removed: int


def _key(block: str) -> str:
    return _WS.sub(" ", block).strip().lower()


def remove_boilerplate(
    page_blocks: list[list[str]],
    *,
    min_pages: int,
    document_frequency: float,
    max_block_chars: int,
    site_wide_cap: int,
) -> BoilerplateResult:
    """Strip blocks appearing on at least ``document_frequency`` of pages.

    Long blocks (over ``max_block_chars``) are left in place: a genuinely
    substantial passage repeated across pages is more likely to be real content
    than chrome, and removing it would damage the evidence.
    """
    page_count = len(page_blocks)
    if page_count < min_pages:
        return BoilerplateResult(
            pages=["\n".join(blocks) for blocks in page_blocks],
            site_wide_content=None,
            removed_block_count=0,
            chars_removed=0,
        )

    frequency: dict[str, int] = {}
    for blocks in page_blocks:
        for key in {_key(block) for block in blocks if block.strip()}:
            frequency[key] = frequency.get(key, 0) + 1

    threshold = max(2, math.ceil(document_frequency * page_count))
    boilerplate_keys = {
        key
        for key, count in frequency.items()
        if count >= threshold and len(key) <= max_block_chars
    }

    if not boilerplate_keys:
        return BoilerplateResult(
            pages=["\n".join(blocks) for blocks in page_blocks],
            site_wide_content=None,
            removed_block_count=0,
            chars_removed=0,
        )

    # Preserve first-seen ordering and original casing for the lifted blocks.
    preserved: list[str] = []
    preserved_keys: set[str] = set()
    cleaned_pages: list[str] = []
    chars_removed = 0

    for blocks in page_blocks:
        kept: list[str] = []
        for block in blocks:
            key = _key(block)
            if key in boilerplate_keys:
                chars_removed += len(block)
                if key not in preserved_keys:
                    preserved_keys.add(key)
                    preserved.append(block.strip())
                continue
            kept.append(block)
        cleaned_pages.append("\n".join(kept).strip())

    site_wide = "\n".join(preserved).strip() or None
    if site_wide and len(site_wide) > site_wide_cap:
        site_wide = site_wide[:site_wide_cap].rsplit("\n", 1)[0].strip() or site_wide[:site_wide_cap]

    return BoilerplateResult(
        pages=cleaned_pages,
        site_wide_content=site_wide,
        removed_block_count=len(boilerplate_keys),
        chars_removed=chars_removed,
    )
