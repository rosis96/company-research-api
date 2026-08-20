# Company Website Research API

A first-party website research API for Custom GPTs. It takes one company URL,
crawls that company's own domain, and returns cleaned page text with the URL each
piece of text came from — so a GPT can write personalized content from evidence
rather than from assumption.

It is deliberately **not** a writer. The division of labour is fixed:

| Component | Does | Never does |
|---|---|---|
| This API | fetch → discover → prioritise → scrape → clean → return | interpret, summarise, judge, or write |
| The GPT | understand → select evidence → write | invent facts, or fall back on training knowledge |

Implements the approved architecture document *Company Website Research API — V1
Architecture, v1.1*. Section references below (SS4, SS7, …) point at it.

---

## What it does

1. Validates and normalises one company URL, through a hard SSRF boundary.
2. Fetches the homepage over plain HTTP; falls back to a browser render only when
   the response contains no usable text (SS4, SS5).
3. Freezes the crawl scope from the homepage's **final** URL — so a rebrand
   redirect is followed once, and nothing after that leaves the domain.
4. Discovers links from homepage anchors, `robots.txt` and `sitemap.xml`.
5. Scores candidates deterministically against a YAML concept lexicon, then
   selects with **category quotas** so the evidence is diverse rather than forty
   case studies (SS7).
6. Runs a **second wave** into hub pages, because case-study detail pages live one
   level below the index and a single-pass crawler never reaches them (SS6).
7. Extracts main content with trafilatura, removes cross-page boilerplate, and
   returns everything under a strict character budget (SS8).
8. Returns HTTP 200 with an explicit `status` for every crawl outcome, including
   failure (SS11).

### The six invariants

Enforced by `tests/test_invariants.py`:

1. No model, no LLM, no external inference in the request path.
2. No text is returned that is not attributable to a URL.
3. The deadline always wins.
4. Nothing outside crawl scope is ever fetched.
5. The payload never exceeds the budget silently.
6. The crawler never rewrites, summarises, or judges company content.

---

## Repository layout

```
app/
  main.py                  FastAPI app factory + lifespan
  api/                     routes, bearer auth, rate limiting
  cache/                   Redis result cache (CONFIG_VERSION-keyed, TTL by outcome)
  config/                  settings + lexicon.yaml, exclusions.yaml, limits.yaml
  crawl/                   normalize, discover, robots, rank, orchestrator
  extract/                 content, metadata, jsonld, boilerplate
  fetch/                   http, sufficiency, renderer adapter, host limiter
  models/                  request, response, internal
  observability/           structlog config
  security/                url_guard, ip_rules, resolver, transport, domains
tests/                     ~500 tests + 8 fixture sites served over real sockets
scripts/export_openapi.py  generates the GPT-facing schema
```

Everything tunable — ranking weights, exclusions, subdomain tiers, budgets,
thresholds — lives in the three YAML files under `app/config/`. No weight is
hard-coded, and a test asserts it.

---

## Local setup

Requires Python 3.12.

```bash
git clone <your-repo> company-research-api
cd company-research-api

python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
pip install -r requirements-dev.txt

playwright install --with-deps chromium     # browser fallback

cp .env.example .env
# edit .env: set CRA_API_KEYS to a real key
```

Generate a key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run it:

```bash
export CRA_CACHE__ENABLED=false          # no local Redis needed
uvicorn app.main:app --reload --port 8000
```

With Redis:

```bash
docker run -d --name cra-redis -p 6379:6379 redis:7-alpine
export CRA_CACHE__REDIS_URL=redis://localhost:6379/0
uvicorn app.main:app --reload --port 8000
```

---

## Tests

```bash
pytest                        # everything
pytest -q --cov=app           # with coverage
pytest -m "not renderer"      # skip tests that drive a real browser
pytest tests/test_url_guard.py -v
```

The suite starts eight fixture websites on loopback and drives the crawler
against them over real sockets, so redirect walking, byte caps, content-type
gating, timeouts and the pinned-DNS transport are all genuinely exercised.

Fixture hostnames resolve through a `StaticResolver`, and loopback is reachable
only because the test passes `extra_allowed_networks` to the guard **policy** —
a constructor argument the production wiring never sets. The guard itself is not
weakened for tests.

If your Chromium build number does not match the installed Playwright package:

```bash
export CRA_RENDERER__EXECUTABLE_PATH=/path/to/chrome
pytest -m renderer
```

---

## Calibration — do this before you trust the output

The test suite proves the **logic** is right against fixture sites. It cannot prove the
**thresholds** are right, because those depend on what real websites in your market look
like. Until you have run real prospects through it, every number in
`app/config/limits.yaml` is an educated guess.

```bash
export CRA_BASE_URL=https://your-domain.com
export CRA_ADVANCED_KEY=<your advanced-scope key>

cp scripts/domains.example.txt domains.txt   # then put 25-40 real prospects in it
python scripts/calibrate.py domains.txt --out reports/run-01
```

Produces `reports/run-01.json` (every raw response) and `reports/run-01.md` (the tuning
report). Re-analyse without re-crawling:

```bash
python scripts/calibrate.py domains.txt --from-json reports/run-01.json --out reports/run-01b
```

The report tells you:

| Section | Answers |
|---|---|
| Outcomes | What share of prospects the GPT can actually write about |
| Why sites failed | Which reason codes dominate, with examples |
| Threshold diagnostics | Sites that failed the word test **and by how much** — the number needed to retune `min_words_homepage` |
| Deadline pressure | Whether crawls are approaching the 45 s GPT Actions timeout |
| What the crawler found | Page-type coverage, especially whether case studies are being reached |
| Lexicon gaps | Pages discovered repeatedly that the concept lexicon could not classify |
| Suggested next actions | Concrete config changes, each tied to a measured number |

**Workflow:** run it → change one thing in `app/config/*.yaml` → **bump
`CRA_CONFIG_VERSION`** → re-run → compare. Without the version bump the cache serves the
previous results and it looks like nothing happened.

Expect two or three rounds. Tuning is normal, not a sign something is broken.

---

## Production deployment (VPS + Docker + Caddy)

**Machine: 2 vCPU / 4 GB RAM minimum.** The 4 GB floor is set by Chromium, not by
FastAPI. Configure swap so a render spike degrades instead of OOM-killing the API.

### 1. Point DNS at the box

Create an `A` record for your domain. GPT Actions require a valid public
certificate on port 443, so a real domain is a prerequisite, not a later step.

### 2. Install Docker

```bash
ssh root@your-vps
curl -fsSL https://get.docker.com | sh
```

### 3. Deploy

```bash
git clone <your-repo> /opt/company-research-api
cd /opt/company-research-api

cp .env.example .env
nano .env                 # set CRA_API_KEYS, DOMAIN, ACME_EMAIL, CRA_USER_AGENT

docker compose build
docker compose up -d
docker compose logs -f api
```

### 4. Egress firewall — SS12, layer 2

Application-level validation is necessary but should not be the only thing
between a clever URL and your metadata endpoint. Chromium in particular does its
own DNS and cannot use the pinned transport, so this layer is what closes that
gap.

```bash
# Block the container network from reaching private ranges.
iptables -I DOCKER-USER -d 10.0.0.0/8      -j REJECT
iptables -I DOCKER-USER -d 172.16.0.0/12   -j REJECT
iptables -I DOCKER-USER -d 192.168.0.0/16  -j REJECT
iptables -I DOCKER-USER -d 169.254.0.0/16  -j REJECT   # cloud metadata
iptables -I DOCKER-USER -d 127.0.0.0/8     -j REJECT
iptables -I DOCKER-USER -d 100.64.0.0/10   -j REJECT

apt-get install -y iptables-persistent && netfilter-persistent save
```

Allow the internal Docker subnet first if the API must still reach Redis:

```bash
docker network inspect company-research-api_internal \
  --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
# then, before the REJECT rules:
iptables -I DOCKER-USER -d <that-subnet> -j RETURN
```

On AWS also enforce IMDSv2 with a hop limit of 1, so containers cannot reach the
metadata service at all.

### 5. Verify

```bash
curl -fsS https://your-domain.com/v1/health | jq
```

### 6. Updating

```bash
cd /opt/company-research-api
git pull
docker compose build && docker compose up -d
```

**Whenever you change `app/config/*.yaml`, bump `CRA_CONFIG_VERSION` in `.env`.**
The YAML files are fingerprinted into the cache key automatically, but the
explicit version is what you control for extraction or ranking changes in code.

---

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `CRA_API_KEYS` | **yes** | — | `key:scope` pairs, comma-separated. Scope is `standard` or `advanced`. |
| `DOMAIN` | **yes** | — | Public hostname for Caddy/TLS. |
| `ACME_EMAIL` | **yes** | — | Let's Encrypt contact. |
| `CRA_USER_AGENT` | recommended | generic | Truthful UA with a contact URL. |
| `CRA_CONFIG_VERSION` | recommended | `1` | Part of the cache key; bump on ranking/extraction changes. |
| `CRA_ENVIRONMENT` | no | `production` | `production` hides `/docs`. |
| `CRA_LOG_LEVEL` / `CRA_LOG_JSON` | no | `INFO` / `true` | Logging. |
| `CRA_RATE_LIMIT_PER_MINUTE` | no | `60` | Per-key sliding window. |
| `CRA_RATE_LIMIT_MONTHLY_QUOTA` | no | `0` (off) | Per-key monthly cap. |
| `CRA_MAX_CONCURRENT_CRAWLS` | no | `4` | **Per worker.** With 2 workers the real cap is 8. |
| `CRA_ROBOTS__ENABLED` | no | `true` | Master switch for robots. |
| `CRA_ROBOTS__APPLY_TO_SEED_URL` | no | `true` | Whether the supplied homepage is subject to robots. |
| `CRA_ROBOTS__ON_BLANKET_DISALLOW` | no | `fail` | `fail` or `proceed`. |
| `CRA_ROBOTS__ON_FETCH_ERROR` | no | `proceed` | Unreachable/malformed robots.txt. |
| `CRA_ROBOTS__MAX_CRAWL_DELAY_MS` | no | `2000` | Cap on declared `Crawl-delay`. |
| `CRA_CACHE__ENABLED` | no | `true` | Result cache. |
| `CRA_CACHE__REDIS_URL` | no | `redis://redis:6379/0` | Redis connection. |
| `CRA_CACHE__TTL_*` | no | see `.env.example` | Differentiated TTLs by outcome. |
| `CRA_SECURITY__MAX_REDIRECT_HOPS` | no | `5` | Per URL. |
| `CRA_SECURITY__MAX_RESPONSE_BYTES` | no | `2097152` | Decompressed. |
| `CRA_RENDERER__ENABLED` | no | `true` | Browser fallback. |
| `CRA_RENDERER__EXECUTABLE_PATH` | no | unset | Pre-installed Chromium. |
| `CRA_RENDERER__HOST_RESOLVER_RULES` | no | unset | Chromium `--host-resolver-rules`. Leave unset in production. |

---

## Health check

```bash
curl -fsS https://your-domain.com/v1/health | jq
```

```json
{
  "status": "ok",
  "version": "1.0.0",
  "schema_version": "1.1",
  "config_version": "1",
  "renderer_available": true,
  "cache_available": true
}
```

`renderer_available: false` means Playwright is missing or disabled — JavaScript-
heavy sites will fail honestly with `too_thin` rather than returning empty
content. `cache_available: false` means Redis is unreachable; the API keeps
working, it just re-crawls.

Unauthenticated, and deliberately absent from the GPT-facing schema.

---

## Example request

```bash
curl -sS https://your-domain.com/v1/research \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}' | jq
```

With options:

```bash
curl -sS https://your-domain.com/v1/research \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "url": "https://example.com",
        "research_depth": "deep",
        "focus": ["case_studies", "services"],
        "force_refresh": false
      }' | jq
```

Response shape (schema v1.1):

```json
{
  "schema_version": "1.1",
  "request_id": "req_01j8k2m9x",
  "status": "success",
  "evidence_sufficient": true,
  "site": {
    "requested_url": "https://example.com",
    "final_url": "https://www.example.com/",
    "canonical_domain": "example.com",
    "domain_rebased": false,
    "language": "en",
    "site_name": "Example Manufacturing Group",
    "tagline": "Precision components for aerospace and medical devices",
    "organization": { "founding_date": "1998", "locations": ["Austin, TX, US"] },
    "site_wide_content": "Text that appeared across most pages, stated once."
  },
  "pages": [
    {
      "url": "https://www.example.com/",
      "page_type": "homepage",
      "title": "Example Manufacturing Group | Precision Components",
      "headings": ["What We Make", "Industries We Serve"],
      "word_count": 612,
      "truncated": false,
      "content": "## What We Make\nExample Manufacturing Group produces..."
    }
  ],
  "failed_pages": [
    { "url": "https://www.example.com/pricing", "reason_code": "http_error_4xx", "http_status": 404 }
  ],
  "coverage": { "found": ["homepage", "services"], "missing": ["pricing", "testimonials"] },
  "crawl_summary": {
    "pages_scraped": 10, "pages_failed": 1, "browser_renders": 2,
    "max_depth_reached": 2, "total_words": 5840, "content_chars": 39120,
    "elapsed_ms": 21430, "stopped_reason": "budget_reached", "cache_hit": false
  },
  "notes": []
}
```

`pages` is sorted by relevance, homepage first — so if ChatGPT truncates the
payload, the best evidence is what survives.

### Status codes

| Code | Meaning |
|---|---|
| `200` | **Every crawl outcome**, including `status: "failed"` |
| `401` | Missing or invalid API key |
| `403` | `advanced` options sent with a `standard` key |
| `422` | Malformed request body |
| `429` | Rate limit or monthly quota (`Retry-After` set) |
| `500` | Unhandled server error |

Crawl failures deliberately return 200. ChatGPT surfaces non-2xx as a tool error,
and the model's most likely recovery is to fall back on its own knowledge of the
company — the exact failure this system exists to prevent.

Reason codes: `invalid_url`, `blocked_scheme`, `private_address`, `dns_failure`,
`connection_failed`, `tls_error`, `timeout`, `deadline_exceeded`,
`http_error_4xx`, `http_error_5xx`, `bot_blocked`, `robots_disallowed`,
`parked_domain`, `non_html_content`, `size_limit_exceeded`, `render_failed`,
`too_thin`, `out_of_scope`, `too_many_redirects`.

---

## Connecting the GPT Action

### 1. Generate the schema

```bash
python scripts/export_openapi.py https://your-domain.com > openapi.gpt-action.json
```

This is not the same as `/openapi.json`: it strips the `advanced` options so the
model cannot pass crawl knobs it has no basis for choosing, drops `/v1/health`,
pins the server URL, and enforces the 300/700-character description limits GPT
Actions impose.

### 2. Create the Action

In ChatGPT → **Create a GPT** → **Configure** → **Create new action**:

1. **Schema** — paste the contents of `openapi.gpt-action.json`.
2. **Authentication** — `API Key`, Auth Type **Bearer**, paste your key.
   Do not use a custom header: GPT Actions do not support them.
3. **Privacy policy** — required to publish publicly.
4. Test with `{"url": "https://example.com"}`.

### 3. GPT instructions — the other half of the system

The API returning honest failures accomplishes nothing on its own. Include
something close to this:

> You research companies using the `research` action, one company at a time.
>
> **Before writing anything**, call the action with the company's website URL.
>
> **Evidence rules — these are absolute:**
> - Every factual claim you write must be traceable to text inside a
>   `pages[].content` field, and you must be able to name the `pages[].url` it
>   came from.
> - If `status` is `"failed"`, or `evidence_sufficient` is `false`, output
>   `N/A — website could not be researched` plus the `status_reason`, and stop.
>   Do not proceed to the writing step.
> - Never use your own knowledge of the company. Never infer from the company
>   name or domain. Never use browsing to fill gaps.
> - Check `coverage.missing` before making claims about a topic. If `pricing` is
>   missing, you do not know their pricing.
> - `site.site_wide_content` is site-level context, not attributable to one page.
> - If `notes` contains `thin_site_blog_fallback`, one page is a blog post rather
>   than core positioning material — weight it accordingly.
>
> **Isolation:** finish and output one company completely before starting another.
> Prefer a fresh conversation per company. Check `site.canonical_domain` on every
> response to confirm you are writing about the company you were asked about.

That last point matters: the API is stateless, so there is no leakage
server-side — but contamination happens in the GPT's context window across turns,
where the API has no visibility and no control.

---

## Known V1 limitations

**Rendering**
- Chromium does its own DNS and cannot use the pinned transport. Browser requests
  are re-validated by route interception and everything except the main document
  is blocked, but in-browser DNS rebinding is closed by the egress firewall rules
  above, not by application code.
- No stealth rendering, proxies or CAPTCHA solving. Sites behind aggressive bot
  protection return `bot_blocked`. Add proxies when the measured block rate
  exceeds ~8% over a rolling week, not before.

**Coverage**
- Depth 2 only. Content three levels deep is not reached.
- PDFs, Office documents and images are never parsed — some case studies are PDFs.
- One locale per site; at most one locale-mismatched page is ever returned.
- Blog and news content is excluded except as a single-page thin-site fallback.

**Scale and operations**
- Synchronous only. The 45-second GPT Actions timeout is the binding constraint;
  batch processing wants the async job API described in SS14, which is not built.
- One URL per request. There is no batch endpoint.
- `CRA_MAX_CONCURRENT_CRAWLS` is per uvicorn worker — a semaphore cannot span
  processes. With 2 workers the real cap is double the configured value.
- Rate limiting falls back to per-process counters if Redis is unavailable, so
  the effective limit briefly becomes `workers × limit`.
- The renderer runs in-process. Extract it into its own service when rendering
  exceeds ~30% of pages (the adapter interface exists for exactly this).

**Integration**
- DNS pinning depends on a verified property of httpcore's connection logic
  (`connect_tcp` receives the origin host while `start_tls` receives SNI
  separately). Verified against **httpcore 1.0.9**. `assert_pinning_supported()`
  runs at startup and fails loudly if a future version changes that contract —
  pinning must never degrade silently.
- `tldextract` is pinned to its bundled Public Suffix List snapshot, so it never
  makes a network call at request time. Refreshing the list means upgrading the
  package.
- The `rate_limit` directive in the Caddyfile needs the `caddy-ratelimit` module;
  see the note at the bottom of that file.

**Deliberately not built** (SS15): no LLM anywhere in the API, no search engines,
no LinkedIn, no directories, no review sites, no enrichment vendors, no company
classification or ICP scoring, no embeddings, no admin UI.
