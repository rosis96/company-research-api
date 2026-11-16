from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse

from ad_research.models import (
    ActivityEvidenceCreate,
    ActivityEvidenceRecord,
    AnalyzeRequest,
    BatchRequest,
    DomainResearchSummary,
    JobCreated,
    JobRowsPage,
    JobSummary,
    StoredResult,
)

router = APIRouter()


@router.get("/", include_in_schema=False)
async def home(request: Request) -> FileResponse:
    return FileResponse(request.app.state.static_dir / "index.html")


@router.get("/v1/health", include_in_schema=False)
@router.get("/api/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "service": "ad-research",
        "version": request.app.state.version,
        "max_batch_size": settings.max_batch_size,
        "max_upload_bytes": settings.max_upload_bytes,
        "crawl_concurrency": settings.concurrency,
    }


@router.post("/api/analyze", response_model=StoredResult)
async def analyze(payload: AnalyzeRequest, request: Request) -> StoredResult:
    result = await request.app.state.jobs.analyze_one(
        payload.url, landing_urls=payload.landing_page_urls
    )
    return request.app.state.store.save_result(result)


@router.post("/api/activity-evidence", response_model=ActivityEvidenceRecord, status_code=201)
async def add_activity_evidence(
    payload: ActivityEvidenceCreate, request: Request
) -> ActivityEvidenceRecord:
    return request.app.state.store.add_activity_evidence(payload)


@router.get("/api/activity-evidence", response_model=list[ActivityEvidenceRecord])
async def list_activity_evidence(domain: str, request: Request) -> list[ActivityEvidenceRecord]:
    return request.app.state.store.list_activity_evidence(domain)


@router.get("/api/research", response_model=DomainResearchSummary)
async def domain_research(domain: str, request: Request) -> DomainResearchSummary:
    return request.app.state.store.get_domain_research(domain)


@router.post("/api/jobs", response_model=JobCreated, status_code=202)
async def create_job(payload: BatchRequest, request: Request) -> JobCreated:
    settings = request.app.state.settings
    if len(payload.domains) > settings.max_batch_size:
        raise HTTPException(422, _batch_limit_message(len(payload.domains), settings))
    job = await run_in_threadpool(request.app.state.store.create_job, payload.domains)
    request.app.state.jobs.start(job.job_id)
    return JobCreated(
        job_id=job.job_id,
        status=job.status,
        total_domains=job.total_domains,
        website_column="Website",
        source_columns=["Website"],
    )


@router.get("/api/jobs", response_model=list[JobSummary])
async def list_jobs(
    request: Request, limit: int = Query(default=20, ge=1, le=100)
) -> list[JobSummary]:
    return request.app.state.store.list_jobs(limit=limit)


@router.post("/api/jobs/upload", response_model=JobCreated, status_code=202)
async def upload_job(request: Request, file: UploadFile) -> JobCreated:
    raw = await file.read()
    settings = request.app.state.settings
    if len(raw) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes // 1_000_000
        raise HTTPException(413, f"CSV file is too large; maximum upload is {limit_mb} MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "CSV must be UTF-8 encoded") from exc
    # Parsing and inserting 25k rows takes long enough to stall live crawls if
    # it ran on the event loop.
    columns, website_column, rows, domains = await run_in_threadpool(_parse_csv, text)
    if len(domains) > settings.max_batch_size:
        raise HTTPException(422, _batch_limit_message(len(domains), settings))
    job = await run_in_threadpool(
        request.app.state.store.create_job,
        domains,
        source_rows=rows,
        source_columns=columns,
        website_column=website_column,
    )
    request.app.state.jobs.start(job.job_id)
    return JobCreated(
        job_id=job.job_id,
        status=job.status,
        total_domains=job.total_domains,
        website_column=website_column,
        source_columns=columns,
    )


@router.get("/api/jobs/{job_id}", response_model=JobSummary)
async def get_job(job_id: str, request: Request) -> JobSummary:
    job = request.app.state.store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.get("/api/jobs/{job_id}/rows", response_model=JobRowsPage)
async def get_job_rows(
    job_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=500),
) -> JobRowsPage:
    rows = await run_in_threadpool(
        request.app.state.store.get_job_rows, job_id, offset=offset, limit=limit
    )
    if rows is None:
        raise HTTPException(404, "job not found")
    return rows


@router.get("/api/results/{result_id}", response_model=StoredResult)
async def get_result(result_id: str, request: Request) -> StoredResult:
    result = request.app.state.store.get_result(result_id)
    if not result:
        raise HTTPException(404, "result not found")
    return result


@router.post("/api/results/{result_id}/rerun", response_model=StoredResult)
async def rerun_result(result_id: str, request: Request) -> StoredResult:
    previous = request.app.state.store.get_result(result_id)
    if not previous:
        raise HTTPException(404, "result not found")
    result = await request.app.state.jobs.analyze_one(previous.result.requested_url)
    return request.app.state.store.save_result(result)


@router.get("/api/jobs/{job_id}/csv")
async def export_job_csv(job_id: str, request: Request) -> StreamingResponse:
    store = request.app.state.store
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    first = store.get_job_rows(job_id, limit=1)
    if first is None:
        raise HTTPException(404, "job not found")
    output_names = _output_column_names(first.source_columns)
    fieldnames = [*first.source_columns, *output_names.values()]

    def generate():
        # A sync generator runs in Starlette's thread pool, and each chunk holds
        # at most 500 rows, so a 25k-row export neither blocks crawls nor has to
        # fit in memory.
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for page in store.iter_job_rows(job_id):
            for row in page.rows:
                writer.writerow(_export_row(row, output_names))
            yield stream.getvalue()
            stream.seek(0)
            stream.truncate(0)
        if stream.tell():
            yield stream.getvalue()

    headers = {"Content-Disposition": f'attachment; filename="{job_id}.csv"'}
    return StreamingResponse(generate(), media_type="text/csv", headers=headers)


def _export_row(row, output_names: dict[str, str]) -> dict:
    output = dict(row.original_data)
    output.update(
        {
            output_names["classification"]: row.ad_infrastructure_result,
            output_names["primary_confidence"]: row.primary_advertising_confidence,
            output_names["infrastructure_confidence"]: row.infrastructure_confidence,
            output_names["infrastructure_freshness"]: row.infrastructure_freshness,
            output_names["activity"]: row.current_ad_activity,
            output_names["combined_confidence"]: row.combined_activity_confidence,
            output_names["detected_platforms"]: ", ".join(row.detected_platforms),
            output_names["runtime_platforms"]: ", ".join(row.runtime_platforms),
            output_names["pages_crawled"]: row.pages_crawled,
            output_names["scan_count"]: row.scan_count,
            output_names["last_runtime"]: (
                row.last_runtime_signal_at.isoformat() if row.last_runtime_signal_at else ""
            ),
            output_names["evidence_1"]: row.evidence_1,
            output_names["evidence_2"]: row.evidence_2,
            output_names["evidence_3"]: row.evidence_3,
            output_names["error_notes"]: row.error or "",
        }
    )
    return output


def _batch_limit_message(count: int, settings) -> str:
    # Name the variable: an old Railway value silently overriding the code
    # default was the reason uploads stopped at 5,000 rows.
    return (
        f"This batch has {count:,} rows but this server accepts at most "
        f"{settings.max_batch_size:,}. Raise AD_RESEARCH_MAX_BATCH_SIZE to allow more."
    )


def _parse_csv(content: str) -> tuple[list[str], str, list[dict[str, str]], list[str]]:
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise HTTPException(422, "CSV is empty")
    columns = [column.strip() for column in reader.fieldnames]
    if any(not column for column in columns):
        raise HTTPException(422, "CSV contains an unnamed column")
    if len({column.casefold() for column in columns}) != len(columns):
        raise HTTPException(422, "CSV contains duplicate column names")
    accepted = {
        "website",
        "domain",
        "url",
        "website domain",
        "company website",
        "company domain",
        "organization website",
        "organization primary domain",
    }
    website_column = next(
        (column for column in columns if _normalize_header(column) in accepted), None
    )
    if website_column is None:
        raise HTTPException(
            422,
            "No website column found. Use Website, Domain, URL, Company Website, or Company Domain.",
        )
    rows = [
        {
            column: str(raw.get(original, "") or "")
            for column, original in zip(columns, reader.fieldnames, strict=True)
        }
        for raw in reader
    ]
    if not rows:
        raise HTTPException(422, "CSV has a header but no lead rows")
    domains = [row.get(website_column, "").strip() for row in rows]
    return columns, website_column, rows, domains


def _normalize_header(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", " ").replace("/", " ").split())


def _output_column_names(source_columns: list[str]) -> dict[str, str]:
    used = {column.casefold() for column in source_columns}

    def unique(preferred: str) -> str:
        if preferred.casefold() not in used:
            used.add(preferred.casefold())
            return preferred
        candidate = f"{preferred} (Ad Research)"
        counter = 2
        while candidate.casefold() in used:
            candidate = f"{preferred} (Ad Research {counter})"
            counter += 1
        used.add(candidate.casefold())
        return candidate

    return {
        "primary_confidence": unique("Primary Advertising Confidence"),
        "classification": unique("Ad Infrastructure Result"),
        "infrastructure_confidence": unique("Infrastructure Confidence"),
        "infrastructure_freshness": unique("Infrastructure Freshness"),
        "activity": unique("Current Ad Activity"),
        "combined_confidence": unique("Combined Activity Confidence"),
        "detected_platforms": unique("Detected Ad Platforms"),
        "runtime_platforms": unique("Runtime Ad Platforms"),
        "pages_crawled": unique("Pages Crawled"),
        "scan_count": unique("Research Scan Count"),
        "last_runtime": unique("Last Runtime Signal At"),
        "evidence_1": unique("Evidence 1"),
        "evidence_2": unique("Evidence 2"),
        "evidence_3": unique("Evidence 3"),
        "error_notes": unique("Error / Notes"),
    }
