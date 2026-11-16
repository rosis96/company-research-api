from __future__ import annotations

import pytest

from ad_research.detector import BrowserManager, is_dead_browser_error
from ad_research.settings import AdResearchSettings


def test_dead_browser_errors_are_recognised() -> None:
    # The exact error that failed every row after ~6,200 in a production batch.
    assert is_dead_browser_error(
        RuntimeError(
            "Browser.new_context: unable to perform operation on "
            "<WriteUnixTransport closed=True reading=False 0x7f5569622740>; "
            "the handler is closed"
        )
    )
    assert is_dead_browser_error(
        RuntimeError("Target page, context or browser has been closed")
    )
    assert not is_dead_browser_error(RuntimeError("net::ERR_NAME_NOT_RESOLVED"))


async def test_crashed_browser_is_relaunched() -> None:
    manager = BrowserManager(AdResearchSettings(recycle_after_domains=0))
    try:
        await manager.start()
    except Exception as exc:  # pragma: no cover - environment without Chromium
        pytest.skip(f"Chromium unavailable: {exc}")
    try:
        first = manager._browser
        await (await manager.new_context()).close()
        # Simulate Chromium dying mid-batch (OOM kill, crash, driver exit).
        first._impl_obj._connection._transport._proc.kill()
        for _ in range(3):
            context = await manager.new_context()
            page = await context.new_page()
            await page.set_content("<p>alive</p>")
            assert await page.inner_text("p") == "alive"
            await context.close()
        assert manager._browser is not first
    finally:
        await manager.close()


async def test_stale_invalidate_does_not_close_a_fresh_browser() -> None:
    manager = BrowserManager(AdResearchSettings(recycle_after_domains=0))
    try:
        await manager.start()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Chromium unavailable: {exc}")
    try:
        old = manager._browser
        await manager.invalidate(old)
        fresh = (await manager.new_context()).browser
        await manager.invalidate(old)  # a second worker reporting the same crash
        assert manager._browser is fresh and fresh.is_connected()
    finally:
        await manager.close()
