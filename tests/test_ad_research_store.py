from __future__ import annotations

from datetime import UTC, datetime

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
