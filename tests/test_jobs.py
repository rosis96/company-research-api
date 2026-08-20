"""Batch research job tests.

The batch layer is orchestration only: every company inside a job goes through
the same ResearchService as POST /v1/research. These tests cover the
orchestration guarantees -- ordering, duplicates, isolation, progress,
pagination, budgets, expiry and auth -- plus proof that single-company behaviour
is unchanged.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.cache.result_cache import InMemoryCache
from app.config.settings import CacheSettings, Settings
from app.jobs.runner import JobRunner
from app.jobs.store import JobMeta, JobStore, JobStoreUnavailable
from app.main import create_app
from app.models.jobs import JobResultRow
from app.models.response import Coverage, CrawlSummary, PageResult, ResearchResponse, SiteInfo
from tests.fixtures.fake_redis import Clock, FakeRedis

STANDARD_KEY = "job-standard-key"
ADVANCED_KEY = "job-advanced-key"
AUTH = {"Authorization": f"Bearer {STANDARD_KEY}"}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def make_response(url: str, *, status="success", pages=2, words=400) -> ResearchResponse:
    from urllib.parse import urlsplit

    from app.security.domains import registrable_domain

    host = urlsplit(url if "://" in url else f"https://{url}").hostname or url
    return ResearchResponse(
        request_id="req_x",
        status=status,
        status_reason=None if status == "success" else "too_thin",
        evidence_sufficient=status == "success",
        site=SiteInfo(
            requested_url=url,
            canonical_domain=registrable_domain(host),
            site_name=f"Site {host}",
        ),
        pages=[
            PageResult(
                url=f"{url}/page{i}",
                page_type="homepage" if i == 0 else "about",
                title=f"Page {i}",
                word_count=words,
                content=f"Content for {host} page {i}. " * 40,
            )
            for i in range(pages)
        ],
        coverage=Coverage(found=["homepage"], missing=["pricing"]),
        crawl_summary=CrawlSummary(pages_scraped=pages, total_words=words * pages),
    )


class StubService:
    """Stands in for ResearchService: records calls, never touches the network."""

    def __init__(self, *, failing: set[str] | None = None, raising: set[str] | None = None,
                 delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self.failing = failing or set()
        self.raising = raising or set()
        self.delay = delay
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = asyncio.Lock()

    async def run(self, payload, *, request_id=None, key_id="-"):
        from app.research_service import ResearchOutcome

        async with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            self.calls.append(payload.url)
            if self.delay:
                await asyncio.sleep(self.delay)
            if payload.url in self.raising:
                raise RuntimeError("crawler exploded")
            status = "failed" if payload.url in self.failing else "success"
            return ResearchOutcome(
                response=make_response(payload.url, status=status),
                cache_hit=False,
                canonical_domain=None,
            )
        finally:
            async with self._lock:
                self.concurrent -= 1


@pytest.fixture
def redis_clock() -> Clock:
    return Clock()


@pytest.fixture
def fake_redis(redis_clock: Clock) -> FakeRedis:
    return FakeRedis(clock=redis_clock)


@pytest.fixture
def store(fake_redis: FakeRedis) -> JobStore:
    return JobStore(fake_redis, prefix="cra", ttl_seconds=86400)


def make_meta(urls: list[str], job_id: str = "job_test") -> JobMeta:
    return JobMeta(
        job_id=job_id, urls=urls, research_depth="standard", focus=None, force_refresh=False
    )


async def run_job(runner: JobRunner, meta: JobMeta) -> None:
    await runner._run(meta)


# ---------------------------------------------------------------------------
# Ordering, duplicates, input_index
# ---------------------------------------------------------------------------


async def test_exact_input_order_is_preserved(store: JobStore) -> None:
    urls = [f"https://company{i}.com" for i in range(8)]
    runner = JobRunner(service=StubService(), store=store, concurrency=4)
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    rows = await store.get_results(meta.job_id, list(range(8)))
    rows.sort(key=lambda r: r.input_index)
    assert [r.requested_url for r in rows] == urls
    assert [r.input_index for r in rows] == list(range(8))


async def test_duplicates_get_their_own_rows(store: JobStore) -> None:
    urls = [
        "https://a.com",
        "https://b.com",
        "https://a.com",   # duplicate
        "https://a.com",   # and again
    ]
    service = StubService()
    runner = JobRunner(service=service, store=store, concurrency=4)
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    rows = await store.get_results(meta.job_id, list(range(4)))
    rows.sort(key=lambda r: r.input_index)

    assert len(rows) == 4
    assert [r.requested_url for r in rows] == urls
    assert [r.input_index for r in rows] == [0, 1, 2, 3]
    # Crawled once per unique URL, but every index still gets a row.
    assert sorted(service.calls) == ["https://a.com", "https://b.com"]


async def test_duplicate_rows_carry_identical_evidence(store: JobStore) -> None:
    urls = ["https://a.com", "https://a.com"]
    runner = JobRunner(service=StubService(), store=store, concurrency=2)
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    rows = await store.get_results(meta.job_id, [0, 1])
    rows.sort(key=lambda r: r.input_index)
    assert rows[0].canonical_domain == rows[1].canonical_domain
    assert [p.url for p in rows[0].pages] == [p.url for p in rows[1].pages]


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_one_failed_site_does_not_fail_the_job(store: JobStore) -> None:
    urls = ["https://ok1.com", "https://bad.com", "https://ok2.com"]
    runner = JobRunner(
        service=StubService(failing={"https://bad.com"}), store=store, concurrency=3
    )
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    assert meta.status == "completed"
    assert meta.successful == 2
    assert meta.failed == 1
    rows = await store.get_results(meta.job_id, [0, 1, 2])
    assert {r.input_index: r.status for r in rows} == {0: "success", 1: "failed", 2: "success"}


async def test_a_crashing_crawl_becomes_a_failed_row(store: JobStore) -> None:
    """An exception inside one company must not take down the job."""
    urls = ["https://ok.com", "https://boom.com"]
    runner = JobRunner(
        service=StubService(raising={"https://boom.com"}), store=store, concurrency=2
    )
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    assert meta.status == "completed"
    rows = await store.get_results(meta.job_id, [0, 1])
    rows.sort(key=lambda r: r.input_index)
    assert rows[0].status == "success"
    assert rows[1].status == "failed"
    assert rows[1].status_reason


async def test_every_index_gets_a_row_even_when_everything_fails(store: JobStore) -> None:
    urls = [f"https://bad{i}.com" for i in range(5)]
    runner = JobRunner(
        service=StubService(raising=set(urls)), store=store, concurrency=5
    )
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    assert meta.completed == 5
    assert meta.failed == 5
    assert len(await store.completed_indices(meta.job_id)) == 5


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 5])
async def test_concurrency_is_bounded(store: JobStore, limit: int) -> None:
    urls = [f"https://c{i}.com" for i in range(12)]
    service = StubService(delay=0.02)
    runner = JobRunner(service=service, store=store, concurrency=limit)
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    assert service.max_concurrent <= limit, service.max_concurrent
    assert meta.completed == 12


def test_concurrency_is_configurable_by_environment() -> None:
    import os

    os.environ["CRA_JOBS__CONCURRENCY"] = "9"
    try:
        assert Settings().jobs.concurrency == 9
    finally:
        del os.environ["CRA_JOBS__CONCURRENCY"]
    assert Settings().jobs.concurrency == 5


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


async def test_progress_counters_add_up(store: JobStore) -> None:
    urls = [f"https://s{i}.com" for i in range(6)]
    runner = JobRunner(
        service=StubService(failing={"https://s1.com", "https://s4.com"}),
        store=store,
        concurrency=3,
    )
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    progress = meta.to_progress()
    assert progress.total == 6
    assert progress.completed == 6
    assert progress.successful + progress.partial + progress.failed == progress.completed
    assert progress.status == "completed"
    assert progress.finished_at


async def test_job_starts_queued_and_ends_completed(store: JobStore) -> None:
    meta = make_meta(["https://a.com"])
    await store.create(meta)
    assert (await store.load(meta.job_id)).status == "queued"

    runner = JobRunner(service=StubService(), store=store, concurrency=1)
    await run_job(runner, meta)
    assert (await store.load(meta.job_id)).status == "completed"


# ---------------------------------------------------------------------------
# Resume after restart
# ---------------------------------------------------------------------------


async def test_resume_only_crawls_missing_indices(store: JobStore) -> None:
    """A deploy mid-job must not re-crawl what already finished."""
    urls = [f"https://r{i}.com" for i in range(5)]
    meta = make_meta(urls)
    meta.status = "running"
    await store.create(meta)

    # Simulate two companies having completed before the restart.
    for index in (0, 1):
        await store.put_result(
            meta.job_id,
            JobResultRow(
                input_index=index, requested_url=urls[index], status="success",
                evidence_sufficient=True, coverage=Coverage(),
            ),
        )

    service = StubService()
    runner = JobRunner(service=service, store=store, concurrency=5)
    resumed = await runner.resume_interrupted()
    await asyncio.sleep(0.2)

    assert resumed == 1
    assert sorted(service.calls) == sorted(urls[2:])   # only the missing three
    reloaded = await store.load(meta.job_id)
    assert reloaded.completed == 5
    assert "resumed_after_restart" in reloaded.notes


async def test_completed_jobs_are_not_resumed(store: JobStore) -> None:
    meta = make_meta(["https://a.com"])
    meta.status = "completed"
    await store.create(meta)
    await store.save(meta)

    service = StubService()
    runner = JobRunner(service=service, store=store, concurrency=1)
    assert await runner.resume_interrupted() == 0
    assert service.calls == []


async def test_resume_lock_prevents_two_workers_racing(store: JobStore) -> None:
    meta = make_meta(["https://a.com"])
    meta.status = "running"
    await store.create(meta)
    assert await store.claim_resume_lock(meta.job_id) is True
    assert await store.claim_resume_lock(meta.job_id) is False


# ---------------------------------------------------------------------------
# Storage and expiry
# ---------------------------------------------------------------------------


async def test_job_state_expires_after_the_configured_ttl(
    fake_redis: FakeRedis, redis_clock: Clock
) -> None:
    store = JobStore(fake_redis, prefix="cra", ttl_seconds=3600)
    meta = make_meta(["https://a.com"])
    await store.create(meta)
    await store.put_result(
        meta.job_id,
        JobResultRow(input_index=0, requested_url="https://a.com", status="success",
                     coverage=Coverage()),
    )

    assert await store.load(meta.job_id) is not None
    assert await store.ttl(meta.job_id) == 3600

    redis_clock.advance(3601)
    assert await store.load(meta.job_id) is None
    assert await store.get_results(meta.job_id, [0]) == []
    assert await store.ttl(meta.job_id) == -2


def test_ttl_is_configurable_by_environment() -> None:
    import os

    os.environ["CRA_JOBS__TTL_SECONDS"] = "21600"
    try:
        assert Settings().jobs.ttl_seconds == 21600   # 6 hours
    finally:
        del os.environ["CRA_JOBS__TTL_SECONDS"]
    assert Settings().jobs.ttl_seconds == 86400       # 24 hours default


async def test_store_refuses_to_operate_without_redis() -> None:
    store = JobStore(None, prefix="cra", ttl_seconds=60)
    assert store.available is False
    with pytest.raises(JobStoreUnavailable):
        await store.create(make_meta(["https://a.com"]))


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def job_client(guard, fixture_servers, fake_redis):
    settings = Settings(
        api_keys=f"{STANDARD_KEY}:standard,{ADVANCED_KEY}:advanced",
        environment="test",
        log_json=False,
        log_level="ERROR",
        cache={"enabled": False},
        renderer={"enabled": False},
        rate_limit_per_minute=1000,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.guard = guard
        app.state.cache = InMemoryCache(CacheSettings(enabled=True))
        app.state.job_store = JobStore(fake_redis, prefix="cra", ttl_seconds=86400)
        app.state.research_service = StubService()
        app.state.job_runner = JobRunner(
            service=app.state.research_service,
            store=app.state.job_store,
            concurrency=settings.jobs.concurrency,
        )
        yield client


def wait_for_job(client, job_id: str, timeout: float = 5.0) -> dict:
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        body = client.get(f"/v1/research/jobs/{job_id}", headers=AUTH).json()
        if body["status"] in ("completed", "failed"):
            return body
        _time.sleep(0.05)
    return body


def test_create_job_returns_immediately_with_a_job_id(job_client) -> None:
    response = job_client.post(
        "/v1/research/jobs",
        json={"urls": ["https://a.com", "https://b.com"]},
        headers=AUTH,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"].startswith("job_")
    assert body["status"] == "queued"
    assert body["total"] == 2


def test_thirty_urls_are_accepted(job_client) -> None:
    urls = [f"https://c{i}.com" for i in range(30)]
    response = job_client.post("/v1/research/jobs", json={"urls": urls}, headers=AUTH)
    assert response.status_code == 202
    assert response.json()["total"] == 30


def test_thirty_one_urls_are_rejected(job_client) -> None:
    urls = [f"https://c{i}.com" for i in range(31)]
    response = job_client.post("/v1/research/jobs", json={"urls": urls}, headers=AUTH)
    assert response.status_code == 422
    assert "30" in response.json()["detail"]


def test_empty_url_list_is_rejected(job_client) -> None:
    assert job_client.post("/v1/research/jobs", json={"urls": []}, headers=AUTH).status_code == 422


def test_job_progress_endpoint(job_client) -> None:
    urls = [f"https://p{i}.com" for i in range(5)]
    job_id = job_client.post("/v1/research/jobs", json={"urls": urls}, headers=AUTH).json()["job_id"]
    body = wait_for_job(job_client, job_id)

    assert body["job_id"] == job_id
    assert body["total"] == 5
    assert body["completed"] == 5
    assert body["successful"] + body["partial"] + body["failed"] == 5
    assert set(body) >= {"job_id", "status", "total", "completed", "successful", "partial", "failed"}


def test_unknown_job_is_404(job_client) -> None:
    assert job_client.get("/v1/research/jobs/job_nope", headers=AUTH).status_code == 404
    assert job_client.get("/v1/research/jobs/job_nope/results", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_results_are_paginated_in_input_order(job_client) -> None:
    urls = [f"https://q{i}.com" for i in range(12)]
    job_id = job_client.post("/v1/research/jobs", json={"urls": urls}, headers=AUTH).json()["job_id"]
    wait_for_job(job_client, job_id)

    seen: list[int] = []
    start = 0
    while True:
        page = job_client.get(
            f"/v1/research/jobs/{job_id}/results", params={"start": start, "limit": 5},
            headers=AUTH,
        ).json()
        seen.extend(r["input_index"] for r in page["results"])
        if not page["has_more"]:
            break
        start = page["next_start"]

    assert seen == list(range(12))


def test_default_limit_is_five(job_client) -> None:
    urls = [f"https://d{i}.com" for i in range(12)]
    job_id = job_client.post("/v1/research/jobs", json={"urls": urls}, headers=AUTH).json()["job_id"]
    wait_for_job(job_client, job_id)

    page = job_client.get(f"/v1/research/jobs/{job_id}/results", headers=AUTH).json()
    assert page["limit"] == 5
    assert page["returned"] == 5
    assert [r["input_index"] for r in page["results"]] == [0, 1, 2, 3, 4]


def test_limit_is_capped_at_ten(job_client) -> None:
    urls = [f"https://e{i}.com" for i in range(30)]
    job_id = job_client.post("/v1/research/jobs", json={"urls": urls}, headers=AUTH).json()["job_id"]
    wait_for_job(job_client, job_id)

    page = job_client.get(
        f"/v1/research/jobs/{job_id}/results", params={"limit": 500}, headers=AUTH
    ).json()
    assert page["limit"] == 10
    assert page["returned"] <= 10


def test_start_beyond_the_end_returns_nothing(job_client) -> None:
    job_id = job_client.post(
        "/v1/research/jobs", json={"urls": ["https://a.com"]}, headers=AUTH
    ).json()["job_id"]
    wait_for_job(job_client, job_id)

    page = job_client.get(
        f"/v1/research/jobs/{job_id}/results", params={"start": 99}, headers=AUTH
    ).json()
    assert page["results"] == []
    assert page["has_more"] is False
    assert "next_start" not in page


def test_negative_start_is_rejected(job_client) -> None:
    job_id = job_client.post(
        "/v1/research/jobs", json={"urls": ["https://a.com"]}, headers=AUTH
    ).json()["job_id"]
    response = job_client.get(
        f"/v1/research/jobs/{job_id}/results", params={"start": -1}, headers=AUTH
    )
    assert response.status_code == 422


def test_result_rows_carry_the_required_fields(job_client) -> None:
    job_id = job_client.post(
        "/v1/research/jobs", json={"urls": ["https://a.com"]}, headers=AUTH
    ).json()["job_id"]
    wait_for_job(job_client, job_id)

    row = job_client.get(f"/v1/research/jobs/{job_id}/results", headers=AUTH).json()["results"][0]
    for field in (
        "input_index", "requested_url", "canonical_domain", "status",
        "evidence_sufficient", "pages", "coverage",
    ):
        assert field in row, field
    assert all("url" in page and "content" in page for page in row["pages"])


# ---------------------------------------------------------------------------
# Response size
# ---------------------------------------------------------------------------


def test_a_results_page_never_exceeds_the_gpt_payload_limit(job_client) -> None:
    """30 companies of full evidence is far past 100,000 characters."""
    urls = [f"https://big{i}.com" for i in range(30)]
    job_id = job_client.post("/v1/research/jobs", json={"urls": urls}, headers=AUTH).json()["job_id"]
    wait_for_job(job_client, job_id)

    start = 0
    while True:
        response = job_client.get(
            f"/v1/research/jobs/{job_id}/results", params={"start": start, "limit": 10},
            headers=AUTH,
        )
        body = response.json()
        assert len(json.dumps(body)) < 100_000
        if not body["has_more"]:
            break
        start = body["next_start"]


def test_oversized_pages_are_trimmed_and_flagged(job_client) -> None:
    from app.api.routes import _apply_results_budget

    rows = [
        JobResultRow(
            input_index=i,
            requested_url=f"https://x{i}.com",
            status="success",
            pages=[
                PageResult(url=f"https://x{i}.com/p", page_type="homepage",
                           word_count=9000, content="word " * 9000)
            ],
            coverage=Coverage(),
        )
        for i in range(5)
    ]
    trimmed = _apply_results_budget(rows, budget=10_000)

    assert trimmed is True
    assert sum(len(p.content) for r in rows for p in r.pages) <= 10_000
    assert all(r.content_truncated for r in rows)


def test_budget_leaves_small_pages_untouched() -> None:
    from app.api.routes import _apply_results_budget

    rows = [
        JobResultRow(
            input_index=0, requested_url="https://x.com", status="success",
            pages=[PageResult(url="https://x.com/", page_type="homepage",
                              word_count=10, content="short content")],
            coverage=Coverage(),
        )
    ]
    assert _apply_results_budget(rows, budget=80_000) is False
    assert rows[0].pages[0].content == "short content"
    assert rows[0].content_truncated is False


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/research/jobs"),
        ("get", "/v1/research/jobs/job_x"),
        ("get", "/v1/research/jobs/job_x/results"),
    ],
)
def test_all_job_endpoints_require_authentication(job_client, method: str, path: str) -> None:
    call = getattr(job_client, method)
    response = call(path, json={"urls": ["https://a.com"]}) if method == "post" else call(path)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/research/jobs"),
        ("get", "/v1/research/jobs/job_x"),
        ("get", "/v1/research/jobs/job_x/results"),
    ],
)
def test_a_standard_key_is_sufficient(job_client, method: str, path: str) -> None:
    """The GPT uses the standard key; no job endpoint may require 'advanced'."""
    call = getattr(job_client, method)
    response = call(path, json={"urls": ["https://a.com"]}, headers=AUTH) if method == "post" \
        else call(path, headers=AUTH)
    assert response.status_code != 403


def test_bad_key_is_rejected(job_client) -> None:
    response = job_client.post(
        "/v1/research/jobs", json={"urls": ["https://a.com"]},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Redis requirement
# ---------------------------------------------------------------------------


def test_jobs_require_redis(guard, fixture_servers) -> None:
    settings = Settings(
        api_keys=f"{STANDARD_KEY}:standard",
        environment="test", log_json=False, log_level="ERROR",
        cache={"enabled": False}, renderer={"enabled": False},
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.guard = guard
        response = client.post(
            "/v1/research/jobs", json={"urls": ["https://a.com"]}, headers=AUTH
        )
        assert response.status_code == 503
        assert "Redis" in response.json()["detail"]


# ---------------------------------------------------------------------------
# The single-company endpoint must be unaffected
# ---------------------------------------------------------------------------


@pytest.fixture
def plain_client(guard, fixture_servers):
    """A normally-wired app: real ResearchService, no job stubs."""
    settings = Settings(
        api_keys="standard-test-key:standard",
        environment="test", log_json=False, log_level="ERROR",
        cache={"enabled": False}, renderer={"enabled": False},
        rate_limit_per_minute=1000,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.guard = guard
        app.state.research_service.guard = guard
        yield client


def test_single_research_still_works_unchanged(plain_client, fixture_servers) -> None:
    """Adding the batch layer must not alter POST /v1/research."""
    response = plain_client.post(
        "/v1/research",
        json={"url": fixture_servers["static"].url("/")},
        headers={"Authorization": "Bearer standard-test-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1.1"
    assert body["status"] == "success"
    assert body["pages"]
    assert "input_index" not in body


def test_single_research_still_returns_200_on_crawl_failure(plain_client) -> None:
    response = plain_client.post(
        "/v1/research", json={"url": "http://localhost/"},
        headers={"Authorization": "Bearer standard-test-key"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


async def test_failed_rows_still_carry_a_canonical_domain(store: JobStore) -> None:
    """Null fields are stripped from responses, so a failed row must not lose it.

    Without a fallback the caller sees no `canonical_domain` at all and cannot
    tell which company failed.
    """
    urls = ["https://ok.com", "https://boom.com"]
    runner = JobRunner(
        service=StubService(raising={"https://boom.com"}), store=store, concurrency=2
    )
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    rows = await store.get_results(meta.job_id, [0, 1])
    rows.sort(key=lambda r: r.input_index)
    assert rows[1].status == "failed"
    assert rows[1].canonical_domain == "boom.com"
    assert rows[0].canonical_domain == "ok.com"


async def test_every_row_serialises_with_canonical_domain_present(store: JobStore) -> None:
    urls = ["https://ok.com", "https://boom.com"]
    runner = JobRunner(
        service=StubService(raising={"https://boom.com"}), store=store, concurrency=2
    )
    meta = make_meta(urls)
    await store.create(meta)
    await run_job(runner, meta)

    rows = await store.get_results(meta.job_id, [0, 1])
    for row in rows:
        payload = json.loads(row.model_dump_json(exclude_none=True))
        assert "canonical_domain" in payload, payload
        assert "input_index" in payload
        assert "requested_url" in payload
        assert "status" in payload


def test_worst_case_results_page_stays_under_the_gpt_limit() -> None:
    """Ten companies of dense evidence must still serialise under 100,000 chars.

    The stub responses used elsewhere are small; this uses realistic page sizes
    (11 pages of 8,000 characters each) so the budget is tested against the shape
    of a real crawl rather than a convenient fixture.
    """
    from app.api.routes import _apply_results_budget
    from app.config.settings import JobSettings
    from app.models.jobs import JobResultsPage

    budget = JobSettings().results_char_budget
    rows = [
        JobResultRow(
            input_index=i,
            requested_url=f"https://averylongcompanydomainname{i}.example.com",
            canonical_domain=f"averylongcompanydomainname{i}.example.com",
            status="success",
            evidence_sufficient=True,
            site_name=f"A Fairly Long Company Name Number {i} Limited",
            pages=[
                PageResult(
                    url=f"https://averylongcompanydomainname{i}.example.com/case-studies/d{p}",
                    page_type="case_study",
                    title=f"A long page title for company {i} page {p}",
                    headings=[f"Heading number {h} on this page" for h in range(8)],
                    word_count=1200,
                    content="word " * 1600,
                )
                for p in range(11)
            ],
            coverage=Coverage(
                found=["homepage", "about", "services", "case_study", "industries"],
                missing=["pricing", "testimonials", "customers", "solutions"],
            ),
        )
        for i in range(10)
    ]
    _apply_results_budget(rows, budget)
    page = JobResultsPage(
        job_id="job_x", status="completed", total=30, completed=30,
        start=0, limit=10, returned=len(rows), has_more=True, next_start=10,
        results=rows, notes=["page_content_trimmed_to_fit_response_budget"],
    )
    size = len(page.model_dump_json(exclude_none=True))
    assert size < 100_000, size
    assert size < 90_000, f"less than 10% headroom: {size}"
