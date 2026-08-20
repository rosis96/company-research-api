"""Redis-backed batch job state.

Architecture reference: SS14 (Redis is already the cache; extend it rather than
introducing a database).

Layout, all under the configured key prefix:

    {prefix}:job:{job_id}:meta      JSON  -- status, counters, the URL list
    {prefix}:job:{job_id}:results   HASH  -- field = input_index, value = JSON row
    {prefix}:jobs:active            SET   -- job ids currently running

Results are written **per company, as each finishes**. That is what makes an
interrupted job recoverable: a restart loses only the companies that were
in flight, never the ones already done.

Unlike the result cache, which fails open, this store is **required**. A job whose
state cannot be persisted is a job that silently disappears, so the API refuses to
create one when Redis is unavailable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import structlog

from app.models.jobs import JobProgress, JobResultRow

log = structlog.get_logger(__name__)


class JobStoreUnavailable(RuntimeError):
    """Redis is not reachable, so batch jobs cannot be persisted."""


@dataclass(slots=True)
class JobMeta:
    """Everything needed to report progress and to resume after a restart."""

    job_id: str
    urls: list[str]
    research_depth: str
    focus: list[str] | None
    force_refresh: bool
    status: str = "queued"
    completed: int = 0
    successful: int = 0
    partial: int = 0
    failed: int = 0
    created_at: str = ""
    finished_at: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.urls)

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "urls": self.urls,
                "research_depth": self.research_depth,
                "focus": self.focus,
                "force_refresh": self.force_refresh,
                "status": self.status,
                "completed": self.completed,
                "successful": self.successful,
                "partial": self.partial,
                "failed": self.failed,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "notes": self.notes,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> JobMeta:
        data = json.loads(raw)
        return cls(**data)

    def to_progress(self) -> JobProgress:
        return JobProgress(
            job_id=self.job_id,
            status=self.status,  # type: ignore[arg-type]
            total=self.total,
            completed=self.completed,
            successful=self.successful,
            partial=self.partial,
            failed=self.failed,
            created_at=self.created_at or None,
            finished_at=self.finished_at,
            notes=self.notes,
        )


class JobStore:
    """Persistence for batch jobs. Requires a working Redis client."""

    def __init__(self, client, *, prefix: str = "cra", ttl_seconds: int = 86400) -> None:
        self._client = client
        self._prefix = prefix
        self._ttl = ttl_seconds

    @property
    def available(self) -> bool:
        return self._client is not None

    # -- keys ----------------------------------------------------------

    def _meta_key(self, job_id: str) -> str:
        return f"{self._prefix}:job:{job_id}:meta"

    def _results_key(self, job_id: str) -> str:
        return f"{self._prefix}:job:{job_id}:results"

    def _active_key(self) -> str:
        return f"{self._prefix}:jobs:active"

    def _require(self):
        if self._client is None:
            raise JobStoreUnavailable("redis is not available")
        return self._client

    # -- meta ----------------------------------------------------------

    async def create(self, meta: JobMeta) -> None:
        client = self._require()
        meta.created_at = meta.created_at or _now()
        await client.set(self._meta_key(meta.job_id), meta.to_json(), ex=self._ttl)
        await client.sadd(self._active_key(), meta.job_id)
        await client.expire(self._active_key(), self._ttl)

    async def load(self, job_id: str) -> JobMeta | None:
        client = self._require()
        raw = await client.get(self._meta_key(job_id))
        if not raw:
            return None
        try:
            return JobMeta.from_json(raw)
        except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
            log.warning("job.meta_decode_failed", job_id=job_id, error=str(exc))
            return None

    async def save(self, meta: JobMeta) -> None:
        client = self._require()
        await client.set(self._meta_key(meta.job_id), meta.to_json(), ex=self._ttl)
        if meta.status in ("completed", "failed"):
            await client.srem(self._active_key(), meta.job_id)

    # -- results -------------------------------------------------------

    async def put_result(self, job_id: str, row: JobResultRow) -> None:
        client = self._require()
        key = self._results_key(job_id)
        await client.hset(key, str(row.input_index), row.model_dump_json(exclude_none=True))
        await client.expire(key, self._ttl)

    async def completed_indices(self, job_id: str) -> set[int]:
        """Which rows already exist -- the basis for resuming after a restart."""
        client = self._require()
        fields = await client.hkeys(self._results_key(job_id))
        out: set[int] = set()
        for f in fields or []:
            try:
                out.add(int(f))
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
        return out

    async def get_results(self, job_id: str, indices: list[int]) -> list[JobResultRow]:
        """Fetch a specific, ordered slice of rows."""
        if not indices:
            return []
        client = self._require()
        raw = await client.hmget(self._results_key(job_id), [str(i) for i in indices])
        rows: list[JobResultRow] = []
        for value in raw or []:
            if not value:
                continue
            try:
                rows.append(JobResultRow.model_validate_json(value))
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("job.result_decode_failed", job_id=job_id, error=str(exc))
        return rows

    async def ttl(self, job_id: str) -> int:
        client = self._require()
        return int(await client.ttl(self._meta_key(job_id)))

    # -- resume --------------------------------------------------------

    async def active_job_ids(self) -> list[str]:
        client = self._require()
        members = await client.smembers(self._active_key())
        return sorted(str(m) for m in (members or []))

    async def claim_resume_lock(self, job_id: str, ttl: int = 300) -> bool:
        """Only one process may resume a given job."""
        client = self._require()
        return bool(
            await client.set(f"{self._prefix}:job:{job_id}:resume", "1", ex=ttl, nx=True)
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
