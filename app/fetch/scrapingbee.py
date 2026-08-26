"""Optional ScrapingBee fetch: residential proxies plus JS rendering.

Enabled only when ``CRA_SCRAPINGBEE_API_KEY`` is set. It is used as the render /
escalation path, so a homepage that blocks our datacenter IP (HTTP 429/403) or
hides its ad tags behind Google Tag Manager is still read: ScrapingBee fetches
through a residential proxy and returns the JS-rendered HTML. With no key this
module is inert and the built-in Chromium renderer is used exactly as before.

Config (all read from the environment, so no settings-schema change is needed):
  CRA_SCRAPINGBEE_API_KEY   the key (required to enable)
  CRA_SCRAPINGBEE_RENDER    "true" (default) runs JS so GTM-injected tags appear
  CRA_SCRAPINGBEE_PREMIUM   "true" (default) uses residential proxies (beats 429)
  CRA_SCRAPINGBEE_COUNTRY   optional 2-letter country code, e.g. "us"
"""
from __future__ import annotations

import os

import httpx
import structlog

log = structlog.get_logger(__name__)

_ENDPOINT = "https://app.scrapingbee.com/api/v1/"


def api_key() -> str:
    return (os.getenv("CRA_SCRAPINGBEE_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(api_key())


def _flag(name: str, default: str) -> bool:
    return (os.getenv(name) or default).strip().lower() in ("1", "true", "yes", "on")


async def fetch(url: str, *, timeout_ms: int) -> dict | None:
    """Fetch ``url`` through ScrapingBee. Returns {html, final_url, status_code}
    on success, or None on any failure so the caller can fall back."""
    key = api_key()
    if not key or not url:
        return None
    params = {
        "api_key": key,
        "url": url,
        "render_js": "true" if _flag("CRA_SCRAPINGBEE_RENDER", "true") else "false",
        "premium_proxy": "true" if _flag("CRA_SCRAPINGBEE_PREMIUM", "true") else "false",
        # Blocks images/CSS to save credits; script tags and inline JS (where the
        # ad fingerprints live) are still returned in the rendered HTML.
        "block_resources": "true",
    }
    country = (os.getenv("CRA_SCRAPINGBEE_COUNTRY") or "").strip()
    if country:
        params["country_code"] = country
    timeout = 60.0 if not timeout_ms else max(8.0, min(90.0, timeout_ms / 1000.0))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(_ENDPOINT, params=params)
    except Exception as exc:  # network, timeout, DNS -- fall back to the browser
        log.info("scrapingbee.error", url=url, error=str(exc))
        return None
    if r.status_code != 200 or not r.text:
        log.info("scrapingbee.non200", url=url, status=r.status_code)
        return None
    # ScrapingBee echoes the resolved URL in this header when it followed redirects.
    final_url = r.headers.get("Spb-Resolved-Url") or r.headers.get("spb-resolved-url") or url
    return {"html": r.text, "final_url": final_url, "status_code": 200}
