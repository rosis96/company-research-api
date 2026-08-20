# Company Website Research API — V1 Architecture

**Status: APPROVED — source of truth for V1 implementation** · **Version 1.1** · **Date:** 2026-08-20 · No implementation code included.

> **v1.1 changelog.** Incorporates the fourteen approved decisions. Changed sections: §6 (robots behind config), §7 (careers allowed at low weight; thin-site blog fallback), §8 (tiered content caps; JSON-LD narrowed), §9 (`debug` flag), §10 (payload slimmed — schema_version `1.1`), §13 (limits updated), §14 (cache specified), §17 (replaced with approved decisions and build order). §16 is retained unchanged as the historical review; every item in it is now addressed.

---

## 0. The constraint that shapes everything

Before any design choice, two hard numbers from OpenAI's GPT Actions documentation:

| Constraint | Value | Consequence |
|---|---|---|
| Round-trip timeout | **45 seconds** | The entire crawl — DNS, homepage, discovery, N page fetches, any browser renders, extraction, serialization — must finish well inside this. Not configurable. |
| Request/response payload | **100,000 characters each** | The response JSON, not just the text, must fit. Overflow is truncated, and truncation looks exactly like a complete answer to the model. |
| Endpoint description | 300 characters | The OpenAPI description the GPT reads is tiny. Behaviour must be obvious from the schema, not explained in prose. |
| Parameter description | 700 characters | Same. |
| Transport | TLS 1.2+, port 443 | Needs a real domain with a valid public certificate. No IP addresses, no custom ports. |
| Custom headers | Not supported | Use `Authorization: Bearer <key>`, not `X-API-Key`. |

Everything below is designed backwards from **45 seconds and 100,000 characters.** Those are the real limits; `max_pages` and `max_depth` are just knobs we use to stay under them.

**Design consequence:** V1 is a *synchronous, deadline-driven crawler that always returns something.* It does not aim to finish a fixed amount of work — it aims to spend a fixed amount of time acquiring the highest-value evidence it can, then stop and report honestly what it got. A crawl that returns 6 good pages in 22 seconds is a success. A crawl that would have returned 12 pages in 50 seconds is a total failure, because ChatGPT will have already given up.

---

## 1. Recommended V1 architecture

### Shape

A single stateless FastAPI service, one container, one meaningful endpoint. No queue, no database, no worker fleet in V1. Optional Redis (or on-disk) cache.

```
                    ┌──────────────────────────────────────────────┐
   Custom GPT       │  Caddy / Cloudflare  (TLS 443, rate limit)    │
      │             └───────────────────────┬──────────────────────┘
      │ POST /v1/research                   │
      │ Authorization: Bearer <key>         ▼
      └───────────────────────►  ┌─────────────────────────────┐
                                 │  FastAPI app (async)        │
                                 │                             │
                                 │  ├─ auth + rate limit       │
                                 │  ├─ URL validator / SSRF    │
                                 │  ├─ Orchestrator (deadline) │
                                 │  ├─ HTTP fetcher (httpx)    │
                                 │  ├─ Discovery + ranking     │
                                 │  ├─ Extractor / cleaner     │
                                 │  └─ Renderer adapter ───────┼──► Playwright
                                 └──────────────┬──────────────┘     (in-process V1,
                                                │                     separate service later)
                                          ┌─────▼─────┐
                                          │  Cache    │  optional, keyed by domain
                                          └───────────┘
```

### The one architectural decision that matters for later scale

**The renderer sits behind an adapter interface from day one.** In V1 it is Playwright running in the same container. The orchestrator calls `renderer.render(url) -> RenderResult` and knows nothing else. This is a ~20-line abstraction that costs nothing now and lets you later:

- move rendering to its own container and scale it independently (it is the only memory-hungry, CPU-hungry part),
- swap in a stealth renderer (Camoufox, patched Chromium) if block rates rise,
- swap in a paid rendering API for the hard 5% of sites without touching the crawler.

Everything else in V1 can be naive. This one seam should not be.

### Module boundaries

| Module | Responsibility | Must never |
|---|---|---|
| `security.url_guard` | Parse, normalize, validate, resolve, IP-check every URL including every redirect hop | Trust a hostname it has not resolved itself |
| `fetch.http` | Streamed HTTP GET with caps and manual redirect handling | Follow a redirect without re-validating |
| `fetch.render` | Browser render of a single URL | Be invoked when HTTP already produced usable content |
| `discover` | Anchors, sitemaps, robots; normalize and dedupe URLs | Emit an off-scope URL |
| `rank` | Deterministic scoring + category quotas | Use any model or external service |
| `extract` | Main content, metadata, boilerplate removal | Summarize, interpret, or rewrite |
| `orchestrate` | Deadline, budgets, wave scheduling, assembly | Retry indefinitely |

---

## 2. Request lifecycle

```
GPT ──POST /v1/research {"url": "https://example.com"} ──► API
```

**T+0.0s — Gate.** Validate bearer token. Check per-key rate limit and monthly quota. Validate request body against the Pydantic model. Start the global deadline clock (`t_deadline = now + 32s`). Generate `request_id`.

**T+0.1s — URL guard.** Normalize the input (scheme default `https`, IDNA-encode host, strip credentials/fragment, reject non-http(s) schemes and non-443/80 ports). Resolve DNS ourselves. Reject if *any* returned A/AAAA record is in a blocked range. Cache the resolved IPs for the duration of this request and pin connections to them.

**T+0.2s — Cache probe.** If a fresh result exists for this canonical domain + config hash and `force_refresh` is false, return it immediately. (Typical hit returns in <100 ms.)

**T+0.3–4.0s — Wave 0: homepage.** Streamed HTTP GET. Manual redirect loop, max 5 hops, each hop re-validated end to end. The **final URL of the homepage establishes the crawl scope**, and the scope is then frozen for the rest of the request. If the final registrable domain differs from the requested one (a rebrand, `oldco.com → newco.com`), we accept it, set `domain_rebased: true`, and record both — the redirect is itself first-party evidence. This rebase happens **once, on the homepage only**, never mid-crawl.

**T+2.0–5.0s — Content sufficiency test.** Not "did we get a 200" — did we get *text*. See §4. If the homepage fails the test, render it, and set the crawl into `js_site` mode (fewer pages, render-first, tighter budgets).

**T+3.0–6.0s — Discovery.** In parallel: parse homepage anchors (URL + anchor text + DOM region), fetch `/robots.txt`, fetch `/sitemap.xml` and any sitemaps robots declares. Sitemap parsing is capped (2 sitemap files, 2,000 URLs). Normalize, dedupe, drop off-scope and hard-excluded URLs.

**T+4.0s — Rank and select Wave 1.** Deterministic scoring (§7), then quota-based selection for category diversity. Typically 8–10 depth-1 URLs.

**T+4.0–14.0s — Wave 1 fetch.** Concurrent, capped at 4 simultaneous connections to the host, each with its own timeout. Failures are recorded, not retried across methods unless the sufficiency test says rendering would help.

**T+12.0–20.0s — Wave 2 (the part your original pipeline is missing).** Wave 1 pages are re-parsed for outlinks. Pages that are *hubs* — a `/case-studies` index, a `/services` listing, a `/work` grid — contribute their child links as depth-2 candidates with an inherited score bonus. Individual case studies live here and are the single most valuable evidence type for personalization. Wave 2 runs only if deadline and page budget allow; it is the first thing cut under pressure.

**T+20.0–24.0s — Extraction and cross-page cleaning.** Per-page main-content extraction, then site-level boilerplate removal using the fact that we now hold N pages from one site (§8). Repeated blocks are removed from every page and preserved *once* in `site.site_wide_content`.

**T+24.0–26.0s — Assembly and budgeting.** Sort pages by relevance descending (homepage always first). Apply per-page and global character budgets, truncating the *lowest*-ranked pages first so the best evidence is never the part that gets cut. Compute coverage, summary, notes, `evidence_sufficient`.

**T+26.0s — Return.** HTTP 200 with the result body. Write to cache.

**Deadline behaviour.** At `t_deadline` the orchestrator stops issuing new work, cancels in-flight fetches, and assembles from whatever completed. A deadline stop yields `status: "partial"` and `stopped_reason: "deadline"` — never an error, never an empty success.

---

## 3. Recommended Python libraries

| Purpose | Library | Why this one |
|---|---|---|
| Web framework | **FastAPI** + **uvicorn** | Async-native, Pydantic v2 validation, and it *generates the OpenAPI schema you need for the GPT Action from the same models* — one source of truth, no hand-maintained YAML drifting from reality. |
| HTTP client | **httpx** (async) | Async, HTTP/2, connection pooling, and critically: `follow_redirects=False` with streamed responses, so we can inspect and validate every hop and abort mid-download at a byte cap. Requests cannot do this cleanly. |
| HTML parsing | **selectolax** (lexbor) | 5–20× faster than BeautifulSoup for link extraction and DOM pruning; we parse every page at least twice, and this is where naive stacks quietly burn a second per page. |
| Main-content extraction | **trafilatura** | The strongest deterministic boilerplate-removal library available. Purpose-built for exactly this: strip nav/footer/cookie chrome, keep article body, preserve heading structure, output clean text or markdown. Also returns metadata (title, description, date, language). No ML inference at request time. |
| Domain logic | **tldextract** | Public Suffix List–backed registrable-domain extraction. Essential: `example.co.uk` vs `example.co` vs `notexample.com.evil.com` cannot be handled with string suffix matching. |
| IP safety | **ipaddress** (stdlib) | `is_private`, `is_loopback`, `is_link_local`, `is_reserved` plus explicit extra ranges. No dependency needed. |
| DNS | **dnspython** or `getaddrinfo` in a thread | We must resolve *ourselves* to validate before connecting (§12). |
| Browser fallback | **playwright** (async) | Direct, well-maintained, controllable. Resource blocking (images/fonts/media) is a one-liner and cuts render time roughly in half. |
| Schemas | **pydantic v2** | Response models double as the OpenAPI contract the GPT consumes. |
| Rate limiting | **slowapi** or reverse-proxy-level | Keep it out of app code if Caddy/Cloudflare can do it. |
| Cache | **redis** (or **diskcache** for a single box) | ~30 lines, large cost saving (§14). |
| Logging | **structlog** | Structured JSON logs; you will need per-domain outcome analytics to know when to add proxies. |

### On Scrapling — I would not build V1 on it

You proposed Scrapling as the scraping backbone. My recommendation is to **not** make it the foundation, for reasons specific to this project:

1. **Its headline feature is irrelevant here.** Scrapling's differentiator is adaptive element tracking — selectors that survive site redesigns. That matters when you scrape *the same site's same element* repeatedly. You are scraping 20,000 *different* sites once each, extracting main content, not targeted selectors. You would pay for a feature you never use.
2. **It abstracts away the layer where your hardest requirements live.** Your two genuinely hard requirements are SSRF-safe fetching (validate every redirect hop, pin to pre-resolved IPs, abort mid-stream at a byte cap) and a hard wall-clock deadline. Both need fine-grained control of the transport. A convenience wrapper over fetching is precisely the wrong abstraction to sit on top of that.
3. **Dependency weight and churn.** It pulls in a substantial stack. For a service whose reliability is the product, fewer moving parts wins.

**Where Scrapling *is* worth having:** as an optional third fetch tier behind the renderer adapter, for the minority of sites that block plain Playwright. Its stealth fetchers are legitimately good at that. Plug it in when your logs show a block rate that justifies it — not before.

---

## 4. HTTP-fetch strategy

Plain HTTP is the default and handles the large majority of B2B company sites.

**Per request:**

- `GET`, HTTP/2 enabled, `Accept-Encoding: gzip, deflate, br`.
- A **truthful, identifiable User-Agent** with a contact URL — e.g. `YourCompanyResearchBot/1.0 (+https://yourdomain.com/bot)`. Impersonating Chrome at 20k requests/month is how you end up on blocklists. An honest UA plus respecting robots.txt is both the defensible position and, in practice, the one that gets blocked *less* over time.
- Timeouts split explicitly: connect 5s, read 8s, total 12s per page. A single slow page must never eat the global deadline.
- `follow_redirects=False`; manual hop loop (§12).
- **Streamed response with a hard byte cap** (2 MB default). Abort the stream at the cap — do not trust `Content-Length`, and cap the *decompressed* size too (gzip bombs).
- **Content-Type check on headers, before the body.** Allow `text/html`, `application/xhtml+xml`. Anything else → abort immediately, record `non_html_content`. This alone avoids downloading PDFs, images, and video files that slipped past URL filtering.
- Politeness: max 4 concurrent connections per host, ~150 ms jitter between issues.

### The content sufficiency test

This is the decision function for the whole fetch tier, and it must be more than "status == 200". A JavaScript SPA returns a perfectly valid 200 with an empty `<div id="root">`.

A response is **sufficient** if all hold:

1. Status is 200 (2xx after redirects).
2. Content-Type is HTML.
3. After a fast DOM prune (drop `script`, `style`, `noscript`, `nav`, `header`, `footer`, `svg`), the remaining visible text is **≥ 200 words** (homepage) or **≥ 120 words** (internal page).
4. The body is not a recognized challenge/interstitial page (Cloudflare "Checking your browser", "Enable JavaScript", "Access denied", `<noscript>`-dominant bodies).
5. The text is not a known parked-domain pattern ("This domain is for sale", registrar placeholders).

Insufficient → the renderer, once. Never more than once.

---

## 5. Browser-fallback strategy

Rendering is roughly 5–15× the cost and latency of an HTTP fetch. It must be rationed, and rationed *per page*, not per site.

**Rules:**

1. **Never render speculatively.** HTTP first, always, including for sites we have previously rendered.
2. **Homepage result sets the mode.** If the homepage needed rendering, the site is almost certainly a JS app, so switch to `js_site` mode: reduce `max_pages` to 6, allow render-first on internal pages (skipping a doomed HTTP attempt), and raise the render cap. If the homepage was fine over HTTP, internal pages usually will be too — render only the individual stragglers.
3. **Hard render cap per request** (default 3 in normal mode, 6 in `js_site` mode), and a **render concurrency cap** of 2. This is the single most important cost control in the system.
4. **Render cheaply.** Block `image`, `font`, `media`, and stylesheet requests; they change nothing about extracted text and dominate load time. Wait for `domcontentloaded` plus a short network-idle window (max 3s), not `load`. Hard per-render timeout of 8s.
5. **Context isolation.** One shared browser process per worker; a **fresh browser context per page**, disposed immediately after. This gives clean cookie/storage isolation between companies at negligible cost — it is the mechanical guarantee behind your "no leakage between companies" requirement.
6. **Recycle the browser** every N renders (~200) to bound memory leaks.
7. **No third attempt.** HTTP → render → if still insufficient, the page is `failed` with a reason code. No retry loops, no exponential backoff, no proxy escalation in V1. Difficult sites are a *reported outcome*, not a problem to solve at request time.

**Budget interaction:** the orchestrator checks remaining deadline before every render. If less than 10 seconds remain, rendering is skipped entirely and the page is marked `skipped_deadline`. The deadline always wins.

---

## 6. Internal-link discovery strategy

Four sources, cheapest first:

1. **Homepage anchors.** Every `<a href>`, capturing the resolved absolute URL, the anchor text, and the **DOM region** it appeared in (`nav`, `header`, `main`, `footer`, `body`). Region is a strong ranking signal — a link in the primary nav is a page the company considers important. Mega-menus are usually present in the HTML, so this catches more than it appears.
2. **`/robots.txt`.** For both the crawl policy and the `Sitemap:` declarations.
3. **`/sitemap.xml`, `/sitemap_index.xml`, and robots-declared sitemaps.** Often a far better link source than nav scraping, especially for finding all case studies at once. Capped hard: 2 sitemap files, 2,000 URLs, 3s total. Sitemap `<lastmod>` is a useful freshness tiebreaker.
4. **Wave-1 hub outlinks (depth 2).** Detailed below — this is the addition your pipeline needs.

### URL normalization and dedup

Applied to every discovered URL before it enters the candidate pool:

- Resolve relative → absolute against the page's final URL.
- Lowercase scheme and host; strip default ports; IDNA-encode.
- Drop the fragment entirely (`#services` is the same page).
- Strip tracking parameters (`utm_*`, `gclid`, `fbclid`, `mc_cid`, `msclkid`, `ref`, `source`, `hsa_*`). Keep other query parameters — some sites genuinely route on them.
- Collapse duplicate slashes; normalize trailing slash; drop `index.html` / `index.php` / `default.aspx`.
- Percent-decode unreserved characters only.
- Dedupe by normalized URL, **and later by extracted-content hash** — which catches `/services` vs `/services/` vs `/en/services` vs `/services/index.html` collapsing to the same page, and prevents a locale-duplicated site from consuming the whole budget on ten translations of one page.

### Hard exclusions (never fetched)

- Schemes: `mailto:`, `tel:`, `javascript:`, `data:`, `sms:`, `ftp:`, `file:`
- Any URL outside crawl scope (§ subdomain policy below)
- Path/segment matches: `login`, `signin`, `sign-in`, `signup`, `sign-up`, `register`, `account`, `my-account`, `profile`, `dashboard`, `admin`, `wp-admin`, `wp-json`, `wp-content`, `cart`, `checkout`, `basket`, `order`, `payment`, `privacy`, `terms`, `tos`, `legal`, `disclaimer`, `cookie`, `gdpr`, `dsar`, `accessibility`, `sitemap.html`, `search`, `feed`, `rss`, `.atom`, `?s=`, `tag/`, `category/`, `author/`, `archive/`, `page/2+`, `#`
- File extensions: `.pdf .doc .docx .xls .xlsx .ppt .pptx .zip .rar .dmg .exe .jpg .jpeg .png .gif .svg .webp .ico .mp4 .mp3 .avi .css .js .json .xml .rss`
- Calendar/booking widgets, `calendly.com`-style embeds (external anyway)
- **Careers detail and ATS paths** (the overview page is allowed — see §7): `/careers/*` where the child is a listing, `/jobs/*`, `/job/*`, `/positions/*`, `/openings/*`, `/vacancies/*`, `/apply/*`, `/application/*`, and any URL on `careers.` / `jobs.` / `apply.` subdomains
- **ATS domains** (external, so already out of scope — listed to make the intent explicit): `greenhouse.io`, `lever.co`, `workable.com`, `bamboohr.com`, `ashbyhq.com`, `smartrecruiters.com`, `myworkdayjobs.com`, `icims.com`, `jobvite.com`, `recruitee.com`, `teamtailor.com`, `personio.de`, `breezy.hr`, `rippling.com`

### Subdomain policy — recommended

**Scope = same registrable domain (eTLD+1), computed with tldextract, established from the homepage's *final* URL and then frozen.**

Subdomains of that registrable domain are **in scope but tiered**:

| Tier | Examples | Treatment |
|---|---|---|
| Primary | apex, `www.` | Full priority |
| Content subdomains | `solutions.`, `products.`, `industries.`, `work.`, `cases.`, `partners.` | In scope, small rank penalty |
| Low-value | `blog.`, `news.`, `press.`, `events.`, `community.`, `forum.`, `docs.`, `shop.`, `store.` | In scope only if the primary tier yielded too little; heavy penalty |
| Excluded outright | `mail.`, `webmail.`, `smtp.`, `login.`, `auth.`, `sso.`, `id.`, `accounts.`, `app.`, `my.`, `portal.`, `dashboard.`, `admin.`, `status.`, `cdn.`, `static.`, `assets.`, `img.`, `media.`, `files.`, `download.`, `support.`, `help.`, `ticket.`, `careers.`, `jobs.`, `track.`, `link.`, `go.`, `email.` | Never fetched |
| Locale subdomains | `uk.`, `de.`, `fr.`, `es.` | Prefer the one matching the requested URL; all others heavily penalized to avoid budget-eating duplicate content |

**Why not restrict to the exact host?** Because plenty of mid-market companies put `/solutions` or their case studies on a subdomain, and a strict host match would silently miss the best evidence on the site. **Why not allow anything?** Because `app.example.com` and `status.example.com` are the two most common ways to waste a page budget on a login form.

### Depth-2 expansion (the missing step)

Your workflow as specified is a single pass: homepage → links → filter → rank → fetch → return. That pipeline structurally **cannot reach your own highest-priority content.** `/case-studies` is a *directory*; the actual success story — the client name, the problem, the numbers, the quote — lives at `/case-studies/acme-manufacturing`, one level deeper. A single-pass crawler returns you a grid of thumbnail titles and calls it a case study.

**Recommended fix — a two-wave frontier:**

After Wave 1 completes, a fetched page is classified as a **hub** if, deterministically:

- its `page_type` is an index type (`case_study_index`, `services_index`, `work_index`, `industries_index`, `products_index`), **and**
- it contains ≥ 3 same-scope outlinks sharing its own path prefix (`/case-studies/*`).

Each hub's children enter the Wave 2 candidate pool with an **inherited score bonus** from the parent (a child of `/case-studies` is presumed to be a case study without needing to match any keyword — which also solves the `/work/12345-project` slug problem where the URL carries no semantic tokens at all).

Wave 2 is budget- and deadline-gated and is always the first thing dropped under pressure. `max_depth: 2` in V1. Depth 3 has poor value-per-second and is explicitly out of scope.

### robots.txt — APPROVED: respected in V1, behind configuration

Robots handling lives entirely in a `RobotsPolicy` object. The crawler consults it through **one method** — `is_allowed(url) -> Allow | Deny` — and never parses, reads, or reasons about robots.txt itself. Changing policy later is a config change, not a crawler change.

**Configuration surface** (env / settings file, with per-key override for `advanced`-scope callers):

| Key | Default | Effect |
|---|---|---|
| `robots.enabled` | `true` | Master switch. `false` makes `is_allowed` return `Allow` unconditionally. |
| `robots.user_agent_token` | `YourCompanyResearchBot` | Group matched in robots.txt; falls back to `*`. |
| `robots.apply_to_seed_url` | `true` | Whether the user-supplied homepage is subject to robots, or treated as user-initiated. Flipping this is the most likely future change — hence its own flag. |
| `robots.respect_crawl_delay` | `true` | Honour `Crawl-delay`. |
| `robots.max_crawl_delay_ms` | `2000` | Cap. A site declaring `Crawl-delay: 30` would otherwise consume the entire deadline. |
| `robots.on_blanket_disallow` | `fail` | `fail` → `status: "failed"`, `reason_code: "robots_disallowed"`. Alternative `proceed`. |
| `robots.on_fetch_error` | `proceed` | Unreachable or malformed robots.txt. Standard interpretation. |
| `robots.cache_ttl_seconds` | `86400` | Per-domain, separate from the result cache. |

Fetch with a 3s timeout, cap the file at 512 KB, and cache the *parsed* policy per registrable domain. A blanket `Disallow: /` is an explicit, honest failure — not a silent empty result. Expect it on a low single-digit percentage of sites.

**Do not scatter robots checks through the crawler.** Exactly two call sites: the seed URL (gated by `apply_to_seed_url`) and the candidate filter that runs before any URL enters a fetch wave. Crawl-delay is applied by the per-host rate limiter, which is also the only place politeness delay exists.

---

## 7. Page-ranking strategy

Fully deterministic. Score = weighted concept match + structural signals − penalties, then a diversity pass.

### Signals

**A. Concept lexicon match** against the URL path *and* the anchor text. Tokenize on `/ - _ . ?` and whitespace, lowercase, crude singularization. Anchor text is often more informative than the URL and must be scored — `/s/12345` with anchor text "Client Success Stories" should rank high.

| Concept cluster | Terms (fuzzy/variant matching, not exact URL equality) | Weight |
|---|---|---|
| Case studies | case-study/studies, success-story/stories, client-story/stories, customer-story/stories, client-success | 100 |
| Work / portfolio | our-work, work, portfolio, projects, results, showcase, gallery, deployments | 90 |
| Services | services, what-we-do, capabilities, offerings, expertise, specialties, disciplines | 85 |
| Solutions | solutions, use-cases, applications | 80 |
| Products | products, platform, software, technology, features | 80 |
| Industries | industries, sectors, markets, verticals, who-we-serve, who-we-help, clients-we-serve | 75 |
| Customers | customers, clients, our-clients, partners, brands-we-work-with | 70 |
| Testimonials | testimonials, reviews, what-clients-say, feedback | 65 |
| About | about, about-us, our-story, company, mission, values, why-us, overview, history | 60 |
| Pricing | pricing, plans, packages, rates, cost, investment | 55 |
| Process | approach, process, how-it-works, methodology, framework, our-way | 50 |
| Team | team, leadership, our-people, founders | 35 |
| Contact | contact, get-in-touch, locations, offices | 25 |
| Careers **overview** | careers, career, culture, our-culture, life-at-*, working-at-*, join-us, join-our-team, work-with-us, why-work-here | 40 |
| Resources | resources, guides, whitepapers, ebooks, webinars, insights | 20 |
| Blog/news | blog, news, articles, press, updates | 8 (thin-site fallback only) |

**B. Structural bonuses**

| Signal | Adjustment |
|---|---|
| Homepage | always selected, exempt from scoring |
| Link in primary `<nav>` / `<header>` | +20 |
| Link in `<main>` body | +5 |
| Link in `<footer>` only | −10 |
| Path depth 1 (`/services`) | +10 |
| Path depth 2 (`/services/consulting`) | +4 |
| Path depth ≥ 4 | −10 |
| Present in sitemap.xml | +5 |
| Linked from ≥ 3 distinct pages | +8 |
| Hub-child inheritance (Wave 2) | +60% of parent's concept score |
| Recent `<lastmod>` (< 12 months) | +3 |

**C. Penalties**

| Signal | Adjustment |
|---|---|
| Date in path (`/2019/04/`) | −40 |
| Query string present | −8 |
| Path length > 80 chars | −10 |
| Non-primary subdomain tier | −15 to −40 by tier |
| Non-matching locale prefix (`/de/`, `/fr/`) | −45 |
| Path token in soft-noise list (`event`, `webinar`, `podcast`, `newsletter`, `award`, `sponsor`) | −20 |

### The diversity pass — do not skip this

A naive top-N-by-score selection on a site with 40 case studies returns **ten case studies and nothing else**. The GPT then knows the company's clients in vivid detail and has no idea what the company actually sells. Evidence quality is about coverage, not just individual page value.

Select via **category quotas**, filling targets in priority order, then spending leftover budget on the highest remaining scores:

| Category | Target | Max |
|---|---|---|
| Homepage | 1 | 1 |
| Services / solutions / products | 2 | 3 |
| Case study **detail** pages | 2 | 3 |
| About / company | 1 | 2 |
| Industries / who-we-serve | 1 | 1 |
| Customers / testimonials | 1 | 1 |
| Case study / work **index** | 1 | 1 |
| Pricing | 1 | 1 |
| Process / approach | 0 | 1 |
| Careers / culture **overview** | 0 | 1 |
| Blog / news post | 0 | 1 (thin-site fallback only) |
| Anything else | 0 | 2 |

A minimum score threshold (say 25) prevents padding the budget with junk when a site is genuinely thin. **Fewer, better pages is the correct outcome for a small site** — six strong pages beats six strong pages plus four filler pages that dilute the GPT's attention and consume the character budget.

### Careers / culture — APPROVED: allowed at low weight

A `/careers` or `/culture` *overview* page is frequently the richest personalization source on a B2B site: headcount and growth, tech stack, values in the company's own voice, office locations, current areas of investment. Individual job listings are noise.

**Rule:**

- **Allowed**, `page_type: "careers_overview"`, weight **40**, **max 1 page, target 0** — it is selected only when higher-value categories have not consumed the budget.
- **Depth-1 only.** A careers page is never treated as a hub and never expands into Wave 2. This is what keeps one allowed page from becoming twenty job listings.
- **Excluded:** all listing/detail paths, ATS subdomains and ATS domains (§6 exclusions).
- Path allowlist: `/careers`, `/career`, `/culture`, `/our-culture`, `/life-at-*`, `/working-at-*`, `/join-us`, `/join-our-team`, `/work-with-us`, `/why-work-here`. Exactly one path segment — `/careers/engineering-manager` does not match.

### Blog / news — APPROVED: thin-site fallback only

Blog and news pages stay out of the normal selection path. One narrowly-scoped exception exists purely to convert marginal failures into usable results.

**Trigger — all must hold, evaluated *after* Wave 1 and Wave 2 have finished:**

1. `pages_scraped < 3` **or** `total_words < 400` (the site is genuinely thin, not merely unusual)
2. Remaining deadline ≥ **6 seconds**
3. At least one first-party blog/news **detail** page candidate exists on the in-scope domain

**Then:** fetch exactly **one** page — the most recent by sitemap `<lastmod>`, falling back to the highest-placed such link on the homepage. **HTTP only, no browser render** (the render budget must not be spent on a fallback). Label `page_type: "blog_post"` and add the note `thin_site_blog_fallback` so the GPT can see this page is a fallback rather than core positioning material.

Blog *index* pages remain excluded in all cases. If the post is older than 18 months it is still used, with the note `blog_fallback_stale`. If nothing qualifies, no fallback runs and the result stands as `partial`.

---

## 8. Content extraction and cleaning

### Per-page extraction

1. **Primary: trafilatura**, configured for precision-leaning extraction, comments off, tables on, markdown output so heading structure survives. Headings carry meaning — "Industries We Serve" is a signal, not decoration, and a plain-text dump destroys the distinction between a heading and a sentence.
2. **Fallback:** if trafilatura returns under ~80 words, run a DOM heuristic on selectolax — remove `script`, `style`, `noscript`, `nav`, `header`, `footer`, `aside`, `form`, `iframe`, `svg`, and elements matching common chrome selectors (`[class*=cookie]`, `[class*=banner]`, `[id*=menu]`, `[role=navigation]`, `[aria-hidden=true]`), then prefer `<main>` / `[role=main]` / `<article>`, else take the highest text-density block. Trafilatura is tuned for articles and occasionally under-extracts short marketing pages; this fallback covers that.
3. **Metadata, always:** `<title>`, meta description, `og:description`, first `<h1>`, all `<h2>` (a useful skeleton of the page's claims), canonical URL, `lang`.
4. **JSON-LD — APPROVED, narrowed to a field allowlist.** Parse `<script type="application/ld+json">` on the **homepage and about page only**. Accept `@type` of `Organization`, `Corporation`, or `LocalBusiness`. Extract **only** these fields, and nothing else:

   | JSON-LD field | Output | Cap |
   |---|---|---|
   | `name` | `site.site_name` (if not already set) | 120 chars |
   | `legalName` | `organization.legal_name` | 120 chars |
   | `slogan` | `site.tagline` (if not already set) | 200 chars |
   | `description` | `organization.description` | 300 chars |
   | `foundingDate` | `organization.founding_date` | 20 chars |
   | `numberOfEmployees` | `organization.employees` | 20 chars |
   | `address` → locality / region / country only | `organization.locations[]` | 3 entries |
   | `areaServed` | `organization.area_served[]` | 3 entries |
   | `knowsAbout` | `organization.knows_about[]` | 5 entries |

   Whole `organization` block hard-capped at **500 characters**; drop it entirely if empty. Malformed JSON-LD is skipped silently, never fatal.

   **Explicitly discarded:** `sameAs`, `logo`, `image`, `contactPoint`, `employee`, `founder`, `makesOffer`, and — importantly — **`aggregateRating` and `review`, which are almost always third-party review data and therefore violate the first-party-only rule.** Excluding them is a correctness requirement, not a size optimisation.

   (`FAQPage` and `Service` markup on service pages is a reasonable V2 addition. Out of scope now.)
5. **Normalize:** unescape entities, strip zero-width and control characters, collapse 3+ newlines to 2, trim per-line whitespace, drop lines under 3 characters that are pure punctuation.

### Cross-page boilerplate removal — the highest-leverage cleaning step

Per-page cleaning always leaves residue: a repeated CTA, a floating "Book a call" strip, a cookie notice trafilatura kept, a footer address block. But we hold **N pages from one site**, and that makes the problem trivially solvable without any model:

1. Split each cleaned page into blocks (paragraphs / list items / headings).
2. Normalize each block for comparison (lowercase, collapse whitespace) and hash it.
3. Count document frequency across the scraped set.
4. When `n_pages ≥ 4`, remove any block appearing on **≥ 60%** of pages.
5. **Preserve the removed set once**, deduped, in `site.site_wide_content`.

Step 5 is what makes this better than plain deletion. The repeated tagline, the recurring value proposition, the footer positioning statement — those are genuine first-party evidence. They should be stated **once as site-level context**, not repeated eleven times inside per-page content where they crowd out the distinctive material and eat the character budget.

Expected effect: 20–35% reduction in returned characters with an increase in signal density.

### Budgeting

- **Tiered per-page caps.** Slimming the payload (§10) freed roughly 4,000 characters of metadata, and that space is given to the best evidence rather than spread evenly:

  | Rank position | Cap |
  |---|---|
  | 1–3 (homepage + top two pages) | **8,000 chars** |
  | 4+ | **5,000 chars** |

  Truncate at a paragraph boundary and set `truncated: true`.
- Global content budget **60,000 characters**. Combined with a metadata budget of ~5,000, total payload lands near 65,000 — comfortably inside the 100,000 limit with margin for unusually long titles, headings, and URLs.
- **Metadata budget: non-content fields must stay under 10% of the payload.** Assert this in tests, not just in prose. If it drifts above 10%, fields move to the `debug` block, not into the content budget.
- When over budget, **truncate from the lowest-ranked page upward**. The homepage and the top case study are never the pages that get cut.
- Drop pages with under 40 words post-cleaning to `failed_pages` with `too_thin` — they consume budget and tempt the GPT to over-read them.

### What extraction must NOT do

No summarizing. No rewriting. No entity extraction beyond deterministic regex (emails, phones, addresses). No "key facts" synthesis. No sentiment, no classification of the company, no ICP scoring. `page_type` is rule-based labeling of the *page*, not judgment about the *company* — that line should be enforced in review.

---

## 9. Proposed API request schema

The model-facing surface stays deliberately minimal. **Do not expose `max_pages`, `max_depth`, `timeout`, or `max_content_length` to the GPT.** Language models supply plausible-looking numbers with no basis, and a GPT that decides `max_pages: 50` produces a timeout that reads to the user as "the tool is broken." Presets, not numbers.

```
POST /v1/research
Authorization: Bearer <api_key>
Content-Type: application/json
```

```json
{
  "url": "https://example.com",
  "research_depth": "standard",
  "focus": ["case_studies", "services"],
  "force_refresh": false
}
```

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `url` | string | yes | — | Company website. Scheme optional; `https://` assumed. |
| `research_depth` | enum `quick` \| `standard` \| `deep` | no | `standard` | Maps server-side to page/render/deadline budgets. |
| `focus` | array of enum | no | `null` | Optional bias toward page types: `case_studies`, `services`, `products`, `industries`, `customers`, `about`, `pricing`. Adjusts quotas; never restricts to only those. |
| `force_refresh` | boolean | no | `false` | Bypass cache. |

Numeric overrides (`max_pages`, `max_render`, `deadline_ms`), the robots overrides from §6, and **`debug: true`** exist server-side and are accepted **only from keys with an `advanced` scope**. All are omitted from the OpenAPI document the GPT is given. Your own batch tooling and local testing can use them; the GPT cannot.

`debug: true` adds a `debug` block carrying everything removed from the default payload in §10 — per-page `relevance_score`, `discovered_via`, `depth`, `fetch_method`, `http_status`, `final_url`, plus `redirect_chain`, `urls_discovered`, `urls_after_filtering`, `pages_selected`, and robots decisions. Crawl behaviour stays fully auditable; the GPT just never pays for it.

Given the 300-character endpoint description limit, the description should read roughly:

> Researches one company website. Crawls the company's own domain only (homepage plus its most relevant internal pages such as services, about, case studies) and returns cleaned page text with source URLs. Returns status "failed" if the site cannot be read — never invents content.

Also expose `GET /v1/health` (unauthenticated, not in the GPT's schema).

---

## 10. Proposed API response schema

**APPROVED, slimmed (v1.1).** The governing rule: **every field must be something the GPT can act on.** Operational metadata that only a developer needs moved to the `debug` block (§9). Retained principles:

- **`pages` sorted by relevance descending, homepage first.** If ChatGPT truncates the payload, the best evidence must be the part that survives.
- **`coverage`** — tells the GPT what it *does not* have. This is the field that prevents a confident paragraph about pricing on a site with no pricing page.
- **`evidence_sufficient`** — one deterministic boolean gate the GPT's instructions key off, instead of asking it to judge sufficiency.
- **`site_wide_content`** — recurring positioning stated once (§8).
- **`schema_version`** — the GPT's instructions reference field names; version them.

### What was removed in the slimming pass, and why

| Removed from default payload | Reason |
|---|---|
| `relevance_score`, `discovered_via`, `depth`, `fetch_method` per page | Operational. The GPT cannot act on them. → `debug` |
| `http_status` on **successful** pages | Always 200. Retained on `failed_pages`, where it carries information. |
| `meta_description` per page | Largely duplicates the opening of `content`. Pure duplication against the budget. |
| `final_url` per page | Emitted **only when it differs from `url`**, otherwise omitted. |
| `redirect_chain` | `domain_rebased` + `final_url` carry the meaning the GPT needs. → `debug` |
| `emails`, `phones`, `addresses`, `social_profiles` | Contact details are not evidence for personalized copy, and social links are third-party pointers we deliberately do not follow. |
| `jsonld_organization` raw object | Replaced by the narrow allowlisted `organization` block (§8), capped at 500 chars. |
| `urls_discovered`, `urls_after_filtering`, `pages_selected` | Development telemetry. → `debug` |
| `robots_policy` | Only interesting when it caused a failure, where `reason_code` already says so. |

Net effect: roughly 4,000 characters returned to the content budget, spent on the tiered per-page caps in §8.

```json
{
  "schema_version": "1.1",
  "request_id": "req_01J8K2M9X",
  "status": "success",
  "status_reason": null,
  "evidence_sufficient": true,

  "site": {
    "requested_url": "http://example.com",
    "final_url": "https://www.example.com/",
    "canonical_domain": "example.com",
    "domain_rebased": false,
    "language": "en",
    "site_name": "Example Manufacturing Group",
    "tagline": "Precision components for aerospace and medical devices",
    "organization": {
      "legal_name": "Example Manufacturing Group, Inc.",
      "founding_date": "1998",
      "employees": "120",
      "locations": ["Austin, TX, US"],
      "area_served": ["North America"]
    },
    "site_wide_content": "Recurring text that appeared across most pages, stated once."
  },

  "pages": [
    {
      "url": "https://www.example.com/",
      "page_type": "homepage",
      "title": "Example Manufacturing Group | Precision Components",
      "headings": ["What We Make", "Industries We Serve", "Why Manufacturers Choose Us"],
      "word_count": 612,
      "truncated": false,
      "content": "## What We Make\nExample Manufacturing Group produces..."
    },
    {
      "url": "https://www.example.com/case-studies/acme-aerospace",
      "page_type": "case_study",
      "title": "Acme Aerospace cuts lead time 40% | Example",
      "headings": ["The Challenge", "Our Approach", "Results"],
      "word_count": 743,
      "truncated": false,
      "content": "## The Challenge\nAcme Aerospace was facing..."
    }
  ],

  "failed_pages": [
    {
      "url": "https://www.example.com/pricing",
      "reason_code": "http_error_4xx",
      "http_status": 404
    }
  ],

  "coverage": {
    "found": ["homepage", "about", "services", "case_study", "industries"],
    "missing": ["pricing", "testimonials", "customers"]
  },

  "crawl_summary": {
    "pages_scraped": 10,
    "pages_failed": 1,
    "browser_renders": 2,
    "max_depth_reached": 2,
    "total_words": 5840,
    "content_chars": 39120,
    "elapsed_ms": 21430,
    "stopped_reason": "budget_reached",
    "cache_hit": false
  },

  "notes": ["two_pages_below_word_threshold_dropped"]
}
```

**Field rules.** `headings` is `h1` plus `h2`s, deduped, capped at **8 entries and 60 chars each** — it is the company's own skeleton of claims and earns its space, but it is capped so a heading-heavy landing page cannot consume the budget. `organization` is omitted entirely when empty; so is `site_wide_content` when fewer than 4 pages were scraped. `final_url` appears on a page object **only** when it differs from `url`. `notes` is a small enum-like list of deterministic flags (`thin_site_blog_fallback`, `blog_fallback_stale`, `js_site_mode`, `two_pages_below_word_threshold_dropped`), never free prose.

`page_type` enum: `homepage`, `about`, `services`, `service_detail`, `products`, `product_detail`, `solutions`, `industries`, `case_study_index`, `case_study`, `customers`, `testimonials`, `portfolio`, `pricing`, `process`, `team`, `contact`, `careers_overview`, `resource`, `other`.

`stopped_reason` enum: `budget_reached`, `deadline`, `candidates_exhausted`, `char_budget_reached`.

### Evidence traceability

Guaranteed structurally: content only ever exists inside a `pages[]` object that carries its own `url`. There is no site-level text field other than `site_wide_content`, which is explicitly labelled as cross-page. The GPT can cite every claim, and your instructions can require it to.

---

## 11. Failure and error model

### HTTP status code policy — a GPT-Actions-specific decision

**Return HTTP 200 for every outcome the crawler was able to evaluate, including total failure.** Non-2xx responses are surfaced to ChatGPT as tool errors, where behaviour degrades badly: the model may retry, may present a raw error to the user, or — the failure mode you care most about — may quietly fall back to its own training knowledge about the company, which is exactly the thing this whole system exists to prevent.

A clean `200 {"status": "failed", "status_reason": "bot_blocked"}` is legible to the model and easy to write an instruction against.

Reserve real error codes for infrastructure concerns only:

| Code | When |
|---|---|
| `200` | All crawl outcomes: success, partial, failed |
| `401` | Missing or invalid bearer token |
| `422` | Malformed request body / unparseable URL string |
| `429` | Rate limit or monthly quota exceeded (include `Retry-After`) |
| `500` | Unhandled server error |

### Site-level status

| Status | Definition |
|---|---|
| `success` | Homepage plus ≥ 1 internal page scraped, total ≥ 300 words, `evidence_sufficient: true` |
| `partial` | Some content retrieved but below the success bar — homepage only, deadline hit mid-crawl, or most pages failed. `evidence_sufficient` may be true or false. |
| `failed` | No usable first-party content. `pages` is empty. |

`evidence_sufficient` is a separate, deterministic boolean: `pages_scraped >= 2 AND total_words >= 300`, or `pages_scraped == 1 AND total_words >= 500`. Tune with real data.

### Reason codes

| Code | Meaning |
|---|---|
| `invalid_url` | Unparseable or malformed |
| `blocked_scheme` | Non-http(s) scheme or disallowed port |
| `private_address` | Resolved to a blocked IP range (SSRF guard) |
| `dns_failure` | Host does not resolve |
| `connection_failed` | TCP/connection error |
| `tls_error` | Certificate or handshake failure |
| `timeout` | Per-page timeout exceeded |
| `deadline_exceeded` | Global request deadline hit before this page |
| `http_error_4xx` / `http_error_5xx` | Origin error status |
| `bot_blocked` | 403/429, challenge page, or WAF interstitial |
| `robots_disallowed` | Blocked by robots.txt |
| `parked_domain` | Registrar placeholder or for-sale page |
| `non_html_content` | Content-Type not HTML |
| `size_limit_exceeded` | Response exceeded byte cap |
| `render_failed` | Browser fallback errored or timed out |
| `too_thin` | Fetched successfully; insufficient text after cleaning |

### The other half of the failure model lives in the GPT

The API returning honest failures accomplishes nothing on its own. The GPT's instructions must contain an unambiguous rule, roughly:

> If the research action returns `status: "failed"` or `evidence_sufficient: false`, output `N/A — website could not be researched` and the reason code. Do not use your own knowledge of the company. Do not infer from the company name or domain. Do not use browsing. Do not proceed to the writing step. Every factual claim you write must be traceable to text inside a `pages[].content` field, and you must be able to name the `pages[].url` it came from.

Also worth stating explicitly: **the API cannot enforce your no-cross-contamination requirement.** The API is stateless and each request is isolated, which guarantees no leakage *inside the service* — but leakage happens in the GPT's context window, where two companies' research can coexist across turns. Mitigations: `canonical_domain` is present on every response so the GPT can self-check, and the GPT's instructions should require finishing and outputting one company completely before accepting another, ideally in a fresh conversation per company.

---

## 12. SSRF and security

This endpoint takes an arbitrary URL from an untrusted-ish source and fetches it from inside your infrastructure. It is a server-side request forgery primitive by definition. Two independent layers, because application-level validation alone has a known race condition.

### Layer 1 — application-level validation

**Scheme and port allowlist.** Only `http` and `https`. Only ports 80 and 443. Reject `file:`, `ftp:`, `gopher:`, `data:`, `javascript:`, `dict:`, `ldap:`, and everything else. Reject URLs containing credentials (`user:pass@host`) — a classic parser-confusion vector.

**Hostname handling.** IDNA-encode before any comparison (Unicode homograph and `。` fullstop tricks). Reject bare IP literals in V1 (a company website is a hostname), which removes decimal/octal/hex IP encoding tricks (`0x7f000001`, `2130706433`, `127.1`) in one move. Reject hostnames resolving to nothing.

**Resolve DNS ourselves, then validate every returned record.** Blocked ranges:

*IPv4:* `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10` (CGNAT), `127.0.0.0/8`, `169.254.0.0/16` (**link-local — this is AWS/Azure/DO metadata at 169.254.169.254**), `172.16.0.0/12`, `192.0.0.0/24`, `192.0.2.0/24`, `192.88.99.0/24`, `192.168.0.0/16`, `198.18.0.0/15`, `198.51.100.0/24`, `203.0.113.0/24`, `224.0.0.0/4`, `240.0.0.0/4`, `255.255.255.255/32`

*IPv6:* `::/128`, `::1/128`, `fc00::/7` (ULA), `fe80::/10` (link-local), `ff00::/8` (multicast), `2001:db8::/32`, `2002::/16` (6to4), `64:ff9b::/96` (NAT64), and **`::ffff:0:0/96` — IPv4-mapped addresses must be unwrapped and re-checked against the IPv4 list**, a commonly missed bypass.

*Hostnames:* `localhost`, `*.localhost`, `*.local`, `*.internal`, `metadata.google.internal`, `metadata.goog`, `instance-data`, `169.254.169.254` in any encoding.

**DNS rebinding.** This is the subtle one. Validating a hostname and then handing that hostname to the HTTP client creates a TOCTOU window: the attacker's DNS returns a public IP on the validation lookup and `127.0.0.1` on the connection lookup, with a 0-second TTL.

*Mitigation:* resolve once, validate the resolved IPs, then **connect to a validated IP directly** while preserving the `Host` header and TLS SNI. In httpx this is done with a custom transport or a pinned-resolver hook. The client must never perform its own second lookup. This applies to every redirect hop independently.

**Redirects — manual, validated, bounded.**

- `follow_redirects=False`; explicit loop, max 5 hops.
- Every hop re-runs the *entire* validation chain: scheme, port, credentials, hostname, DNS resolution, IP range check. A redirect is an attacker-controlled URL and deserves exactly as much suspicion as the input.
- Loop detection via a visited set.
- **Scope enforcement:** an off-registrable-domain redirect is allowed **only on the homepage, only once**, and sets `domain_rebased: true`. On any internal page, an off-scope redirect drops that page. This is your "don't silently wander onto another domain" requirement, and note the failure mode it prevents is not just security: without it, one `/partners` link redirecting to a partner's site quietly poisons your research with another company's copy — precisely the contamination you are designing against.
- Downgrade `https → http` is allowed but flagged; `http → https` is normal.

**Response handling.** Content-Type allowlist checked on headers before the body. Streamed download with a hard byte cap (2 MB), enforced on decompressed bytes. Explicit connect/read/total timeouts. No response header is ever echoed back to the caller.

### Layer 2 — network-level egress control

Application validation is necessary but should not be the only thing standing between a clever URL and your metadata endpoint. Run the fetcher with a restricted egress path:

- Docker network policy or cloud firewall rules **denying outbound to all RFC1918, loopback, link-local, and CGNAT ranges** from the container.
- On AWS: enforce IMDSv2 and set the metadata hop limit to 1 so containers cannot reach it at all. GCP/Azure have equivalents.
- No credentials, no cloud IAM roles, no internal service discovery reachable from the fetcher's environment. Assume the fetcher will eventually be tricked and make that outcome boring.

If you later add a proxy pool, route *all* outbound scraping through it — a proxy egress path is itself a strong SSRF control, since the proxy has no access to your private network.

### Browser-specific hardening

Playwright bypasses your httpx transport entirely and will happily fetch whatever the page asks for. Mitigations: launch with `--no-sandbox` **only** inside an already-sandboxed container (better: keep the Chromium sandbox on and grant `SYS_ADMIN` narrowly); run the browser in the same egress-restricted network; block non-document subresource types; disable downloads; use a fresh incognito context per page; hard timeout with forced context close.

### API-surface security

- **Auth:** `Authorization: Bearer <key>`. Custom headers are not supported by GPT Actions, so this is the right choice regardless. Keys stored hashed, rotatable, scoped (`standard` vs `advanced`), revocable.
- **Rate limiting:** per key, e.g. 60 req/min burst plus a monthly quota, enforced at the proxy where possible. Return `429` with `Retry-After`.
- **Request size:** cap request body at a few KB. There is no legitimate large request here.
- **Logging:** log `request_id`, key ID, requested domain, status, reason, timings, render count. Do **not** log full page content. You need this data to know when proxies become necessary.
- **Concurrency guard:** a global semaphore on in-flight crawls so a burst cannot exhaust memory via simultaneous browser contexts.
- **Abuse consideration:** this endpoint is a general-purpose fetcher. Anyone with a key can use it to fetch arbitrary public URLs from your IP. Keep keys private, monitor per-key domain diversity, and note that your server IP's reputation is a shared resource.

---

## 13. Suggested V1 limits and defaults

Deliberately conservative. Every default is chosen to fit inside 45 seconds and 100,000 characters with margin.

| Setting | `quick` | **`standard`** | `deep` | Notes |
|---|---|---|---|---|
| Global deadline | 18s | **32s** | 38s | Hard stop; assemble and return partial |
| Max pages scraped | 5 | **11** | 16 | Includes homepage |
| Max depth | 1 | **2** | 2 | Depth 3 out of scope |
| Max browser renders | 1 | **3** | 5 | 6 in `js_site` mode |
| HTTP concurrency per host | 3 | **4** | 4 | Politeness |
| Render concurrency | 1 | **2** | 2 | Memory-bound |
| Per-page connect timeout | 4s | **5s** | 5s | |
| Per-page read timeout | 6s | **8s** | 8s | |
| Per-page total timeout | 8s | **12s** | 12s | |
| Per-render timeout | 6s | **8s** | 10s | |
| Max response bytes/page | 2 MB | **2 MB** | 3 MB | Decompressed |
| Max redirect hops | 5 | **5** | 5 | Per URL |
| Per-page content cap, rank 1–3 | 5,000 | **8,000** | 8,000 | Characters |
| Per-page content cap, rank 4+ | 3,500 | **5,000** | 5,000 | Characters |
| Global content budget | 25,000 | **60,000** | 75,000 | Characters; well under 100k |
| Metadata share of payload | — | **< 10%** | — | Asserted in tests |
| Thin-site blog fallback | on | **on** | on | Needs ≥ 6s remaining |
| Careers overview pages | 0 | **max 1** | max 1 | Depth-1 only, never a hub |
| Sitemap URL cap | 500 | **2,000** | 3,000 | |
| Min words to keep a page | 40 | **40** | 40 | Else `too_thin` |
| Min score to select | 30 | **25** | 15 | |
| Boilerplate df threshold | — | **60%** | 60% | Requires ≥ 4 pages |
| Cache TTL | 14d | **14d** | 14d | Per canonical domain |
| Rate limit | — | **60/min per key** | — | Plus monthly quota |

**Worst-case timing sanity check (standard, JS-heavy site):** homepage HTTP 3s + render 8s = 11s → discovery 3s = 14s → 6 pages rendered at concurrency 2, 8s each = 24s → over deadline at 32s, so Wave 2 is skipped, in-flight work is cancelled, and the response returns ~7 pages with `stopped_reason: "deadline"`, `status: "partial"`. This is the intended behaviour, not a bug — verify it early with a deliberately slow test site.

**Expected normal case:** static B2B marketing site, 10–11 pages over HTTP only, 8–14 seconds, ~35,000 characters returned, zero renders.

---

## 14. Scaling to 20,000 websites/month

**20,000/month ≈ 667/day ≈ 28/hour average.** This is a small workload. A single 2 vCPU / 4 GB VPS running one container handles it with enormous headroom, assuming bursts are absorbed. Do not architect for scale you do not have — the risk here is over-building, not under-building.

The realistic pressures, in the order they will actually arrive:

**1. Cost of rendering, not request volume.** If 20% of sites need rendering, that is ~4,000 renders/month — fine in-process. If block rates push it to 60%, rendering becomes your dominant cost and the constraint that forces every subsequent change. **Instrument the render rate from day one.** It is the leading indicator for every other scaling decision.

**2. Caching — APPROVED for V1.** Redis, running as a third container alongside `api` and `caddy`.

**Cache key:** `sha256(canonical_domain + research_depth + sorted(focus) + CONFIG_VERSION)`

`CONFIG_VERSION` is a constant bumped whenever the lexicon, ranking weights, extraction logic, or response schema changes. Without it, every ranking tweak during development is invisible behind stale cache entries — this is the detail that makes the cache safe to iterate against, and it is easy to forget. `force_refresh: true` bypasses reads but still writes.

**Stored value:** the fully assembled response body, so a hit returns in under ~100 ms with no crawl at all.

**Differentiated TTLs**, because not all outcomes deserve the same lifetime:

| Outcome | TTL | Reasoning |
|---|---|---|
| `success` | 14 days | Marketing sites change slowly |
| `partial` | 3 days | Might do better on a retry |
| `failed` — `dns_failure`, `parked_domain`, `robots_disallowed` | 7 days | Stable conditions; stop re-hitting them |
| `failed` — `bot_blocked`, `timeout`, `connection_failed` | 24 hours | Often transient; retry sooner |
| `failed` — `private_address`, `invalid_url`, `blocked_scheme` | not cached | Rejected before any crawl; nothing to cache |

Negative caching matters more than it looks: without it, a dead domain retried across a campaign burns a full 32-second deadline every time.

Store the cache write **after** the response is assembled and **outside** the request deadline, so a slow Redis can never cost the caller time.

**3. Bulk processing wants a different endpoint shape.** The sync-under-45s design exists solely to satisfy GPT Actions. When you inevitably want to process 2,000 domains from a spreadsheet, that constraint is pure cost — you would rather run a slow, thorough 90-second crawl. Add, without touching the crawler core:

```
POST /v1/research/jobs      -> 202 {"job_id": "..."}
GET  /v1/research/jobs/{id} -> status + result
```

backed by Redis + arq/RQ. Same orchestrator, different budgets. The GPT keeps the sync endpoint forever.

**4. Extract the renderer.** When rendering exceeds ~30% of pages or memory becomes the binding constraint, move Playwright into its own container behind the adapter interface and scale it independently. Because of the day-one seam, this is a config change rather than a rewrite.

**5. Proxies — only when data justifies them.** Set an explicit trigger: *if `bot_blocked` exceeds 8% of requests over a rolling week, evaluate residential proxies.* Until then they are pure cost and added failure surface. Route through the renderer/fetcher adapters when the time comes.

**6. Persistence and observability.** Postgres for request logs and job results once you want per-domain history, ranking-quality analysis, or the ability to answer "why did this company's output look wrong three weeks ago." Metrics worth tracking from the start: success/partial/failed rate, block rate, render rate, `too_thin` rate, p50/p95 latency, average pages returned, average characters returned, cache hit rate, `stopped_reason` distribution.

**Explicitly rejected scaling paths for V1:** Kubernetes, Lambda/serverless (Playwright and 45-second budgets fit badly), autoscaling groups, multi-region, a message bus. At 28 requests/hour, one container behind Caddy on a small VPS is correct, and admitting that is the right engineering call.

### Approved deployment target — VPS + Docker + Caddy

**Machine: 2 vCPU / 4 GB RAM minimum.** The 4 GB floor is set by Chromium, not by FastAPI — two concurrent browser contexts plus the base image will use well over 1 GB under load. Configure swap so a render spike degrades instead of OOM-killing the API. Hetzner CX22 / DigitalOcean 4 GB class is ample.

**Three services via Docker Compose:**

| Service | Role |
|---|---|
| `caddy` | TLS 443 via automatic Let's Encrypt, HTTP→HTTPS redirect, request-size limit, coarse rate limiting, access logs |
| `api` | FastAPI + uvicorn + Playwright in one image (~1.2 GB with Chromium and its system deps) |
| `redis` | Result cache and rate-limit counters; bound to the internal network only, never published |

**Notes that will bite if missed:**

- **uvicorn workers = 2, not `2 × CPU`.** Worker count here is bounded by browser memory, not CPU. Each worker owns one persistent browser process.
- A **global in-flight semaphore** across workers caps concurrent crawls so a burst cannot spawn unbounded browser contexts.
- Egress firewall rules per §12 apply to the `api` container specifically.
- Persist only Caddy's certificate volume and Redis' snapshot. Everything else is disposable — the whole box should be reproducible from the compose file.
- Health check on `GET /v1/health`, plus a container restart policy. Chromium zombie processes are the most likely cause of a slow degradation, so recycle the browser every ~200 renders (§5) and alert on RSS growth.
- GPT Actions require a valid public certificate on port 443 — a real domain is a prerequisite, not a later step.

---

## 15. What I would deliberately NOT build in V1

**Your explicit rules, upheld:** no LLM of any kind inside the API, no search engines, no LinkedIn, no directories, no review sites, no news or third-party articles, no enrichment vendors, no cached model knowledge, no copywriting, no summarization.

**Additionally out of scope, on purpose:**

| Not building | Why |
|---|---|
| Proxy rotation / residential IPs | Cost and complexity with no evidence yet that it is needed. Gate on measured block rate. |
| CAPTCHA solving | Poor economics, legally murky, and a signal you should skip the site instead. |
| PDF, DOCX, or slide extraction | Some case studies are PDFs; the parsing and size handling is a project of its own. Track how often it costs you. |
| Depth 3+ / full-site crawling | Value per second falls off a cliff. Depth 2 reaches case study detail pages, which is the point. |
| Multi-URL batch endpoint | Conflicts with your one-at-a-time isolation requirement and with the 45s budget. Belongs in the async job API later. |
| Screenshots / visual capture | Not text evidence. Costs render time. |
| Image, logo, or brand-asset extraction | Not what the GPT writes from. |
| Multi-locale crawling | Duplicate content that eats the budget for near-zero marginal signal. Pick one locale. |
| Retry/backoff escalation | Explicitly against your instruction, and correctly so. Two attempts maximum, then report. |
| Company classification, ICP scoring, industry taxonomy | This is interpretation. It belongs in the GPT, and putting it here erodes the boundary that makes the system trustworthy. |
| Embeddings / vector storage / semantic ranking | Ranking is deterministic by design. Keyword + structural signals are sufficient and auditable. |
| Admin UI, dashboards, multi-tenant billing | Not the product yet. Structured logs are enough. |
| Change detection / re-crawl scheduling | Interesting later for trigger-based outreach; unnecessary now. |
| Postgres, queues, workers | Add when the async job API arrives, not before. |

---

## 16. Flaws in the proposed architecture

Ordered by how much they would cost you.

**1. The single-pass pipeline cannot reach your highest-value pages.** Your workflow — homepage → links → filter → rank → fetch → return — is depth-1 only. Case studies and success stories are your top-ranked concepts and they almost universally live at depth 2 behind an index page. As specified, the crawler returns the `/case-studies` grid (a page of titles and thumbnails) and nothing else. The two-wave frontier with hub-child inheritance (§6) is the fix, and it is the single most important change in this document.

**2. No global wall-clock deadline.** You specified `max_pages`, `max_depth`, `timeout`, and `max_content_length`, but the binding constraint is 45 seconds round-trip, and none of those enforce it. Per-page timeouts do not compose: eleven pages at 12s each is 132 seconds. Without a deadline that cancels in-flight work and returns partial results, roughly the slowest 10–20% of sites will fail as ChatGPT timeouts — which is indistinguishable to the user from "your tool is broken."

**3. Response size is unbudgeted.** 100,000 characters covers the whole JSON payload. Eleven pages of unclipped marketing copy will exceed it on content-rich sites, and truncation is silent — the model receives a cut-off JSON blob that reads as a complete answer. Per-page caps, a global budget, and relevance-descending page order (so truncation destroys the worst evidence, not the best) are all required.

**4. "HTTP fetch succeeded" is not the right fallback trigger.** Your flow tests whether the response "was successful." A React or Vue site returns HTTP 200 with an empty shell. The trigger must be a *content* sufficiency test — word count after DOM pruning, plus challenge-page detection (§4) — or you will silently return near-empty content for JS-heavy sites and never notice, because nothing errored.

**5. Per-page cleaning alone will leave visible boilerplate.** Your step 11 lists the right targets but frames it as a single-page problem. The cross-page frequency method (§8) is more effective, entirely deterministic, and cheap — and it turns repeated content from noise into a site-level evidence field rather than deleting it.

**6. Excluding careers wholesale discards strong signal.** Job *listings* are noise; a `/careers` or `/culture` overview page frequently contains the best personalization material on the site (growth, stack, values, locations, current investments). Demote to ~40, do not ban. See §7.

**7. Exposing `max_pages` / `max_depth` to the GPT is a footgun.** Language models emit plausible numbers with no basis. A GPT that decides on `max_pages: 40` produces a timeout the user reads as a broken tool. Use a `research_depth` enum and keep the numbers server-side (§9).

**8. Returning HTTP error codes for crawl failures will make the GPT behave worse.** ChatGPT surfaces non-2xx as tool errors, and the model's most likely recovery is to fall back on its own knowledge of the company — the exact failure you built this system to prevent. Return 200 with an explicit `status` field (§11).

**9. robots.txt is absent from the spec.** At 20,000 sites/month with an identifiable bot this is a real exposure and needs a deliberate decision rather than an omission. Recommendation in §6.

**10. No caching.** At your volume with repeated domains, GPT retries, and development iteration, a domain-keyed cache is the cheapest cost reduction available and materially speeds up your own testing loop.

**11. Content-Type is listed under security but is really a cost control too.** Checking it on headers before downloading the body is what stops a 40 MB PDF or an MP4 from consuming a page slot and 8 seconds of your deadline.

**12. Redirect scope enforcement is framed as a security issue; it is equally a data-integrity issue.** An internal `/partners` link that redirects to a partner's domain does not just widen your attack surface — it injects another company's marketing copy into the research payload. That is the cross-contamination you are explicitly designing against, arriving through a path you had not accounted for.

**13. The isolation guarantee is weaker than it sounds.** The API is stateless, so there is genuinely no leakage between companies *server-side*. But contamination happens in the GPT's context window across turns, where the API has no visibility and no control. Treat this as a GPT-instruction and workflow problem (one company per conversation, complete before starting the next) rather than something the architecture solves.

**14. Scrapling as the backbone is the wrong tool for this job.** Its adaptive-selector strength does not apply to one-shot crawls of thousands of distinct sites, and it abstracts exactly the transport layer where your SSRF and deadline requirements live. Keep it as an optional stealth tier behind the renderer adapter (§3).

---

## 17. Approved decisions and build order

### Decision record

| # | Decision | Resolution | Section |
|---|---|---|---|
| 1 | robots.txt | Respected in V1, entirely behind a `RobotsPolicy` config object | §6 |
| 2 | Careers pages | `/careers`, `/culture`, `/life-at-*` overview allowed at weight 40, max 1, depth-1 only. Listings, ATS paths and `careers.` subdomains excluded | §6, §7 |
| 3 | Blog / news | Excluded except as a single-page thin-site fallback with an explicit deterministic trigger | §7 |
| 4 | Caching | In V1. Redis, domain-keyed, `CONFIG_VERSION` in the key, differentiated TTLs including negative caching | §14 |
| 5 | JSON-LD | Included, narrowed to a field allowlist, 500-char cap. `aggregateRating` / `review` excluded as third-party data | §8 |
| 6 | Deployment | Small VPS, Docker Compose (`caddy` + `api` + `redis`), 2 vCPU / 4 GB floor | §14 |
| 7 | Async job API | Not built. Orchestrator kept framework-free so it drops in later | §1, §14 |
| 8 | Renderer adapter | As proposed, unchanged | §1, §5 |
| 9 | Stack | httpx + Playwright + selectolax + trafilatura. Scrapling not a core dependency; optional hard-site tier later | §3 |
| 10 | Two-wave depth-2 crawl | Kept. Hub detection with score inheritance | §6 |
| 11 | Hard deadline, char budget, deterministic ranking, category diversity, per-page traceability | All kept | §2, §7, §8 |
| 12 | Payload | Slimmed. Operational metadata moved to `debug`; metadata held under 10% of payload | §9, §10 |
| 13 | No summarization or interpretation | Enforced as a module boundary, not a guideline | §1, §8 |
| 14 | Crawl failures return HTTP 200 | Confirmed. Non-2xx reserved for auth, quota, malformed request, server error | §11 |

### The invariants

Six properties that must hold at every commit. If a change breaks one of these, the change is wrong, not the invariant:

1. **No model, no LLM, no external inference in the request path.** Every decision is deterministic and explainable from config.
2. **No text is returned that is not attributable to a URL** — content exists only inside `pages[]` (or the explicitly cross-page `site_wide_content`).
3. **The deadline always wins.** No code path can exceed it; every stop produces a valid response.
4. **Nothing outside crawl scope is ever fetched** — enforced at the URL guard, not by convention.
5. **The payload never exceeds the budget silently** — truncation is always reported.
6. **The crawler never rewrites, summarizes, or judges company content.**

### Build order

1. `security.url_guard` — normalization, IP validation, pinned-resolver transport, redirect loop. **First, and tested hardest.** Everything else fetches through it, and it is the one component where a bug is a vulnerability rather than a quality issue.
2. Pydantic models for request, response, and internal `Page` / `Candidate` — the contract everything else compiles against.
3. `fetch.http` with streaming caps, content-type gating, and the sufficiency test.
4. `discover` + URL normalization + exclusion rules + `RobotsPolicy`.
5. `rank` — lexicon as a standalone config file, scoring, quotas. Independently testable against fixture URL sets with no network.
6. `extract` — trafilatura path, DOM fallback, cross-page boilerplate removal, JSON-LD allowlist.
7. `orchestrate` — waves, deadline, budgets, thin-site fallback, assembly. Framework-free.
8. `fetch.render` behind the adapter.
9. FastAPI layer, auth, rate limiting, cache.
10. OpenAPI document tuned for the GPT (300/700-char description limits), then Docker Compose and Caddy.

**Test fixtures worth building early:** a static HTML site, a JS-only SPA, a site with a case-study index at depth 2, a site that redirects across domains, a deliberately slow site (deadline behaviour), a site with `Disallow: /`, a thin one-page site, and a locale-duplicated site. These eight cover most of the behaviour that is hard to reason about and easy to get wrong.

### Remaining artifacts after the API

The **GPT's system instructions are as load-bearing as the API** and should be written alongside it, not after. The API returning honest failures accomplishes nothing if the GPT's instructions do not forbid falling back to training knowledge, require citation to `pages[].url`, and gate on `evidence_sufficient`. Budget real effort for them.
