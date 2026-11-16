from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
)
from sqlalchemy.engine import Engine

from ad_research.activity import (
    attach_research_context,
    build_history,
    summarize_activity,
)
from ad_research.models import (
    ActivityEvidenceCreate,
    ActivityEvidenceRecord,
    AnalysisResult,
    DomainResearchSummary,
    JobRow,
    JobRowsPage,
    JobSummary,
    StoredResult,
    jsonable_result,
)
from ad_research.qualification import enrich_ad_infrastructure_result
from app.security.domains import registrable_domain
from app.security.url_guard import coerce_input_url

metadata = MetaData()

crawl_jobs = Table(
    "ad_research_crawl_jobs",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("total_domains", Integer, nullable=False),
    Column("completed_domains", Integer, nullable=False, default=0),
    Column("failed_domains", Integer, nullable=False, default=0),
    Column("status", String(30), nullable=False),
    Column("input_json", Text, nullable=False),
)

crawl_results = Table(
    "ad_research_crawl_results",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("job_id", String(40), ForeignKey("ad_research_crawl_jobs.id"), nullable=True),
    Column("domain", String(255), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("status", String(30), nullable=False),
    Column("pages_crawled", Integer, nullable=False),
    Column("overall_confidence", String(30), nullable=False),
    Column("recommendation", String(40), nullable=False),
    Column("result_json", Text, nullable=False),
    Column("error", Text, nullable=True),
)

crawl_job_rows = Table(
    "ad_research_job_rows",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("job_id", String(40), ForeignKey("ad_research_crawl_jobs.id"), nullable=False),
    Column("row_index", Integer, nullable=False),
    Column("website", Text, nullable=False),
    Column("original_json", Text, nullable=False),
    Column("status", String(30), nullable=False),
    Column("result_id", String(40), ForeignKey("ad_research_crawl_results.id"), nullable=True),
    # Kept as the physical column name so existing local databases remain usable.
    Column("icp_classification", String(20), nullable=False, default="INCONCLUSIVE"),
    Column("evidence_1", Text, nullable=False, default=""),
    Column("evidence_2", Text, nullable=False, default=""),
    Column("evidence_3", Text, nullable=False, default=""),
    Column("error", Text, nullable=True),
)

domain_scans = Table(
    "ad_research_domain_scans",
    metadata,
    Column("result_id", String(40), primary_key=True),
    Column("domain_key", String(255), nullable=False, index=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("status", String(30), nullable=False),
    Column("infrastructure_confidence", String(30), nullable=False),
    Column("infrastructure_result", String(30), nullable=False),
    Column("infrastructure_freshness", String(30), nullable=False),
    Column("runtime_platforms_json", Text, nullable=False, default="[]"),
    Column("identifiers_json", Text, nullable=False, default="{}"),
)

activity_evidence = Table(
    "ad_research_activity_evidence",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("domain_key", String(255), nullable=False, index=True),
    Column("domain", String(2048), nullable=False),
    Column("source", String(50), nullable=False),
    Column("status", String(30), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("advertiser_name", String(300), nullable=True),
    Column("active_ads_count", Integer, nullable=True),
    Column("total_ads_count", Integer, nullable=True),
    Column("first_ad_seen_at", String(10), nullable=True),
    Column("last_ad_seen_at", String(10), nullable=True),
    Column("landing_page_urls_json", Text, nullable=False, default="[]"),
    Column("source_url", Text, nullable=True),
    Column("notes", Text, nullable=True),
)


# create_all only builds indexes together with brand-new tables, so
# create_schema also creates these on databases that already exist.
_INDEXES = (
    Index("ix_ad_research_job_rows_job_order", crawl_job_rows.c.job_id, crawl_job_rows.c.row_index),
    Index("ix_ad_research_job_rows_job_status", crawl_job_rows.c.job_id, crawl_job_rows.c.status),
    Index("ix_ad_research_crawl_results_job", crawl_results.c.job_id),
    # Lets deletes from crawl_results check this foreign key without a table scan.
    Index("ix_ad_research_job_rows_result", crawl_job_rows.c.result_id),
)


class CrawlStore:
    """Small SQL repository; SQLite locally and PostgreSQL via DATABASE_URL later."""

    def __init__(self, database_url: str) -> None:
        database_url = _normalize_database_url(database_url)
        kwargs = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine: Engine = create_engine(database_url, **kwargs)
        self._lock = threading.RLock()
        # Job metadata never changes after creation; caching it keeps progress
        # polls from re-parsing a 25k-domain JSON document every two seconds.
        self._job_metadata_cache: dict[str, dict] = {}

    def create_schema(self) -> None:
        metadata.create_all(self.engine)
        for index in _INDEXES:
            index.create(self.engine, checkfirst=True)
        self._migrate_legacy_classification_labels()
        self._backfill_domain_scans()

    def _backfill_domain_scans(self) -> None:
        with self._lock, self.engine.begin() as connection:
            existing = select(domain_scans.c.result_id)
            rows = (
                connection.execute(
                    select(crawl_results).where(crawl_results.c.id.not_in(existing))
                )
                .mappings()
                .all()
            )
            for row in rows:
                try:
                    result = enrich_ad_infrastructure_result(
                        AnalysisResult.model_validate_json(row["result_json"])
                    )
                except Exception:
                    continue
                connection.execute(domain_scans.insert().values(**_scan_values(row["id"], result)))

    def _migrate_legacy_classification_labels(self) -> None:
        mapping = {
            "ICP": "AD INFRA DETECTED",
            "NON ICP": "NO AD INFRA DETECTED",
            "NOT SURE": "INCONCLUSIVE",
        }
        with self._lock, self.engine.begin() as connection:
            connection.execute(
                crawl_results.update()
                .where(crawl_results.c.recommendation == "LOW_PRIORITY")
                .values(recommendation="ASK_SPEND")
            )
            for old, new in mapping.items():
                connection.execute(
                    crawl_job_rows.update()
                    .where(crawl_job_rows.c.icp_classification == old)
                    .values(icp_classification=new)
                )
            persisted = connection.execute(
                select(
                    crawl_job_rows.c.id,
                    crawl_results.c.result_json,
                ).join(
                    crawl_results,
                    crawl_results.c.id == crawl_job_rows.c.result_id,
                ).where(
                    crawl_job_rows.c.evidence_1.like("Could not verify the website:%")
                    | crawl_job_rows.c.evidence_1.like("%during after the crawler%")
                    | crawl_job_rows.c.evidence_2.like("%during after the crawler%")
                    | crawl_job_rows.c.evidence_3.like("%during after the crawler%")
                )
            ).all()
            for row in persisted:
                result = enrich_ad_infrastructure_result(
                    AnalysisResult.model_validate_json(row.result_json)
                )
                summaries = [*result.evidence_summaries, "", ""][:3]
                connection.execute(
                    crawl_job_rows.update()
                    .where(crawl_job_rows.c.id == row.id)
                    .values(
                        icp_classification=result.ad_infrastructure_result,
                        evidence_1=summaries[0],
                        evidence_2=summaries[1],
                        evidence_3=summaries[2],
                    )
                )

    def recover_interrupted_jobs(self) -> int:
        """Make process-loss visible instead of leaving jobs permanently RUNNING."""
        with self._lock, self.engine.begin() as connection:
            job_ids = list(
                connection.execute(
                    select(crawl_jobs.c.id).where(crawl_jobs.c.status == "RUNNING")
                ).scalars()
            )
            if not job_ids:
                return 0
            connection.execute(
                crawl_job_rows.update()
                .where(crawl_job_rows.c.job_id.in_(job_ids))
                .where(crawl_job_rows.c.status.in_(("PENDING", "RUNNING")))
                .values(
                    status="INCONCLUSIVE",
                    icp_classification="INCONCLUSIVE",
                    evidence_1="Job was interrupted before this website could be completed.",
                    error="process interrupted",
                )
            )
            connection.execute(
                crawl_jobs.update()
                .where(crawl_jobs.c.id.in_(job_ids))
                .values(
                    status="INCONCLUSIVE",
                    failed_domains=(
                        crawl_jobs.c.failed_domains
                        + crawl_jobs.c.total_domains
                        - crawl_jobs.c.completed_domains
                    ),
                    completed_domains=crawl_jobs.c.total_domains,
                )
            )
        return len(job_ids)

    def jobs_to_resume(self) -> list[str]:
        """Return unfinished jobs and put interrupted rows back in the queue.

        PostgreSQL is the source of truth for batches.  A Railway restart must
        therefore resume unfinished rows instead of converting a valid upload
        into hundreds of false ``INCONCLUSIVE`` results.
        """
        with self._lock, self.engine.begin() as connection:
            job_ids = list(
                connection.execute(
                    select(crawl_jobs.c.id).where(
                        crawl_jobs.c.status.in_(("PENDING", "RUNNING"))
                    )
                ).scalars()
            )
            if job_ids:
                connection.execute(
                    crawl_job_rows.update()
                    .where(crawl_job_rows.c.job_id.in_(job_ids))
                    .where(crawl_job_rows.c.status == "RUNNING")
                    .values(status="PENDING")
                )
        return job_ids

    def fail_unfinished_job(self, job_id: str, error: str) -> None:
        """Make a job-runner failure visible and leave no batch stuck forever."""
        message = " ".join(error.split())[:500]
        with self._lock, self.engine.begin() as connection:
            unfinished = connection.execute(
                select(crawl_job_rows.c.id)
                .where(crawl_job_rows.c.job_id == job_id)
                .where(crawl_job_rows.c.status.in_(("PENDING", "RUNNING")))
            ).scalars().all()
            if unfinished:
                connection.execute(
                    crawl_job_rows.update()
                    .where(crawl_job_rows.c.id.in_(unfinished))
                    .values(
                        status="INCONCLUSIVE",
                        icp_classification="INCONCLUSIVE",
                        evidence_1=f"Batch worker stopped before this website completed: {message}",
                        error=message,
                    )
                )
            job = connection.execute(
                select(crawl_jobs.c.total_domains, crawl_jobs.c.failed_domains).where(
                    crawl_jobs.c.id == job_id
                )
            ).first()
            if job:
                connection.execute(
                    crawl_jobs.update().where(crawl_jobs.c.id == job_id).values(
                        status="INCONCLUSIVE",
                        completed_domains=job.total_domains,
                        failed_domains=min(job.total_domains, job.failed_domains + len(unfinished)),
                    )
                )

    def purge_expired(self, max_age_days: int) -> dict[str, int]:
        """Delete research older than ``max_age_days`` and report what was removed.

        Batches are tied to their job's ``created_at``; standalone single-site
        results and external activity evidence are aged on their own timestamps.
        Children are removed before parents so the foreign keys stay satisfied on
        PostgreSQL as well as SQLite.

        Expired rows are selected with subqueries instead of materialised id
        lists: a large backlog passed to ``IN (...)`` exceeds PostgreSQL's 65535
        bind-parameter limit and crashes startup.
        """
        if max_age_days <= 0:
            return {"jobs": 0, "results": 0, "activity_evidence": 0}
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        removed = {"jobs": 0, "results": 0, "activity_evidence": 0}
        expired_jobs = select(crawl_jobs.c.id).where(crawl_jobs.c.created_at < cutoff)
        expired_results = select(crawl_results.c.id).where(
            crawl_results.c.job_id.in_(expired_jobs)
            | (
                crawl_results.c.job_id.is_(None)
                & (crawl_results.c.completed_at < cutoff)
            )
        )
        with self._lock, self.engine.begin() as connection:
            expired_job_ids = list(connection.execute(expired_jobs).scalars())
            connection.execute(
                domain_scans.delete().where(
                    domain_scans.c.result_id.in_(expired_results)
                )
            )
            connection.execute(
                crawl_job_rows.delete().where(
                    crawl_job_rows.c.job_id.in_(expired_jobs)
                )
            )
            removed["results"] = connection.execute(
                crawl_results.delete().where(
                    crawl_results.c.id.in_(expired_results)
                )
            ).rowcount or 0
            removed["jobs"] = connection.execute(
                crawl_jobs.delete().where(crawl_jobs.c.created_at < cutoff)
            ).rowcount or 0
            removed["activity_evidence"] = connection.execute(
                activity_evidence.delete().where(activity_evidence.c.created_at < cutoff)
            ).rowcount or 0
        for job_id in expired_job_ids:
            self._job_metadata_cache.pop(job_id, None)
        return removed

    def create_job(
        self,
        domains: list[str],
        *,
        source_rows: list[dict[str, str]] | None = None,
        source_columns: list[str] | None = None,
        website_column: str = "Website",
    ) -> JobSummary:
        job_id = f"job_{uuid.uuid4().hex}"
        created_at = datetime.now(UTC)
        if source_rows is None:
            source_rows = [{website_column: domain} for domain in domains]
        if len(source_rows) != len(domains):
            raise ValueError("source row count must match domain count")
        source_columns = source_columns or list(source_rows[0] if source_rows else [website_column])
        metadata_json = {
            "domains": domains,
            "source_columns": source_columns,
            "website_column": website_column,
        }
        with self._lock, self.engine.begin() as connection:
            connection.execute(
                crawl_jobs.insert().values(
                    id=job_id,
                    created_at=created_at,
                    total_domains=len(domains),
                    completed_domains=0,
                    failed_domains=0,
                    status="PENDING",
                    input_json=json.dumps(metadata_json),
                )
            )
            connection.execute(
                crawl_job_rows.insert(),
                [
                    {
                        "id": f"row_{uuid.uuid4().hex}",
                        "job_id": job_id,
                        "row_index": index,
                        "website": domain,
                        "original_json": json.dumps(
                            {column: str(source.get(column, "") or "") for column in source_columns}
                        ),
                        "status": "PENDING",
                        "icp_classification": "INCONCLUSIVE",
                        "evidence_1": "",
                        "evidence_2": "",
                        "evidence_3": "",
                    }
                    for index, (domain, source) in enumerate(zip(domains, source_rows, strict=True))
                ],
            )
        return JobSummary(
            job_id=job_id,
            created_at=created_at,
            total_domains=len(domains),
            completed_domains=0,
            failed_domains=0,
            status="PENDING",
        )

    def set_job_status(self, job_id: str, status: str) -> None:
        with self._lock, self.engine.begin() as connection:
            connection.execute(
                crawl_jobs.update().where(crawl_jobs.c.id == job_id).values(status=status)
            )

    def save_result(
        self,
        result: AnalysisResult,
        job_id: str | None = None,
        job_row_id: str | None = None,
    ) -> StoredResult:
        result = enrich_ad_infrastructure_result(result)
        result_id = f"res_{uuid.uuid4().hex}"
        if job_id and not job_row_id:
            raise ValueError("job_row_id is required for batch results")
        with self._lock, self.engine.begin() as connection:
            # Record the scan first so the history computed below includes it,
            # then write the fully scored result once.
            connection.execute(domain_scans.insert().values(**_scan_values(result_id, result)))
            history, activity = self._research_parts(
                connection, _result_domain_key(result)
            )
            result = attach_research_context(result, history, activity)
            connection.execute(
                crawl_results.insert().values(
                    id=result_id,
                    job_id=job_id,
                    domain=result.domain,
                    started_at=result.started_at,
                    completed_at=result.completed_at,
                    status=result.status,
                    pages_crawled=result.pages_crawled,
                    overall_confidence=result.advertising_infrastructure_confidence,
                    recommendation=result.recommendation,
                    result_json=json.dumps(jsonable_result(result)),
                    error=result.error,
                )
            )
            if job_id:
                self._complete_job_row(connection, job_id, job_row_id, result_id, result)
        return StoredResult(result_id=result_id, job_id=job_id, result=result)

    def link_job_row_result(self, job_id: str, job_row_id: str, stored: StoredResult) -> None:
        """Finish a duplicate-website row with a scan already saved in this job."""
        with self._lock, self.engine.begin() as connection:
            self._complete_job_row(
                connection, job_id, job_row_id, stored.result_id, stored.result
            )

    def _complete_job_row(
        self,
        connection,
        job_id: str,
        job_row_id: str,
        result_id: str,
        result: AnalysisResult,
    ) -> None:
        summaries = [*result.evidence_summaries, "", ""][:3]
        connection.execute(
            crawl_job_rows.update()
            .where(crawl_job_rows.c.id == job_row_id)
            .where(crawl_job_rows.c.job_id == job_id)
            .values(
                status=result.status,
                result_id=result_id,
                icp_classification=result.ad_infrastructure_result,
                evidence_1=summaries[0],
                evidence_2=summaries[1],
                evidence_3=summaries[2],
                error=result.error,
            )
        )
        self._increment_job(
            connection, job_id, failed=result.status in {"CRAWL_FAILED", "INCONCLUSIVE"}
        )

    def mark_job_row_running(self, job_id: str, row_id: str) -> None:
        with self._lock, self.engine.begin() as connection:
            connection.execute(
                crawl_job_rows.update()
                .where(crawl_job_rows.c.id == row_id)
                .where(crawl_job_rows.c.job_id == job_id)
                .values(status="RUNNING")
            )

    def fail_job_row(self, job_id: str, row_id: str, error: str) -> None:
        """Finish a row even when an unexpected error escaped the detector."""
        message = " ".join(error.split())[:500]
        with self._lock, self.engine.begin() as connection:
            connection.execute(
                crawl_job_rows.update()
                .where(crawl_job_rows.c.id == row_id)
                .where(crawl_job_rows.c.job_id == job_id)
                .values(
                    status="CRAWL_FAILED",
                    icp_classification="INCONCLUSIVE",
                    evidence_1=f"Could not verify the website: {message}",
                    error=message,
                )
            )
            self._increment_job(connection, job_id, failed=True)

    def get_result(self, result_id: str) -> StoredResult | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(crawl_results).where(crawl_results.c.id == result_id))
                .mappings()
                .first()
            )
        if not row:
            return None
        result = enrich_ad_infrastructure_result(
            AnalysisResult.model_validate_json(row["result_json"])
        )
        with self.engine.connect() as connection:
            history, activity = self._research_parts(connection, _result_domain_key(result))
        result = attach_research_context(result, history, activity)
        return StoredResult(
            result_id=row["id"],
            job_id=row["job_id"],
            result=result,
        )

    def add_activity_evidence(
        self, payload: ActivityEvidenceCreate
    ) -> ActivityEvidenceRecord:
        evidence_id = f"act_{uuid.uuid4().hex}"
        created_at = datetime.now(UTC)
        domain_key = _domain_key(payload.domain)
        values = {
            "id": evidence_id,
            "domain_key": domain_key,
            "domain": payload.domain,
            "source": payload.source,
            "status": payload.status,
            "observed_at": payload.observed_at,
            "created_at": created_at,
            "advertiser_name": payload.advertiser_name,
            "active_ads_count": payload.active_ads_count,
            "total_ads_count": payload.total_ads_count,
            "first_ad_seen_at": (
                payload.first_ad_seen_at.isoformat() if payload.first_ad_seen_at else None
            ),
            "last_ad_seen_at": (
                payload.last_ad_seen_at.isoformat() if payload.last_ad_seen_at else None
            ),
            "landing_page_urls_json": json.dumps(payload.landing_page_urls),
            "source_url": payload.source_url,
            "notes": payload.notes,
        }
        with self._lock, self.engine.begin() as connection:
            connection.execute(activity_evidence.insert().values(**values))
        return ActivityEvidenceRecord(
            **payload.model_dump(),
            id=evidence_id,
            domain_key=domain_key,
            created_at=created_at,
        )

    def list_activity_evidence(self, domain: str) -> list[ActivityEvidenceRecord]:
        domain_key = _domain_key(domain)
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(activity_evidence)
                    .where(activity_evidence.c.domain_key == domain_key)
                    .order_by(activity_evidence.c.observed_at.desc())
                )
                .mappings()
                .all()
            )
        return [_activity_record(row) for row in rows]

    def get_landing_urls(self, domain: str, *, limit: int = 5) -> list[str]:
        records = self.list_activity_evidence(domain)
        urls: list[str] = []
        for record in records:
            if record.status not in {"ACTIVE", "RECENT"}:
                continue
            for url in record.landing_page_urls:
                if url not in urls:
                    urls.append(url)
                if len(urls) >= limit:
                    return urls
        return urls

    def get_domain_research(self, domain: str) -> DomainResearchSummary:
        domain_key = _domain_key(domain)
        with self.engine.connect() as connection:
            history, activity = self._research_parts(connection, domain_key)
            latest = (
                connection.execute(
                    select(crawl_results)
                    .join(domain_scans, domain_scans.c.result_id == crawl_results.c.id)
                    .where(domain_scans.c.domain_key == domain_key)
                    .order_by(domain_scans.c.observed_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if latest:
            result = enrich_ad_infrastructure_result(
                AnalysisResult.model_validate_json(latest["result_json"])
            )
            result = attach_research_context(result, history, activity)
            return DomainResearchSummary(
                domain=domain_key,
                infrastructure_result=result.ad_infrastructure_result,
                infrastructure_confidence=result.advertising_infrastructure_confidence,
                infrastructure_freshness=result.infrastructure_freshness,
                history=history,
                external_activity=activity,
                primary_advertising_confidence=result.primary_advertising_confidence,
                combined_activity_confidence=result.combined_activity_confidence,
                combined_confidence_reasons=result.combined_confidence_reasons,
            )
        if activity.confidence == "CONFIRMED_CURRENT":
            combined = "MEDIUM"
            reasons = [
                "Current external advertising activity was found, but the website has not been successfully assessed."
            ]
        elif activity.confidence in {"RECENT_ACTIVITY", "POSSIBLE"}:
            combined = "LOW"
            reasons = ["External activity exists, but current website infrastructure is unknown."]
        else:
            combined = "INCONCLUSIVE"
            reasons = ["No website scan is available for a combined assessment."]
        return DomainResearchSummary(
            domain=domain_key,
            history=history,
            external_activity=activity,
            primary_advertising_confidence="INCONCLUSIVE",
            combined_activity_confidence=combined,
            combined_confidence_reasons=reasons,
        )

    def _research_parts(self, connection, domain_key: str):
        scan_rows = (
            connection.execute(
                select(domain_scans)
                .where(domain_scans.c.domain_key == domain_key)
                .order_by(domain_scans.c.observed_at)
            )
            .mappings()
            .all()
        )
        scans = [
            {
                **dict(row),
                "runtime_platforms": json.loads(row["runtime_platforms_json"]),
                "identifiers": json.loads(row["identifiers_json"]),
            }
            for row in scan_rows
        ]
        evidence_rows = (
            connection.execute(
                select(activity_evidence)
                .where(activity_evidence.c.domain_key == domain_key)
                .order_by(activity_evidence.c.observed_at.desc())
            )
            .mappings()
            .all()
        )
        history = build_history(scans)
        activity = summarize_activity([_activity_record(row) for row in evidence_rows])
        return history, activity

    def _research_parts_many(self, connection, domain_keys: set[str]):
        if not domain_keys:
            return {}
        scans_by_domain: dict[str, list[dict]] = {key: [] for key in domain_keys}
        evidence_by_domain: dict[str, list[ActivityEvidenceRecord]] = {
            key: [] for key in domain_keys
        }
        scan_rows = (
            connection.execute(
                select(domain_scans)
                .where(domain_scans.c.domain_key.in_(domain_keys))
                .order_by(domain_scans.c.observed_at)
            )
            .mappings()
            .all()
        )
        for row in scan_rows:
            scans_by_domain[row["domain_key"]].append(
                {
                    **dict(row),
                    "runtime_platforms": json.loads(row["runtime_platforms_json"]),
                    "identifiers": json.loads(row["identifiers_json"]),
                }
            )
        evidence_rows = (
            connection.execute(
                select(activity_evidence)
                .where(activity_evidence.c.domain_key.in_(domain_keys))
                .order_by(activity_evidence.c.observed_at.desc())
            )
            .mappings()
            .all()
        )
        for row in evidence_rows:
            evidence_by_domain[row["domain_key"]].append(_activity_record(row))
        return {
            key: (
                build_history(scans_by_domain[key]),
                summarize_activity(evidence_by_domain[key]),
            )
            for key in domain_keys
        }

    def get_job(self, job_id: str) -> JobSummary | None:
        with self.engine.connect() as connection:
            job = (
                connection.execute(select(crawl_jobs).where(crawl_jobs.c.id == job_id))
                .mappings()
                .first()
            )
            if not job:
                return None
            result_ids = list(
                connection.execute(
                    select(crawl_job_rows.c.result_id)
                    .where(crawl_job_rows.c.job_id == job_id)
                    .where(crawl_job_rows.c.result_id.is_not(None))
                    .order_by(crawl_job_rows.c.row_index)
                ).scalars()
            )
        return JobSummary(
            job_id=job["id"],
            created_at=job["created_at"],
            total_domains=job["total_domains"],
            completed_domains=job["completed_domains"],
            failed_domains=job["failed_domains"],
            status=job["status"],
            result_ids=result_ids,
        )

    def list_jobs(self, *, limit: int = 20) -> list[JobSummary]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(crawl_jobs)
                .order_by(crawl_jobs.c.created_at.desc())
                .limit(limit)
            ).mappings().all()
        return [
            JobSummary(
                job_id=row["id"],
                created_at=row["created_at"],
                total_domains=row["total_domains"],
                completed_domains=row["completed_domains"],
                failed_domains=row["failed_domains"],
                status=row["status"],
            )
            for row in rows
        ]

    def get_job_domains(self, job_id: str) -> list[str] | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(crawl_jobs.c.input_json).where(crawl_jobs.c.id == job_id)
            ).scalar_one_or_none()
        if not value:
            return None
        parsed = json.loads(value)
        return parsed.get("domains", []) if isinstance(parsed, dict) else parsed

    def get_job_rows_for_run(self, job_id: str) -> list[tuple[str, str]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(crawl_job_rows.c.id, crawl_job_rows.c.website)
                .where(crawl_job_rows.c.job_id == job_id)
                .where(crawl_job_rows.c.status == "PENDING")
                .order_by(crawl_job_rows.c.row_index)
            ).all()
        return [(row.id, row.website) for row in rows]

    def get_job_rows(self, job_id: str, *, offset: int = 0, limit: int = 500) -> JobRowsPage | None:
        metadata = self._get_job_metadata(job_id)
        if metadata is None:
            return None
        with self.engine.connect() as connection:
            total_rows = connection.execute(
                select(crawl_jobs.c.total_domains).where(crawl_jobs.c.id == job_id)
            ).scalar_one()
            rows = (
                connection.execute(
                    select(
                        crawl_job_rows,
                        crawl_results.c.result_json.label("_result_json"),
                    )
                    .outerjoin(
                        crawl_results,
                        crawl_results.c.id == crawl_job_rows.c.result_id,
                    )
                    .where(crawl_job_rows.c.job_id == job_id)
                    .order_by(crawl_job_rows.c.row_index)
                    .offset(offset)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            parsed: dict[str, AnalysisResult] = {}
            domain_keys: set[str] = set()
            for row in rows:
                if not row["_result_json"]:
                    continue
                result = enrich_ad_infrastructure_result(
                    AnalysisResult.model_validate_json(row["_result_json"])
                )
                parsed[row["id"]] = result
                domain_keys.add(_result_domain_key(result))
            contexts = self._research_parts_many(connection, domain_keys)
        return JobRowsPage(
            job_id=job_id,
            website_column=metadata["website_column"],
            source_columns=metadata["source_columns"],
            total_rows=total_rows,
            rows=[
                self._job_row_model(
                    row,
                    attach_research_context(
                        parsed[row["id"]], *contexts[_result_domain_key(parsed[row["id"]])]
                    )
                    if row["id"] in parsed
                    else None,
                )
                for row in rows
            ],
        )

    def list_job_results(self, job_id: str) -> list[StoredResult]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(crawl_results)
                    .where(crawl_results.c.job_id == job_id)
                    .order_by(crawl_results.c.completed_at)
                )
                .mappings()
                .all()
            )
        return [
            StoredResult(
                result_id=row["id"],
                job_id=row["job_id"],
                result=enrich_ad_infrastructure_result(
                    AnalysisResult.model_validate_json(row["result_json"])
                ),
            )
            for row in rows
        ]

    def iter_job_rows(self, job_id: str, *, chunk_size: int = 500):
        """Yield a job's rows page by page so exports never load a whole job."""
        offset = 0
        while True:
            page = self.get_job_rows(job_id, offset=offset, limit=chunk_size)
            if page is None or not page.rows:
                return
            yield page
            offset += len(page.rows)
            if offset >= page.total_rows:
                return

    def _get_job_metadata(self, job_id: str) -> dict | None:
        cached = self._job_metadata_cache.get(job_id)
        if cached is not None:
            return cached
        with self.engine.connect() as connection:
            value = connection.execute(
                select(crawl_jobs.c.input_json).where(crawl_jobs.c.id == job_id)
            ).scalar_one_or_none()
        if value is None:
            return None
        parsed = json.loads(value)
        if isinstance(parsed, list):
            parsed = {}
        # Keep only the small fields; the domain list can hold 25k entries.
        metadata = {
            "source_columns": parsed.get("source_columns") or ["Website"],
            "website_column": parsed.get("website_column") or "Website",
        }
        if len(self._job_metadata_cache) >= 256:
            self._job_metadata_cache.clear()
        self._job_metadata_cache[job_id] = metadata
        return metadata

    @staticmethod
    def _job_row_model(row, result: AnalysisResult | None = None) -> JobRow:
        return JobRow(
            row_id=row["id"],
            row_index=row["row_index"],
            website=row["website"],
            original_data=json.loads(row["original_json"]),
            status=row["status"],
            result_id=row["result_id"],
            ad_infrastructure_result=row["icp_classification"],
            infrastructure_confidence=(
                result.advertising_infrastructure_confidence if result else "INCONCLUSIVE"
            ),
            infrastructure_freshness=(
                result.infrastructure_freshness if result else "INCONCLUSIVE"
            ),
            primary_advertising_confidence=(
                result.primary_advertising_confidence if result else "INCONCLUSIVE"
            ),
            current_ad_activity=(
                result.external_activity.confidence if result else "NOT_CHECKED"
            ),
            combined_activity_confidence=(
                result.combined_activity_confidence if result else "INCONCLUSIVE"
            ),
            detected_platforms=(
                sorted(key for key, item in result.platforms.items() if item.detected)
                if result
                else []
            ),
            runtime_platforms=(result.runtime_platforms if result else []),
            pages_crawled=(result.pages_crawled if result else 0),
            scan_count=result.history.total_scans if result else 0,
            last_runtime_signal_at=(result.history.last_runtime_signal_at if result else None),
            evidence_1=row["evidence_1"],
            evidence_2=row["evidence_2"],
            evidence_3=row["evidence_3"],
            error=row["error"],
        )

    def _increment_job(self, connection, job_id: str, *, failed: bool) -> None:
        row = connection.execute(
            select(
                crawl_jobs.c.completed_domains,
                crawl_jobs.c.failed_domains,
                crawl_jobs.c.total_domains,
            ).where(crawl_jobs.c.id == job_id)
        ).one()
        completed = row.completed_domains + 1
        values = {
            "completed_domains": completed,
            "failed_domains": row.failed_domains + int(failed),
        }
        if completed >= row.total_domains:
            values["status"] = "COMPLETED"
        connection.execute(crawl_jobs.update().where(crawl_jobs.c.id == job_id).values(**values))

    def dispose(self) -> None:
        self.engine.dispose()


def _normalize_database_url(value: str) -> str:
    # Railway commonly exposes postgresql:// while SQLAlchemy otherwise looks
    # for psycopg2. This service intentionally ships psycopg v3.
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    return value


def _domain_key(value: str) -> str:
    candidate = value.strip().lower()
    if not candidate:
        return ""
    try:
        host = urlsplit(coerce_input_url(candidate)).hostname or candidate
    except Exception:
        host = candidate.split("/")[0]
    return registrable_domain(host.removeprefix("www."))


def _scan_values(result_id: str, result: AnalysisResult) -> dict:
    identifiers = {
        platform: item.identifiers
        for platform, item in result.platforms.items()
        if item.identifiers
    }
    return {
        "result_id": result_id,
        "domain_key": _result_domain_key(result),
        "observed_at": result.completed_at,
        "status": result.status,
        "infrastructure_confidence": result.advertising_infrastructure_confidence,
        "infrastructure_result": result.ad_infrastructure_result,
        "infrastructure_freshness": result.infrastructure_freshness,
        "runtime_platforms_json": json.dumps(result.runtime_platforms),
        "identifiers_json": json.dumps(identifiers),
    }


def _activity_record(row) -> ActivityEvidenceRecord:
    first_seen = date.fromisoformat(row["first_ad_seen_at"]) if row["first_ad_seen_at"] else None
    last_seen = date.fromisoformat(row["last_ad_seen_at"]) if row["last_ad_seen_at"] else None
    return ActivityEvidenceRecord(
        id=row["id"],
        domain_key=row["domain_key"],
        domain=row["domain"],
        source=row["source"],
        status=row["status"],
        observed_at=row["observed_at"],
        created_at=row["created_at"],
        advertiser_name=row["advertiser_name"],
        active_ads_count=row["active_ads_count"],
        total_ads_count=row["total_ads_count"],
        first_ad_seen_at=first_seen,
        last_ad_seen_at=last_seen,
        landing_page_urls=json.loads(row["landing_page_urls_json"]),
        source_url=row["source_url"],
        notes=row["notes"],
    )


def _result_domain_key(result: AnalysisResult) -> str:
    return _domain_key(result.domain or result.requested_url)
