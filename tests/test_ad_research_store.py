from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import inspect

from ad_research.jobs import _website_key
from ad_research.models import AnalysisResult
from ad_research.store import CrawlStore


def result(domain: str, status: str = "COMPLETED") -> AnalysisResult:
    now = datetime.now(UTC)
    return AnalysisResult(
        domain=domain,
        requested_url=f"https://{domain}",
        final_url=f"https://{domain}/",
        status=status,
        started_at=now,
        completed_at=now,
        advertising_infrastructure_confidence=("NONE" if status == "COMPLETED" else "INCONCLUSIVE"),
        recommendation=("LOW_PRIORITY" if status == "COMPLETED" else "ASK_SPEND"),
    )


def test_store_persists_job_progress_and_full_result(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    job = store.create_job(["one.test", "two.test"])
    store.set_job_status(job.job_id, "RUNNING")
    rows = store.get_job_rows_for_run(job.job_id)
    first = store.save_result(result("one.test"), job.job_id, rows[0][0])
    store.save_result(result("two.test", "INCONCLUSIVE"), job.job_id, rows[1][0])

    loaded = store.get_result(first.result_id)
    summary = store.get_job(job.job_id)
    assert loaded and loaded.result.domain == "one.test"
    assert summary and summary.completed_domains == 2
    assert summary.failed_domains == 1
    assert summary.status == "COMPLETED"
    saved_rows = store.get_job_rows(job.job_id)
    assert [row.website for row in saved_rows.rows] == ["one.test", "two.test"]
    assert saved_rows.rows[0].ad_infrastructure_result == "NO AD INFRA DETECTED"


def test_interrupted_jobs_become_inconclusive_on_restart(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    job = store.create_job(["one.test"])
    store.set_job_status(job.job_id, "RUNNING")
    assert store.recover_interrupted_jobs() == 1
    summary = store.get_job(job.job_id)
    assert summary.status == "INCONCLUSIVE"
    assert summary.completed_domains == 1
    assert summary.failed_domains == 1
    row = store.get_job_rows(job.job_id).rows[0]
    assert row.status == "INCONCLUSIVE"
    assert row.ad_infrastructure_result == "INCONCLUSIVE"


def test_unfinished_jobs_can_resume_after_process_restart(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    first = store.create_job(["one.test", "two.test"])
    second = store.create_job(["three.test"])
    store.set_job_status(first.job_id, "RUNNING")
    row_id = store.get_job_rows_for_run(first.job_id)[0][0]
    store.mark_job_row_running(first.job_id, row_id)

    resumable = store.jobs_to_resume()

    assert set(resumable) == {first.job_id, second.job_id}
    assert len(store.get_job_rows_for_run(first.job_id)) == 2
    assert [job.job_id for job in store.list_jobs()] == [second.job_id, first.job_id]


def test_runner_level_failure_finishes_every_remaining_row(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    job = store.create_job(["one.test", "two.test"])

    store.fail_unfinished_job(job.job_id, "browser worker crashed")

    summary = store.get_job(job.job_id)
    assert summary.status == "INCONCLUSIVE"
    assert summary.completed_domains == 2
    assert summary.failed_domains == 2
    rows = store.get_job_rows(job.job_id).rows
    assert all(row.status == "INCONCLUSIVE" for row in rows)
    assert all("browser worker crashed" in row.evidence_1 for row in rows)


def test_schema_adds_job_indexes_to_existing_databases(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    with store.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_ad_research_job_rows_job_order")
    store.create_schema()  # a restart on an older database
    names = {item["name"] for item in inspect(store.engine).get_indexes("ad_research_job_rows")}
    assert {"ix_ad_research_job_rows_job_order", "ix_ad_research_job_rows_job_status"} <= names


def test_duplicate_rows_share_one_scan_without_extra_history(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    job = store.create_job(["one.test", "https://www.one.test/", "two.test"])
    rows = store.get_job_rows_for_run(job.job_id)
    stored = store.save_result(result("one.test"), job.job_id, rows[0][0])
    store.link_job_row_result(job.job_id, rows[1][0], stored)
    store.save_result(result("two.test"), job.job_id, rows[2][0])

    summary = store.get_job(job.job_id)
    assert summary.completed_domains == 3
    assert summary.status == "COMPLETED"
    page = store.get_job_rows(job.job_id)
    assert page.total_rows == 3
    assert page.rows[0].result_id == page.rows[1].result_id == stored.result_id
    assert page.rows[1].ad_infrastructure_result == "NO AD INFRA DETECTED"
    # Linking a row is not another scan of the domain.
    assert store.get_domain_research("one.test").history.total_scans == 1


def test_iter_job_rows_pages_through_every_row(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    job = store.create_job([f"site{index}.test" for index in range(7)])
    pages = list(store.iter_job_rows(job.job_id, chunk_size=3))
    assert [len(page.rows) for page in pages] == [3, 3, 1]
    assert [row.row_index for page in pages for row in page.rows] == list(range(7))


def test_website_key_merges_only_the_same_site() -> None:
    assert _website_key("one.test") == _website_key("https://WWW.one.test/")
    assert _website_key("one.test") != _website_key("shop.one.test")
    assert _website_key("one.test/uk") != _website_key("one.test")
    assert _website_key("") == ""


async def test_one_unrecordable_failure_does_not_stop_the_batch(tmp_path) -> None:
    import asyncio

    from ad_research.jobs import JobRunner

    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    domains = [f"site{i}.test" for i in range(40)]
    job = store.create_job(domains)
    crawled: list[str] = []

    class Detector:
        async def analyze(self, domain, landing_page_urls=None):
            await asyncio.sleep(0.001)
            crawled.append(domain)
            if domain == "site3.test":
                raise RuntimeError("browser crashed")
            return result(domain)

    original_fail = store.fail_job_row

    def flaky_fail(job_id, row_id, error):
        if "browser crashed" in error:
            raise ConnectionError("database connection dropped")
        return original_fail(job_id, row_id, error)

    store.fail_job_row = flaky_fail
    runner = JobRunner(Detector(), store, concurrency=3)
    await runner._run(job.job_id)

    # Before the fix, the escaped ConnectionError ended the batch and every
    # uncrawled website was written off as "batch worker stopped".
    assert sorted(crawled) == sorted(domains)
    rows = store.get_job_rows(job.job_id).rows
    completed = [row for row in rows if row.status == "COMPLETED"]
    assert len(completed) == 39


def test_purge_expired_removes_old_research_and_keeps_recent(tmp_path) -> None:
    from datetime import timedelta

    from ad_research.store import (
        activity_evidence,
        crawl_job_rows,
        crawl_jobs,
        crawl_results,
        domain_scans,
    )

    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()

    old_job = store.create_job(["old.test"])
    old_row = store.get_job_rows_for_run(old_job.job_id)[0][0]
    store.save_result(result("old.test"), old_job.job_id, old_row)

    new_job = store.create_job(["new.test"])
    new_row = store.get_job_rows_for_run(new_job.job_id)[0][0]
    store.save_result(result("new.test"), new_job.job_id, new_row)

    # Age the first job (and a standalone single-site result) past the window.
    stale = datetime.now(UTC) - timedelta(days=10)
    store.save_result(result("standalone.test"))  # job_id is NULL
    with store.engine.begin() as connection:
        connection.execute(
            crawl_jobs.update()
            .where(crawl_jobs.c.id == old_job.job_id)
            .values(created_at=stale)
        )
        connection.execute(
            crawl_results.update()
            .where(crawl_results.c.job_id.is_(None))
            .values(completed_at=stale)
        )

    removed = store.purge_expired(max_age_days=3)
    assert removed["jobs"] == 1
    assert removed["results"] >= 2  # batch result + standalone result

    assert store.get_job(old_job.job_id) is None
    assert store.get_job(new_job.job_id) is not None
    with store.engine.connect() as connection:
        from sqlalchemy import func, select

        assert connection.execute(
            select(func.count()).select_from(crawl_job_rows)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(domain_scans)
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(activity_evidence)
        ).scalar_one() == 0
    store.dispose()


def test_purge_expired_is_noop_when_retention_disabled(tmp_path) -> None:
    store = CrawlStore(f"sqlite:///{tmp_path / 'test.db'}")
    store.create_schema()
    job = store.create_job(["keep.test"])
    row = store.get_job_rows_for_run(job.job_id)[0][0]
    store.save_result(result("keep.test"), job.job_id, row)
    removed = store.purge_expired(max_age_days=0)
    assert removed == {"jobs": 0, "results": 0, "activity_evidence": 0}
    assert store.get_job(job.job_id) is not None
    store.dispose()
