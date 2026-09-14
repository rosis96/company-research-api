# Paid Advertising Infrastructure & Activity Detector

This is a separate application inside the repository. It does not import or
start the existing `app.main` FastAPI application, its batch runner, or any
production scheduler. Its entry point is `ad_research.main:app` and its future
Railway image is `Dockerfile.ad-research`.

It now keeps two questions separate:

1. **What paid-advertising machinery did a real browser observe today?**
2. **Is there dated external evidence that the advertiser currently has live ads?**

It never turns either kind of evidence into an estimated monthly spend or a
final Unicorn Marketers qualification decision.

## What the browser does

For each domain it creates a fresh Chromium context, installs request monitoring
before navigation, loads the homepage, waits for late JavaScript, records scripts,
requests, cookies, selected runtime globals and source markers, scrolls, attempts
an obvious cookie-consent acceptance, then repeats its observations. It chooses
up to `AD_RESEARCH_MAX_PAGES` useful same-site pages from navigation and common
commercial paths. It never submits a form, purchase, booking, or contact detail.

Platform rules live in `ad_research/signatures/*.json`. Add a file with network,
script, cookie, global, DOM and identifier patterns to add a platform without
changing crawler control flow.

Classification is conservative:

- `CONFIRMED`: an advertising-specific runtime request was initiated.
- `LIKELY`: an advertising-specific script, ID, global or source marker exists.
- `POSSIBLE`: supporting evidence such as an attribution cookie only.
- `NOT_DETECTED`: no matching evidence in a conclusive crawl.
- Overall `HIGH`: at least two confirmed platforms, or one confirmed platform
  firing on two strategically different pages.
- Overall `MEDIUM`: one confirmed platform, or two likely platforms.
- `GTM` is reported separately and never increases advertising confidence.
- GA4 (`G-...` or analytics requests) has no Google Ads signature. Google Ads
  requires an `AW-...` marker or advertising/conversion endpoint.

Every completed result also receives an infrastructure-freshness label:

- `RUNTIME_ACTIVE`: an advertising-specific request actually fired in this scan.
- `TAG_LOADED`: an advertising tag loaded, but no advertising event was observed.
- `SOURCE_ONLY`: an ID or marker existed only in source/DOM. This may be stale.
- `COOKIE_ONLY`: only a supporting attribution cookie was found.
- `NOT_OBSERVED`: no advertising-specific evidence was observed.
- `INCONCLUSIVE`: the site could not be inspected reliably.

`RUNTIME_ACTIVE` means the machinery worked during this browser session. It does
not prove that an ad campaign is currently purchasing traffic. Likewise, one or
several pixel IDs do not increase current-activity confidence by themselves:
companies legitimately use multiple pixels for brands, regions, agencies,
funnels, migrations, or testing, and abandoned IDs can remain installed.

For cold-outreach CSVs, the browser-observation label is:

- `AD INFRA DETECTED`: the crawl completed and browser evidence is `HIGH` or
  `MEDIUM`.
- `NO AD INFRA DETECTED`: the crawl completed but supported browser evidence was
  `LOW` or `NONE`. This does **not** mean the company does not advertise.
- `INCONCLUSIVE`: the site could not be inspected reliably (`CRAWL_FAILED` or
  `INCONCLUSIVE`). These rows must not be treated as negative prospects.
- `NO WEBSITE`: the supplied domain, its www/apex alternative, and HTTPS/HTTP
  alternatives all produced stable no-site signals such as DNS failure, 404/410,
  or connection refusal. Bot blocks, rate limits, TLS errors, and timeouts remain
  `INCONCLUSIVE` because they do not prove the website is inactive.

Actual advertising activity and Unicorn Marketers ICP qualification are separate
downstream decisions. The activity layer can store dated, attributable evidence
from Meta Ad Library, Google Ads Transparency Center, or another permitted
external source. V2 deliberately supports manual/API evidence entry rather than
pretending that a fragile unauthenticated scrape is authoritative.
`LOW`, `NONE`, and `INCONCLUSIVE` browser evidence recommend `ASK_SPEND`; they do
not automatically deprioritize or disqualify the lead.

Current-activity claims decay with age. Fresh active evidence (up to 45 days) is
`CONFIRMED_CURRENT`; active/recent evidence up to 120 days is `RECENT_ACTIVITY`;
older evidence becomes `POSSIBLE`. `NOT_FOUND` means only that a named source was
checked at a recorded time and found nothing—it is not proof the company does not
advertise elsewhere.

The combined confidence is strongest when independent sources agree: a current
ad-library record plus runtime browser evidence can reach `HIGH`; current external
activity without observable website infrastructure is `MEDIUM`; browser
infrastructure alone cannot exceed `MEDIUM`. This prevents an old pixel from
being presented as proof of current advertising.

`before_consent` in technical diagnostics means the request appeared during the
initial load of a fresh browser context, before any crawler consent action. The
page might not have presented a banner at all. It is not a legal conclusion about whether consent was required,
previously established, region-dependent, or correctly implemented.

`CRAWL_FAILED` and `INCONCLUSIVE` always produce overall `INCONCLUSIVE`, never
`NONE`.

## Local setup

Python 3.12 and Chromium are required.

```bash
cd company-research-api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium
cp .env.ad-research.example .env
uvicorn ad_research.main:app --host 127.0.0.1 --port 8010 --reload
```

Open <http://127.0.0.1:8010>. The OpenAPI page is at
<http://127.0.0.1:8010/docs>.

The local V1 intentionally has no login. Bind it to `127.0.0.1` as shown and do
not expose it publicly. Add service authentication before a Railway deployment.

Before using the UI, the phase-one diagnostic can be run directly:

```bash
python scripts/ad_research_one.py https://example.com
```

It prints the full evidence report, including precise matching request URLs,
script evidence, cookie/global/source evidence, consent phase and per-page
diagnostics.

## API

- `POST /api/analyze` — body: `{"url":"https://company.com", "landing_page_urls":[]}`
- `POST /api/activity-evidence` — save a dated external activity record
- `GET /api/activity-evidence?domain=company.com` — list external records
- `GET /api/research?domain=company.com` — combined history/activity summary
- `POST /api/jobs` — body: `{"domains":["one.com","two.com"]}`
- `POST /api/jobs/upload` — multipart CSV; accepts `website`, `domain` or `url`
- `GET /api/jobs/{job_id}` — progress and result IDs
- `GET /api/jobs/{job_id}/rows` — original lead rows plus live row status
- `GET /api/results/{result_id}` — complete auditable evidence
- `POST /api/results/{result_id}/rerun` — fresh standalone rerun
- `GET /api/jobs/{job_id}/csv` — flattened download

Concurrency is process-wide and defaults to three simultaneous domains. One
browser process is reused; every domain gets an isolated browser context. The
future container deliberately runs one Uvicorn worker so the configured
concurrency and browser lifecycle remain real process-wide limits.

## Apollo and lead-list CSV behavior

The uploaded file is stored as ordered job rows, not deduplicated domains. Every
row receives its own row ID, crawl, result ID and output—even when ten rows share
the same website. Names, emails and every other original column remain attached
to that exact row. Jobs are isolated by job ID, so one uploaded file cannot use
or overwrite another file's rows.

The default limit is 25,000 rows and 100 MB per CSV. The interface paginates
large files while processing continues; the completed export always includes
every row, not only the currently visible page.

The completed download preserves the original header order and appends:

- `Primary Advertising Confidence`
- `Ad Infrastructure Result`
- `Infrastructure Confidence`
- `Infrastructure Freshness`
- `Current Ad Activity`
- `Combined Activity Confidence`
- `Detected Ad Platforms`
- `Runtime Ad Platforms`
- `Pages Crawled`
- `Research Scan Count`
- `Last Runtime Signal At`
- `Evidence 1`
- `Evidence 2`
- `Evidence 3`
- `Error / Notes`

Evidence is ranked and deduplicated: multiple platforms are preferred, followed
by distinct runtime events on different pages, then supporting scripts or IDs.
Raw repetitive matches remain available in the per-result diagnostics. If the
source CSV already contains an output column name, the generated column receives
an `Ad Research` suffix rather than overwriting the source value.

## Persistence and database setup

Local development defaults to SQLite at `./ad_research.db`. Tables are created
idempotently at startup:

- `ad_research_crawl_jobs`
- `ad_research_crawl_results`
- `ad_research_domain_scans`
- `ad_research_activity_evidence`

`ad_research_domain_scans` provides repeat-scan history without changing the
uploaded lead rows. `ad_research_activity_evidence` stores external claims with
source, source URL, observation date, optional active-ad count and optional ad
landing pages. Landing pages from current/recent records are safely revisited on
the next domain scan (up to `AD_RESEARCH_MAX_LANDING_PAGES`) and are labeled
separately from ordinary site navigation.

If the process exits mid-batch, a previously `RUNNING` job is marked
`INCONCLUSIVE` on startup instead of remaining stuck or being presented as a
completed negative. Browser recycling and per-domain completion logs include
process RSS to help diagnose Chromium memory pressure.

For Railway PostgreSQL, set `AD_RESEARCH_DATABASE_URL` to the Railway
`DATABASE_URL`. The `psycopg` driver is included. Table names are prefixed to
avoid collisions when reusing an existing database. Before changing these
tables after V1, introduce Alembic migrations; startup `create_all` is only the
initial schema bootstrap, not a substitute for versioned production migrations.

## Railway service

Create a second service in the same Railway project using this repository and:

1. Set its Dockerfile path to `Dockerfile.ad-research`.
2. Set `AD_RESEARCH_DATABASE_URL`, preferably using the existing PostgreSQL
   service reference while the prefixed schema remains small and isolated.
3. Set `AD_RESEARCH_MAX_BATCH_SIZE=25000` and
   `AD_RESEARCH_MAX_UPLOAD_BYTES=100000000`. A pre-existing Railway variable
   overrides the code default, so an old value of `5000` must be changed.
4. Give Chromium at least 2 GB memory; 4 GB is safer for concurrency 3.
5. Keep one replica initially. Scale domain concurrency with
   `AD_RESEARCH_CONCURRENCY`, not Uvicorn workers.
6. Open `/api/health` and confirm the returned `max_batch_size`,
   `max_upload_bytes`, and `crawl_concurrency`, then run the diagnostic test set
   before connecting any lead workflow.

The service binds to `0.0.0.0:$PORT` in the container. The original repository
Dockerfile and `app.main:app` entry point are unchanged.

## Calibration test set

Maintain a labeled CSV outside source control with at least:

1. a confirmed Meta Pixel site;
2. a confirmed Google Ads conversion site;
3. GTM-only and GA4-only controls;
4. a consent-gated advertising site;
5. a multi-pixel site;
6. an analytics-only site;
7. a no-ad-infrastructure site;
8. known bot-blocked and unavailable sites.

Real websites change frequently, so labels must record the date and manual
browser evidence. A site that becomes blocked is an inconclusive test result,
not a negative-control success.

## Designed extension point

Website observations remain one evidence source. Future permitted Meta Ad
Library, Google advertising, creative-volume and longevity collectors should
emit the same dated activity-evidence records. They must not rewrite website
infrastructure confidence or imply a $5K monthly spend threshold from pixels
alone.
