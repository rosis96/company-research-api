"""Environment-driven application settings.

Architecture reference: SS6 (robots policy configuration), SS12 (auth, limits),
SS14 (cache, deployment).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RobotsSettings(BaseModel):
    """The robots policy surface from SS6.

    Every value here is a knob precisely so that robots behaviour can change
    without touching the crawler.
    """

    enabled: bool = True
    user_agent_token: str = "CompanyResearchBot"
    apply_to_seed_url: bool = True
    respect_crawl_delay: bool = True
    max_crawl_delay_ms: int = 2000
    on_blanket_disallow: Literal["fail", "proceed"] = "fail"
    on_fetch_error: Literal["proceed", "fail"] = "proceed"
    cache_ttl_seconds: int = 86400
    max_bytes: int = 524288
    timeout_seconds: float = 3.0


class CacheSettings(BaseModel):
    """Result cache configuration (SS14)."""

    enabled: bool = True
    redis_url: str = "redis://localhost:6379/0"
    key_prefix: str = "cra"
    ttl_success_seconds: int = 14 * 86400
    ttl_partial_seconds: int = 3 * 86400
    ttl_failed_stable_seconds: int = 7 * 86400
    ttl_failed_transient_seconds: int = 86400
    connect_timeout_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """One API key's identity and its limits.

    ``rate_limit_per_minute`` and ``monthly_quota`` of ``None`` mean "use the
    server default", so a key only carries a number when it genuinely differs.
    ``label`` names the workspace the key belongs to and appears in logs, never
    in a response.
    """

    digest: str
    scope: str
    label: str = ""
    rate_limit_per_minute: int | None = None
    monthly_quota: int | None = None

    @property
    def is_advanced(self) -> bool:
        return self.scope == "advanced"

    @property
    def short_id(self) -> str:
        return self.digest[:12]


def _key_record(
    raw_key: str,
    scope: str,
    *,
    label: str = "",
    rate_limit_per_minute: object = None,
    monthly_quota: object = None,
) -> KeyRecord:
    clean_scope = scope.strip().lower()
    if clean_scope not in {"standard", "advanced"}:
        clean_scope = "standard"
    return KeyRecord(
        digest=hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
        scope=clean_scope,
        label=label.strip(),
        rate_limit_per_minute=_positive_or_none(rate_limit_per_minute),
        monthly_quota=_positive_or_none(monthly_quota),
    )


def _positive_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


class ScrapeSettings(BaseModel):
    """Single-URL scrape (POST /v1/scrape).

    ``/v1/research`` crawls a whole domain; this fetches exactly the URL it is
    given. It exists for the two cases the crawler is wrong for: the caller
    already knows the page, and the caller needs an answer in seconds.
    """

    enabled: bool = True
    #: Default wall-clock budget. A plain HTTP page lands well inside this; a
    #: browser render adds 1.5-3s, which is why `render` is a caller choice.
    default_deadline_ms: int = 8_000
    max_deadline_ms: int = 30_000
    #: Ceiling on returned content. Server callers are not bound by the GPT
    #: Actions 100k limit, so this is generous, but it is not unbounded.
    max_content_chars: int = 120_000
    #: `output_format: html` returns markup, which is far larger than prose.
    max_html_chars: int = 400_000


class IdempotencySettings(BaseModel):
    """Safe retries for POST endpoints.

    Fails open: without Redis the header is ignored and the request runs, which
    costs a duplicated crawl rather than an outage.
    """

    enabled: bool = True
    ttl_seconds: int = 86_400


class WebhookSettings(BaseModel):
    """Outbound callbacks for batch jobs.

    A caller-supplied URL that the server POSTs to is a classic SSRF pivot, so
    every callback target goes through the same guard as a crawl target. Every
    delivery is signed, so the receiver can prove the call came from here.
    """

    enabled: bool = True
    #: HMAC-SHA256 key. Callbacks are refused when this is unset: an unsigned
    #: webhook is one anybody can forge.
    signing_secret: str = ""
    timeout_seconds: float = 5.0
    max_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    #: When set, callback URLs must be on one of these hosts. Empty means any
    #: host that passes the SSRF guard.
    allowed_hosts: tuple[str, ...] = ()
    #: Callbacks carry the *event* by default, not the evidence: the receiver
    #: fetches results from the results endpoint. Thirty companies of full
    #: evidence is a lot of large POSTs with partial-delivery problems.
    include_results: bool = False


class JobSettings(BaseModel):
    """Batch research jobs (SS14).

    Unlike the result cache, which fails open, jobs *require* Redis: a job whose
    state cannot be persisted is a job that silently vanishes.
    """

    enabled: bool = True
    max_urls: int = 30
    #: Companies crawled at once inside one job.
    concurrency: int = 5
    #: How long job state and results live in Redis.
    ttl_seconds: int = 86400
    default_limit: int = 5
    max_limit: int = 10
    #: Character budget for one results page, shared across the companies in it.
    #: GPT Actions cap a response at 100,000 characters, and 30 companies of full
    #: evidence is several hundred thousand, so a slice must be budgeted the same
    #: way a single research response is (SS10).
    #:
    #: 70,000 rather than 80,000: measured against worst-case content (10
    #: companies x 11 dense pages) an 80,000 budget serialises to ~91,000
    #: characters, leaving under 10% headroom. 70,000 lands at ~86,000 *and*
    #: keeps more pages, because the per-company share still covers whole pages
    #: instead of truncating one mid-way.
    results_char_budget: int = 70000
    #: Temporary operational endpoints under /v1/diagnostics.  They expose no
    #: credentials, but they are behind a switch so they can be turned off once
    #: the deployment is trusted, without a code change.
    diagnostics_enabled: bool = True
    #: Resume jobs left running by a deploy or restart.
    resume_on_startup: bool = True


class SecuritySettings(BaseModel):
    """Guard and fetch safety limits (SS12)."""

    max_redirect_hops: int = 5
    allow_ip_literals: bool = False
    #: Applied to the *decompressed* body; the stream is aborted at this size.
    max_response_bytes: int = 2097152
    max_request_body_bytes: int = 8192


class RendererSettings(BaseModel):
    """Browser fallback configuration (SS5)."""

    enabled: bool = True
    recycle_after_renders: int = 200
    #: Retry a homepage through the browser when the HTTP client is refused
    #: (401/403/407/429).  The browser keeps the same honest User-Agent, so this
    #: only recovers sites whose block is a TLS or header fingerprint check, or a
    #: JavaScript challenge -- not sites that have blocked us by name.
    escalate_on_block: bool = True
    #: Point at a pre-installed Chromium instead of Playwright's managed
    #: download.  Needed when the image ships a browser whose build number does
    #: not match the installed Playwright package.
    executable_path: str | None = None
    #: Chromium --host-resolver-rules value, e.g. "MAP * 10.0.0.9".  Chromium
    #: performs its own DNS and cannot use the pinned httpx transport (SS12), so
    #: this is the only lever for constraining browser-side resolution.  It is a
    #: browser-launch flag, not a per-request one, so it cannot pin each render
    #: to its own validated address; the network-level egress rules in the README
    #: remain the primary control.  Left unset in production by default.
    host_resolver_rules: str | None = None
    block_resource_types: tuple[str, ...] = (
        "image",
        "font",
        "media",
        "stylesheet",
        "websocket",
        "manifest",
        "other",
    )
    launch_args: tuple[str, ...] = (
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-extensions",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--mute-audio",
    )


class Settings(BaseSettings):
    """Top-level settings, populated from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="CRA_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "production", "test"] = "production"
    log_level: str = "INFO"
    log_json: bool = True

    #: Comma-separated ``key:scope`` pairs, e.g. ``abc123:standard,xyz789:advanced``.
    #: A bare ``key`` is treated as ``standard`` scope.
    api_keys: str = ""

    #: Identifiable, truthful user agent (SS4).  The contact URL is not optional
    #: in spirit: an anonymous bot at this volume is what gets blocklisted.
    user_agent: str = "CompanyResearchBot/1.0 (+https://example.com/bot)"

    #: Bumped by hand whenever ranking or extraction semantics change (SS14).
    config_version: str = "1"

    rate_limit_per_minute: int = 60
    rate_limit_monthly_quota: int = 0  # 0 disables the monthly quota
    max_concurrent_crawls: int = 4

    robots: RobotsSettings = Field(default_factory=RobotsSettings)
    jobs: JobSettings = Field(default_factory=JobSettings)
    scrape: ScrapeSettings = Field(default_factory=ScrapeSettings)
    webhooks: WebhookSettings = Field(default_factory=WebhookSettings)
    idempotency: IdempotencySettings = Field(default_factory=IdempotencySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    renderer: RendererSettings = Field(default_factory=RendererSettings)

    # ------------------------------------------------------------------

    def api_key_table(self) -> dict[str, KeyRecord]:
        """Return ``{sha256(key): KeyRecord}``.

        Two accepted formats:

        * the original ``key:scope,key:scope`` list, which still means exactly
          what it did;
        * a JSON array, which additionally carries per-key limits and a label::

              [{"key": "...", "scope": "standard", "label": "revcadence-prod",
                "rate_limit_per_minute": 600, "monthly_quota": 50000}]

        A positional fifth CSV field was the obvious extension and is
        deliberately not offered: this value is already the single most
        error-prone thing in the configuration, and adding two more positions to
        mis-count would make that worse. Anything past a couple of keys should
        use the JSON form.

        Keys are hashed at load time so the plaintext never sits in a long-lived
        structure, and lookups compare digests rather than keys.
        """
        raw = self.api_keys.strip()
        if not raw:
            return {}
        if raw.startswith("["):
            return self._api_keys_from_json(raw)

        table: dict[str, KeyRecord] = {}
        for entry in raw.split(","):
            item = entry.strip()
            if not item:
                continue
            if ":" in item:
                raw_key, scope = item.rsplit(":", 1)
            else:
                raw_key, scope = item, "standard"
            record = _key_record(raw_key.strip(), scope)
            table[record.digest] = record
        return table

    def _api_keys_from_json(self, raw: str) -> dict[str, KeyRecord]:
        try:
            entries = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"CRA_API_KEYS is not valid JSON: {exc}") from exc
        if not isinstance(entries, list):
            raise ValueError("CRA_API_KEYS JSON must be an array of objects")

        table: dict[str, KeyRecord] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("key"):
                raise ValueError("each CRA_API_KEYS entry needs a 'key'")
            record = _key_record(
                str(entry["key"]).strip(),
                str(entry.get("scope", "standard")),
                label=str(entry.get("label", "")),
                rate_limit_per_minute=entry.get("rate_limit_per_minute"),
                monthly_quota=entry.get("monthly_quota"),
            )
            table[record.digest] = record
        return table


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
