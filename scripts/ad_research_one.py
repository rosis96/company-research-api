#!/usr/bin/env python3
"""Phase-one diagnostic: analyze one website and print the complete evidence JSON."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ad_research.detector import AdvertisingDetector, BrowserManager
from ad_research.settings import get_settings
from ad_research.signature_engine import SignatureEngine
from app.security.url_guard import GuardPolicy, UrlGuard


async def run(url: str) -> None:
    settings = get_settings()
    browser = BrowserManager(settings)
    detector = AdvertisingDetector(
        settings,
        browser,
        UrlGuard(GuardPolicy(max_redirect_hops=5)),
        SignatureEngine(settings.signature_directory),
    )
    try:
        result = await detector.analyze(url)
        print(result.model_dump_json(indent=2))
    finally:
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Company website URL or domain")
    args = parser.parse_args()
    asyncio.run(run(args.url))
