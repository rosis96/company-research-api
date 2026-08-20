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
import uuid
from dataclasses import dataclass, field, fields

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
        """Decode leniently.

        A strict ``cls(**data)`` turns *any* schema drift -- a field added in a
        later deploy, a field removed -- into ``TypeError``, which the caller
        reports as "job not found".  A stale-but-readable job is far better than
        a 404, so unknown keys are dropped and absent optional keys keep their
        defaults.  Only a genuinely unusable record (no ``job_id`` or no ``urls``)
        is rejected.
        """
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("job meta is not an object")
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        if not payload.get("job_id") or payload.get("urls") is None:
            raise ValueError("job meta is missing job_id or urls")
        return cls(**payload)

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
        """Persist a new job, then prove it can be read back.

        Handing out a ``job_id`` for a job that is not actually retrievable is the
        worst failure this layer can have: the caller polls a 404 forever with no
        way to tell "expired" from "never existed".  So the write is verified
        through the same client before returning, and a failed verification is
        raised as :class:`JobStoreUnavailable` -- a 503 at creation time, which is
        honest, instead of a mysterious 404 a second later.
        """
        client = self._require()
        meta.created_at = meta.created_at or _now()
        key = self._meta_key(meta.job_id)

        await client.set(key, meta.to_json(), ex=self._ttl)
        await client.sadd(self._active_key(), meta.job_id)
        await client.expire(self._active_key(), self._ttl)

        exists, ttl = await self._key_state(key)
        log.info(
            "job.persisted",
            job_id=meta.job_id,
            key=key,
            ttl_after_write=ttl,
            exists_after_write=exists,
            total=meta.total,
            redis=connection_identity(client),
        )
        if not exists:
            raise JobStoreUnavailable(
                "job state was written but could not be read back from redis"
            )
        if ttl == 0:
            raise JobStoreUnavailable(
                f"job state expired immediately (configured ttl {self._ttl}s)"
            )

    async def load(self, job_id: str) -> JobMeta | None:
        client = self._require()
        key = self._meta_key(job_id)
        raw = await client.get(key)
        if not raw:
            # The 404 path.  Log enough to tell the three causes apart --
            # expired, never written, or written to a different redis -- without
            # ever putting credentials in a log line.
            exists, ttl = await self._key_state(key)
            log.warning(
                "job.load_miss",
                job_id=job_id,
                key=key,
                exists_at_get=exists,
                ttl_at_get=ttl,
                in_active_set=await self._in_active_set(job_id),
                redis=connection_identity(client),
            )
            return None
        try:
            return JobMeta.from_json(raw)
        except (ValueError, TypeError) as exc:
            log.warning(
                "job.meta_decode_failed", job_id=job_id, key=key, error=str(exc)
            )
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

    # -- diagnostics ---------------------------------------------------

    async def _key_state(self, key: str) -> tuple[bool, int]:
        """``(exists, ttl)``.  Redis TTL semantics: -2 missing, -1 no expiry."""
        try:
            ttl = int(await self._require().ttl(key))
        except Exception as exc:  # pragma: no cover - redis hiccup
            log.warning("job.ttl_probe_failed", key=key, error=str(exc))
            return (False, -2)
        return (ttl != -2, ttl)

    async def _in_active_set(self, job_id: str) -> bool | None:
        client = self._require()
        try:
            return bool(await client.sismember(self._active_key(), job_id))
        except Exception:  # pragma: no cover - old client without sismember
            return None

    async def describe(self, job_id: str) -> dict:
        """Everything needed to explain a 404 for one job id."""
        client = self._require()
        key = self._meta_key(job_id)
        raw = await client.get(key)
        exists, ttl = await self._key_state(key)
        decode = "absent"
        if raw:
            try:
                JobMeta.from_json(raw)
                decode = "ok"
            except (ValueError, TypeError) as exc:
                decode = f"failed: {exc}"
        return {
            "job_id": job_id,
            "meta_key": key,
            "exists": exists,
            "ttl_seconds": ttl,
            "raw_bytes": len(raw) if raw else 0,
            "decode": decode,
            "in_active_set": await self._in_active_set(job_id),
            "results_key": self._results_key(job_id),
            "results_stored": len(await self.completed_indices(job_id)),
        }

    async def self_test(self) -> dict:
        """Write a throwaway job, read it straight back, then remove it.

        This is the whole reported bug in one request: if the round trip succeeds
        here but a real job 404s, the write and the read are not talking to the
        same redis.
        """
        probe_id = f"job_probe_{uuid.uuid4().hex[:8]}"
        meta = JobMeta(
            job_id=probe_id,
            urls=["https://diagnostic.probe.invalid"],
            research_depth="standard",
            focus=None,
            force_refresh=False,
            notes=["diagnostic_probe"],
        )
        result: dict = {"probe_job_id": probe_id}
        try:
            await self.create(meta)
            result["write_ok"] = True
        except Exception as exc:
            result["write_ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        try:
            loaded = await self.load(probe_id)
            result["read_back_ok"] = loaded is not None
            result["ttl_after_write"] = (await self._key_state(self._meta_key(probe_id)))[1]
        finally:
            await self._forget_probe(probe_id)
        return result

    async def _forget_probe(self, job_id: str) -> None:
        """Remove a diagnostic probe.  Never used on real jobs."""
        client = self._require()
        try:
            await client.delete(self._meta_key(job_id))
            await client.srem(self._active_key(), job_id)
        except Exception:  # pragma: no cover - best effort cleanup
            pass


def connection_identity(client) -> dict:
    """Which redis this process is pointed at -- host, port and db only.

    Deliberately never includes the username or password, so it is safe in a log
    line and safe in a diagnostic response.
    """
    if client is None:
        return {"configured": False}
    try:
        kwargs = dict(client.connection_pool.connection_kwargs)
    except Exception:  # pragma: no cover - non-redis client (tests)
        return {"configured": True, "client": type(client).__name__}
    return {
        "configured": True,
        "host": kwargs.get("host") or kwargs.get("path") or "unknown",
        "port": kwargs.get("port"),
        "db": kwargs.get("db", 0),
    }


async def server_identity(client) -> dict:
    """Identity of the redis *server*, to prove two processes share one.

    ``run_id`` changes whenever the server restarts and differs between servers,
    so comparing it across two requests answers "is this the same redis?".
    """
    if client is None:
        return {"reachable": False}
    out: dict = {"reachable": False}
    try:
        info = await client.info("server")
        out.update(
            reachable=True,
            run_id=str(info.get("run_id", ""))[:16],
            redis_version=info.get("redis_version"),
            uptime_seconds=info.get("uptime_in_seconds"),
        )
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        memory = await client.info("memory")
        out["maxmemory"] = memory.get("maxmemory")
        out["maxmemory_policy"] = memory.get("maxmemory_policy")
        out["used_memory_human"] = memory.get("used_memory_human")
    except Exception:  # pragma: no cover
        pass
    try:
        out["dbsize"] = int(await client.dbsize())
    except Exception:  # pragma: no cover
        pass
    return out


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
