"""Batch callbacks: signing, SSRF, retries and delivery.

A caller-supplied URL that this server POSTs to is a new outbound surface, and
it is the riskiest thing added in this change. These tests exist mostly to hold
that surface closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings, WebhookSettings
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.jobs.webhook import WebhookDispatcher, WebhookError, sign
from app.main import create_app
from tests.fixtures.fake_redis import Clock, FakeRedis
from tests.test_jobs import STANDARD_KEY, StubService, make_meta

SECRET = "test-signing-secret"
AUTH = {"Authorization": f"Bearer {STANDARD_KEY}"}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class RecordingDispatcher:
    """Stands in for the real dispatcher: records, never leaves the process."""

    def __init__(self, available: bool = True, fail: bool = False) -> None:
        self._available = available
        self.fail = fail
        self.deliveries: list[tuple[str, str, dict]] = []

    @property
    def available(self) -> bool:
        return self._available

    async def deliver(self, url: str, event: str, data: dict) -> bool:
        if self.fail:
            raise RuntimeError("receiver is on fire")
        self.deliveries.append((url, event, data))
        return True


@pytest.fixture
def dispatcher(guard):
    def build(**overrides):
        settings = WebhookSettings(signing_secret=SECRET, **overrides)
        return WebhookDispatcher(
            guard=guard, settings=settings, user_agent="CompanyResearchBot/1.0 (+x)"
        )

    return build


@pytest.fixture
def store(fake_redis_local):
    return JobStore(fake_redis_local, prefix="cra", ttl_seconds=86400)


@pytest.fixture
def fake_redis_local():
    return FakeRedis(clock=Clock())


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_a_signature_is_verifiable_by_the_receiver() -> None:
    body = b'{"event":"company.completed"}'
    timestamp = "1700000000"
    expected = hmac.new(
        SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert sign(SECRET, timestamp, body) == f"sha256={expected}"


def test_the_timestamp_is_inside_the_signed_material() -> None:
    """Otherwise a captured delivery replays forever against a fresh receiver."""
    body = b'{"a":1}'
    assert sign(SECRET, "1700000000", body) != sign(SECRET, "1700000001", body)


def test_a_different_body_produces_a_different_signature() -> None:
    assert sign(SECRET, "1", b'{"a":1}') != sign(SECRET, "1", b'{"a":2}')


def test_a_different_secret_produces_a_different_signature() -> None:
    assert sign(SECRET, "1", b"x") != sign("other", "1", b"x")


# ---------------------------------------------------------------------------
# The callback URL is an SSRF vector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/hook",
        "http://[::1]/hook",
        "http://0177.0.0.1/hook",
        "file:///etc/passwd",
        "http://user:pass@example.com/hook",
        "http://example.com:22/hook",
    ],
)
async def test_a_callback_url_goes_through_the_same_guard(dispatcher, url) -> None:
    with pytest.raises(WebhookError):
        await dispatcher().validate_target(url)


async def test_callbacks_are_refused_without_a_signing_secret(guard) -> None:
    """An unsigned webhook is one anybody can forge."""
    unsigned = WebhookDispatcher(
        guard=guard, settings=WebhookSettings(signing_secret=""), user_agent="x"
    )
    assert unsigned.available is False
    with pytest.raises(WebhookError, match="signing secret"):
        await unsigned.validate_target("https://hooks.example.com/x")


async def test_callbacks_can_be_disabled_entirely(guard) -> None:
    off = WebhookDispatcher(
        guard=guard,
        settings=WebhookSettings(enabled=False, signing_secret=SECRET),
        user_agent="x",
    )
    assert off.available is False
    with pytest.raises(WebhookError, match="disabled"):
        await off.validate_target("https://hooks.example.com/x")


async def test_an_allowlist_is_enforced_when_configured(dispatcher) -> None:
    built = dispatcher(allowed_hosts=("hooks.revcadence.test",))
    with pytest.raises(WebhookError, match="allowlisted"):
        await built.validate_target("https://public.test/hook")


async def test_delivery_is_refused_when_unavailable(guard) -> None:
    off = WebhookDispatcher(
        guard=guard, settings=WebhookSettings(signing_secret=""), user_agent="x"
    )
    assert await off.deliver("https://public.test/hook", "job.completed", {}) is False


# ---------------------------------------------------------------------------
# What the runner sends
# ---------------------------------------------------------------------------


async def test_each_company_produces_an_event(store) -> None:
    hooks = RecordingDispatcher()
    runner = JobRunner(
        service=StubService(), store=store, concurrency=2, webhooks=hooks
    )
    meta = make_meta(["https://a.com", "https://b.com"])
    meta.callback_url = "https://hooks.example.com/x"
    await store.create(meta)
    await runner._run(meta)

    events = [event for _, event, _ in hooks.deliveries]
    assert events.count("company.completed") == 2
    assert events.count("job.completed") == 1
    assert events[-1] == "job.completed"


async def test_the_event_names_the_company_and_its_outcome(store) -> None:
    hooks = RecordingDispatcher()
    runner = JobRunner(service=StubService(), store=store, concurrency=1, webhooks=hooks)
    meta = make_meta(["https://a.com"])
    meta.callback_url = "https://hooks.example.com/x"
    await store.create(meta)
    await runner._run(meta)

    _, _, data = hooks.deliveries[0]
    assert data["job_id"] == meta.job_id
    assert data["input_index"] == 0
    assert data["requested_url"] == "https://a.com"
    assert data["result_status"] == "success"


async def test_the_event_carries_no_evidence_by_default(store) -> None:
    """Thirty companies of full evidence is thirty large POSTs nobody needs.

    The receiver is told *which* company finished; it fetches the content from
    the results endpoint, where paging and budgets already apply.
    """
    hooks = RecordingDispatcher()
    runner = JobRunner(service=StubService(), store=store, concurrency=1, webhooks=hooks)
    meta = make_meta(["https://a.com"])
    meta.callback_url = "https://hooks.example.com/x"
    await store.create(meta)
    await runner._run(meta)

    _, _, data = hooks.deliveries[0]
    assert "result" not in data
    assert "pages" not in json.dumps(data)


async def test_evidence_can_be_included_when_the_operator_asks(store) -> None:
    hooks = RecordingDispatcher()
    runner = JobRunner(
        service=StubService(),
        store=store,
        concurrency=1,
        webhooks=hooks,
        include_results_in_callbacks=True,
    )
    meta = make_meta(["https://a.com"])
    meta.callback_url = "https://hooks.example.com/x"
    await store.create(meta)
    await runner._run(meta)

    _, _, data = hooks.deliveries[0]
    assert data["result"]["pages"]


async def test_the_final_event_carries_the_tally(store) -> None:
    hooks = RecordingDispatcher()
    runner = JobRunner(
        service=StubService(failing={"https://bad.com"}),
        store=store,
        concurrency=2,
        webhooks=hooks,
    )
    meta = make_meta(["https://ok.com", "https://bad.com"])
    meta.callback_url = "https://hooks.example.com/x"
    await store.create(meta)
    await runner._run(meta)

    _, event, data = hooks.deliveries[-1]
    assert event == "job.completed"
    assert data["successful"] == 1
    assert data["failed"] == 1
    assert data["status"] == "completed"


async def test_no_callback_url_means_no_deliveries(store) -> None:
    hooks = RecordingDispatcher()
    runner = JobRunner(service=StubService(), store=store, concurrency=1, webhooks=hooks)
    meta = make_meta(["https://a.com"])
    await store.create(meta)
    await runner._run(meta)
    assert hooks.deliveries == []


async def test_a_broken_receiver_never_breaks_the_job(store) -> None:
    """Results are already durable in Redis. A webhook is an optimisation."""
    hooks = RecordingDispatcher(fail=True)
    runner = JobRunner(service=StubService(), store=store, concurrency=2, webhooks=hooks)
    meta = make_meta(["https://a.com", "https://b.com"])
    meta.callback_url = "https://hooks.example.com/x"
    await store.create(meta)
    await runner._run(meta)

    assert meta.status == "completed"
    assert meta.completed == 2
    rows = await store.get_results(meta.job_id, [0, 1])
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def hook_client(guard, fixture_servers, fake_redis_local):
    settings = Settings(
        api_keys=f"{STANDARD_KEY}:standard",
        environment="test",
        log_json=False,
        log_level="ERROR",
        cache={"enabled": False},
        renderer={"enabled": False},
        rate_limit_per_minute=1000,
        webhooks={"signing_secret": SECRET},
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.guard = guard
        app.state.webhooks.guard = guard
        app.state.job_store = JobStore(fake_redis_local, prefix="cra", ttl_seconds=86400)
        app.state.research_service = StubService()
        app.state.job_runner = JobRunner(
            service=app.state.research_service,
            store=app.state.job_store,
            concurrency=2,
            webhooks=app.state.webhooks,
        )
        yield client


def test_a_bad_callback_url_is_rejected_at_creation(hook_client) -> None:
    """A 422 the caller can act on beats a job that silently fails to report."""
    response = hook_client.post(
        "/v1/research/jobs",
        json={"urls": ["https://a.com"], "callback_url": "http://169.254.169.254/x"},
        headers=AUTH,
    )
    assert response.status_code == 422
    assert "callback" in response.json()["detail"].lower()


def test_a_good_callback_url_is_accepted(hook_client) -> None:
    response = hook_client.post(
        "/v1/research/jobs",
        json={"urls": ["https://a.com"], "callback_url": "https://public.test/hook"},
        headers=AUTH,
    )
    assert response.status_code == 202


def test_a_job_without_a_callback_still_works(hook_client) -> None:
    response = hook_client.post(
        "/v1/research/jobs", json={"urls": ["https://a.com"]}, headers=AUTH
    )
    assert response.status_code == 202


def test_the_callback_url_survives_a_round_trip(hook_client) -> None:
    from app.jobs.store import JobMeta

    meta = JobMeta(
        job_id="job_cb",
        urls=["https://a.com"],
        research_depth="standard",
        focus=None,
        force_refresh=False,
        callback_url="https://public.test/hook",
    )
    restored = JobMeta.from_json(meta.to_json())
    assert restored.callback_url == "https://public.test/hook"
