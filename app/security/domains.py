"""Registrable-domain (eTLD+1) resolution.

``tldextract`` will fetch the Public Suffix List over the network on first use.
That would be an outbound request that does not pass through the URL guard,
violating invariant 4, and it would also add unpredictable latency inside the
request deadline.  The extractor here is therefore pinned to the snapshot that
ships with the installed package: ``suffix_list_urls=()`` disables all network
access, and ``cache_dir=None`` stops it writing to disk at runtime.

Refreshing the suffix list is a deliberate build-time action (upgrade the
``tldextract`` package), not something that can happen mid-request.
"""

from __future__ import annotations

from functools import lru_cache

import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, fallback_to_snapshot=True)


@lru_cache(maxsize=8192)
def registrable_domain(host: str) -> str:
    """Return the eTLD+1 for ``host``.

    Falls back to the host itself when the suffix list yields nothing useful
    (for example a bare hostname with no public suffix), so that scope
    comparisons still behave deterministically.
    """
    normalized = host.lower().rstrip(".")
    result = _EXTRACT(normalized)
    if result.domain and result.suffix:
        return f"{result.domain}.{result.suffix}"
    # No recognised public suffix (an intranet name, or a TLD absent from the
    # snapshot).  Fall back to the *full* host rather than to ``result.domain``:
    # returning the bare label would make ``a.example-unknown`` and
    # ``b.example-unknown`` compare equal and silently widen the crawl scope.
    return normalized


@lru_cache(maxsize=8192)
def subdomain_label(host: str) -> str:
    """Return the subdomain portion of ``host`` (``""`` for an apex domain)."""
    return _EXTRACT(host.lower().rstrip(".")).subdomain


def same_registrable_domain(a: str, b: str) -> bool:
    return registrable_domain(a) == registrable_domain(b)
