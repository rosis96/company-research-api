from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AdResearchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AD_RESEARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8010
    database_url: str = "sqlite:///./ad_research.db"
    concurrency: int = 3
    max_batch_size: int = 25_000
    max_upload_bytes: int = 100_000_000
    total_timeout_seconds: int = 75
    navigation_timeout_ms: int = 20_000
    settle_time_ms: int = 2_000
    post_consent_wait_ms: int = 3_000
    max_pages: int = 5
    min_pages: int = 3
    max_landing_pages: int = 3
    browser_headless: bool = True
    browser_executable_path: str | None = None
    recycle_after_domains: int = 100
    ignore_https_errors: bool = False
    user_agent: str = (
        "UnicornAdResearchBot/0.1 "
        "(internal advertising-infrastructure research; contact site operator)"
    )
    signature_directory: Path = Path(__file__).parent / "signatures"
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> AdResearchSettings:
    return AdResearchSettings()
