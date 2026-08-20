"""Environment-driven application settings.

Architecture reference: SS6 (robots policy configuration), SS12 (auth, limits),
SS14 (cache, deployment).
"""

from __future__ import annotations

import hashlib
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
    cache: CacheSettings = Field(default_factory=CacheSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    renderer: RendererSettings = Field(default_factory=RendererSettings)

    # ------------------------------------------------------------------

    def api_key_table(self) -> dict[str, str]:
        """Return ``{sha256(key): scope}``.

        Keys are hashed at load time so the plaintext never sits in a
        long-lived structure, and lookups use a constant-time comparison of the
        digest rather than of the key itself.
        """
        table: dict[str, str] = {}
        for entry in self.api_keys.split(","):
            item = entry.strip()
            if not item:
                continue
            if ":" in item:
                raw_key, scope = item.rsplit(":", 1)
            else:
                raw_key, scope = item, "standard"
            scope = scope.strip().lower()
            if scope not in {"standard", "advanced"}:
                scope = "standard"
            digest = hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()
            table[digest] = scope
        return table


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
