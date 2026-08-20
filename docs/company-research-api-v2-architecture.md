# Company Website Research API — V2 Architecture (Rich Evidence)

**Status: PROPOSAL — extends the APPROVED v1.1 architecture** · **Version 2.0** · **Date: 2026-08-20**

> v1.1 remains the source of truth for **security, deadlines, crawl scope, deterministic
> ranking, first-party-only research, payload limits, failure behaviour and GPT Action
> compatibility**. Nothing in this document relaxes any of it. Everything here is an
> *extension* that must survive those constraints, and where a capability cannot survive
> them, this document says so rather than quietly bending them.

---

## 0. The finding that shapes this document

The honest conclusion from costing every capability below against the **45-second GPT
Actions timeout**:

> **The full rich-evidence pipeline does not fit in a synchronous GPT Action.**

Rendering a JS site, interacting with three pages, parsing a PDF and capturing a
screenshot is a 60–120 second operation. There is no scheduling trick that makes it a
32-second one.

That gives a three-way split, and it is the most important decision in this document:

| Mode | Deadline | Capabilities | Consumer |
|---|---|---|---|
| **Sync (`/v1/research`)** | 32 s | Tiers 1–3, image metadata, at most 1 document | The Custom GPT, live in a chat |
| **Async (`/v1/research/jobs`)** | 180 s | Everything: deep interaction, multiple documents, screenshots | Batch tooling; the GPT via poll for "deep" requests |
| **Map (`/v1/map`)** | 8 s | URL universe only, no page fetching | Cheap preview / operator tooling |

The sync endpoint stays exactly as capable as it can be in 32 seconds and no more. The
async endpoint is where "approach Firecrawl" actually becomes true. **This is the change
that unlocks everything else, and it should be built before the fancy extraction, not
after.** Building screenshots and multi-document parsing into the sync path first would
produce a system that times out on precisely the rich sites the features exist for.

---

## 1. Firecrawl-like capabilities worth adding

Firecrawl's current surface (verified against their docs) spans formats — markdown, html,
rawHtml, screenshot, links, json, images, branding, product, audio, video, summary, query,
highlights — plus `scrape` / `crawl` / `map` / `search` / `extract` endpoints and browser
actions (click, write, press, wait, screenshot).

Judged strictly against *researching a public company website so a GPT can write accurate
personalisation*:

| Capability | Verdict | Why |
|---|---|---|
| **JavaScript rendering** | **Have (V1)** | Already tier 2. Extend, don't rebuild. |
| **Markdown extraction** | **Have (V1)** | trafilatura already emits heading-preserving markdown. |
| **Link discovery** | **Have (V1)** | Extend with rendered anchors + embedded router state. |
| **Sitemap crawling** | **Have (V1)** | Extend the cap and add nested-index following. |
| **Structured metadata** | **Have (V1)** | JSON-LD allowlist + OG. Extend to `Product`, `FAQPage`, `BreadcrumbList`. |
| **Page mapping (`/map`)** | **Add — V1.5** | High value, near-zero cost. Knowing the page universe *before* spending the budget is exactly the discipline this system needs. |
| **Browser actions (click/scroll/wait)** | **Add — V1.5, heavily constrained** | This is the single biggest evidence unlock. Portfolio grids and case-study lists behind "Load more" are the highest-value content on agency and manufacturer sites, and they are invisible to tier 2. Constrained to a closed allowlist (§4). |
| **Scroll-to-load** | **Add — V1.5** | Cheapest of all actions, no click semantics, high yield on lazy-loaded grids. |
| **Click-to-expand / accordions / tabs** | **Add — V1.5** | Common on services and FAQ pages; often where the actual capability detail lives. |
| **PDF parsing** | **Add — V1.5** | Many mid-market companies publish their best case studies only as PDFs. Directly serves the top-ranked research concept. |
| **DOCX parsing** | **Add — V1.5** | Cheap to add once the document pipeline exists (`python-docx`, MIT). Rarer than PDF but the marginal cost is small. |
| **Image extraction** | **Add — V1.5, metadata only** | Firecrawl returns a flat list of image URLs. We do better *for this use case*: alt text, caption, nearest heading and source page, filtered to content images. That context is what makes an image usable as evidence. |
| **Screenshots** | **Add — V2, selective** | Genuinely useful for portfolio and infographic pages where text extraction is weak. But it needs artifact hosting and signed URLs, and it is the least text-efficient evidence type. Last in, first dropped. |
| **Dynamic route discovery** | **Add — V1.5, narrow** | Extract routes from `__NEXT_DATA__` / `__NUXT__` / Apollo state **for link discovery only**, never for content. |
| **Caching with freshness control** | **Have (V1), extend** | Add render and document sub-caches (§11). |

---

## 2. Capabilities to intentionally skip

| Capability | Verdict | Why not |
|---|---|---|
| **LLM extraction (`json` / `extract`)** | **Never** | Violates invariant 1 outright. It is also the wrong division of labour: the whole point of this system is that the GPT reasons over evidence the crawler did not pre-digest. Adding schema-extraction here would put an un-auditable inference step between the website and the claim. |
| **`summary` / `query` / `highlights` formats** | **Never** | Same reason. These are LLM outputs wearing a format's clothing. Invariant 6. |
| **Web search (`/search`)** | **Never** | The entire premise is first-party-only. Search reintroduces snippets, directories and third-party articles through the back door. |
| **`branding` format (colours, fonts)** | **Skip** | Interesting, not evidence. A GPT writing personalised outreach cannot use the brand's hex codes. |
| **`product` format** | **Skip for now** | Deterministic product extraction is real, but it is tuned for e-commerce. Our `Product` JSON-LD handling covers the B2B case adequately. Revisit only if e-commerce prospects become a segment. |
| **Audio / video extraction** | **Never** | Out of scope, large, and would require transcription — which means a model. |
| **`rawHtml` format** | **Skip** | v1.1 is explicit: no huge raw HTML unless there is a technical reason. There isn't one. |
| **`write` / `press` actions** | **Never** | Typing into a page means forms, search boxes and logins. Forbidden by the interaction allowlist and by §13. |
| **`executeJavascript` action** | **Never** | Arbitrary JS is the single most dangerous thing a GPT could hand a crawler. Structurally impossible in this design (§4). |
| **Full-site `crawl`** | **Skip** | Conflicts with the page budget and the 45 s deadline, and produces worse evidence than 11 well-chosen pages. Our targeted two-wave crawl is the deliberate alternative. |
| **Batch scrape endpoint** | **Defer to async jobs** | The async job API covers this without a second surface. |
| **Change tracking** | **Defer to V2+** | Genuinely interesting for trigger-based outreach ("they just launched a new service line"). Not now; note it as the most compelling future addition. |
| **Stealth mode / proxy pools** | **Defer** | Unchanged from v1.1: gate on a measured `bot_blocked` rate above ~8 % over a rolling week. |
| **Generic autonomous browser agent** | **Never** | Explicitly out of scope. Every action is planned by a pure function from a closed allowlist. |

**The through-line:** we add *acquisition* capability and refuse *interpretation*
capability. Firecrawl is a general-purpose scraping platform that has grown an LLM layer;
we are a purpose-built evidence collector whose value depends on not having one.

---

## 3. Revised fetch-tier architecture

### Tiers

| Tier | What | Cost units | Typical wall-clock | Chosen when |
|---|---|---|---|---|
| **0** | Cache hit (render / document / response) | 0 | <100 ms | Always tried first |
| **1** | HTTP fetch | 1 | 0.3–1.5 s | Always tried first for HTML |
| **2** | Browser render | 8 | 3–6 s | Tier 1 output fails the adequacy test |
| **3** | Browser render + deterministic interaction | 14 | 5–9 s | Tier 2 output still inadequate **and** the DOM shows disclosure affordances |
| **4a** | PDF extraction | 6 | 1–4 s | URL/content-type is PDF and the document ranked high enough |
| **4b** | DOCX extraction | 3 | 0.5–2 s | Same, DOCX |
| **S** | Screenshot (element) | 3 | 1–2 s | Visual-evidence gate passes (§6). Rides on an existing tier 2/3 render. |
| **S+** | Screenshot (full page) | 5 | 2–4 s | Same, plus page height < 4000 px |
| **I** | Image-enabled render surcharge | +3 | +1–2 s | `include_images` and the page is visually significant |

Tier 3 *includes* the tier 2 render; they are not additive. Screenshots and image-loading
are **modifiers on a browser render**, not separate fetches — capturing a screenshot of a
page we already rendered is nearly free, and the architecture should make that cheapness
visible so the orchestrator schedules them together.

### The cost budget generalises `max_renders`

v1.1 rationed rendering with a simple counter. That does not generalise to five cost
classes. Replace it with a **cost budget in units**, spent by the orchestrator and
enforced before every escalation:

| Preset | Cost budget | Deadline | Notes |
|---|---|---|---|
| `quick` | 20 | 18 s | Tier 1–2 only, no interaction, no documents |
| `standard` | 60 | 32 s | Tiers 1–3, ≤1 document, image metadata, no screenshots |
| `deep` (sync) | 90 | 38 s | Tiers 1–3, ≤2 documents, image metadata |
| `deep` (async) | 400 | 180 s | Everything, including screenshots |

`max_renders` stays as a secondary hard cap so a pathological site cannot spend the whole
budget on browsers, but the unit budget is the primary control.

### Escalation is adequacy-driven, per page — and adequacy is per page type

v1.1 escalated on a single word-count threshold. That is wrong for index pages, and it
caused a real bug during V1 implementation: a case-study index is a grid of thumbnails
with almost no prose, so it failed the word test and was discarded — taking the entire
depth-2 crawl with it. The fix generalises into a first-class concept:

**Adequacy is measured against what the page type is *for*.**

| Page type | Adequate when |
|---|---|
| `homepage` | ≥ 200 words |
| `about`, `services`, `service_detail`, `solutions`, `products` | ≥ 120 words |
| `case_study`, `product_detail` | ≥ 150 words |
| `case_study_index`, `portfolio`, `products` (index) | **≥ 3 in-section child links** *or* ≥ 150 words |
| `industries`, `customers`, `testimonials` | ≥ 80 words **or** ≥ 4 named entities in list markup |
| `pricing` | ≥ 60 words **or** a table with ≥ 2 rows |
| `careers_overview`, `process`, `blog_post` | ≥ 100 words |

**Link-adequacy is the new idea.** An index page that yields three child links has done its
job even with forty words, and a page that yields zero child links is inadequate even with
four hundred. This is what makes tier-3 escalation trigger on exactly the pages where
"Load more" is hiding the evidence.

### Escalation rules

```
tier 1  ──inadequate──►  tier 2  ──inadequate AND affordances present──►  tier 3
   │                        │                                                 │
   └── adequate ────────────┴─────────────────── adequate ───────────────────┘
                                    │
                              still inadequate
                                    │
                                    ▼
                       keep best-so-far, mark degraded,
                       consider screenshot gate (§6)
```

Unchanged from v1.1 and non-negotiable: **never escalate speculatively**, never more than
one escalation step per page per crawl, and the deadline always wins — if less than the
tier's wall-clock estimate remains, the escalation is skipped and reported, not attempted.


---

## 4. Dynamic-page interaction design

### The architectural move: a pure planner, a dumb executor

```
rendered DOM snapshot ──►  plan_actions(snapshot, page_type, config)  ──►  [Action, ...]
                            (pure function, no browser, no model,            │
                             fully unit-testable without Playwright)         ▼
                                                                       ActionExecutor
                                                                    (runs the plan, no
                                                                     decisions of its own)
```

Splitting these matters for three reasons. The planner is testable against static HTML
fixtures with no browser at all, which is where the interesting logic lives. The executor
becomes small enough to audit line by line — and it is the component with the dangerous
capability. And it makes invariant 1 structurally checkable: `plan_actions` takes
`(DomSnapshot, PageType, ActionConfig)` and nothing else. **No argument to the planner is
derived from the request body beyond `page_type`, and no selector or script ever comes
from the caller.** That is a signature-level guarantee, not a code-review promise.

### Action allowlist (closed, versioned in `actions.yaml`)

| Action | Trigger | Max per page | Timeout | Reason code |
|---|---|---|---|---|
| `scroll_to_bottom` | lazy-load hints: `loading="lazy"`, `data-src`, infinite-scroll containers, or page height grew after render | 4 scrolls | 3 s total | `lazy_content` |
| `click_load_more` | visible button matching the load-more lexicon, page type is an index, child links < expected | 3 clicks | 4 s total | `paginated_index` |
| `expand_disclosures` | `<details>`, `[aria-expanded="false"]`, `.accordion`, `.faq-item` present and text below adequacy | 10 elements | 3 s total | `collapsed_content` |
| `activate_tabs` | `[role="tab"]` or `.tab` with ≥2 panels | 4 tabs | 3 s total | `tabbed_content` |
| `expand_read_more` | button/link matching the read-more lexicon inside `<main>` | 6 elements | 2 s total | `truncated_content` |
| `suppress_overlays` | consent/modal/newsletter overlay covering content | 1 | 0.5 s | `overlay_blocking` |

**Global caps per page:** at most **3 action groups**, **12 total element interactions**,
**6 seconds** of interaction wall-clock, and the whole plan is abandoned the moment the
global deadline reserve is threatened.

**Convergence rule:** after each action group, re-measure extracted text length and child
link count. If the delta is below a threshold (e.g. +40 words or +1 link), stop early. A
page that stops yielding gets no more clicks. This is what prevents a "Load more" that
loads nothing from consuming three clicks.

### Cookie consent: suppress, do not click

Worth calling out as a deliberate departure from what most crawlers do. Consent banners
block content, so something must be done about them — but **clicking "Accept all" is
making a legal declaration on the site owner's behalf from an automated client**. It is
also the single most common way a crawler accidentally accepts tracking.

**Design: never click a consent button.** Instead the executor injects a fixed,
code-owned stylesheet that hides known consent/overlay containers (`#onetrust-consent-sdk`,
`#CybotCookiebotDialog`, `[id*="cookie-banner"]`, `[class*="cookie-consent"]`,
`[role="dialog"][aria-modal="true"]`) and restores `body { overflow: auto }`. This is CSS,
not JavaScript; it changes nothing on the server; it cannot be influenced by the request;
and the selector list lives in YAML. It gets us the content without taking an action in
the user's name.

### Element eligibility — every condition must hold

An element is eligible for interaction only if **all** of the following are true:

1. Tag/role is in the allowlist: `button`, `[role=button]`, `[role=tab]`, `summary`,
   `[aria-expanded]`, `a[href^="#"]`, `a[href=""]`, `a:not([href])`
2. Visible: bounding box > 0, not `display:none` / `visibility:hidden` / `opacity:0`
3. Text or `aria-label` matches the action's compiled regex allowlist from YAML
4. **Not** inside `<form>`, `<nav>`, `<header>`, `<footer>`, or `[role=navigation]`
5. **Not** matching the forbidden-context selectors: `[type=submit]`, `[type=file]`,
   `input`, `textarea`, `select`, `[href*="login"]`, `[href*="cart"]`,
   `[href*="checkout"]`, `[href*="signup"]`, `[data-*="purchase"]`, anything inside an
   element matching `.cart`, `.checkout`, `.login`, `.account`
6. Does **not** carry an `href` that would navigate away (anything other than `#`,
   empty, or absent)
7. Is inside `<main>` / `[role=main]` / `<article>` when such a container exists

### Navigation guard — the interaction-specific SSRF surface

Interaction creates a URL-change vector that v1.1's fetch guard never sees, because the
browser navigates itself. Therefore, around **every** action:

```
before:  record page.url
action:  execute (with its own timeout)
after:   if page.url changed:
             validate the new URL through UrlGuard with the frozen CrawlScope
             ├── invalid or off-scope ──► abort the whole interaction plan,
             │                            discard the page, record `interaction_navigated_away`
             └── valid and in-scope  ──► go back(); record the URL as a *discovered candidate*,
                                          never as this page's content
```

An in-scope navigation is genuinely useful — it is a route we had not discovered — but it
must re-enter the normal ranking pipeline, not be silently absorbed as the current page's
evidence.

### Forbidden absolutely

No form submission, no typing of any kind, no credentials, no login, no purchase, no
message sending, no financial interaction, no file upload, no download triggering, no
drag, no `window.open`, no navigation clicks, no arbitrary JavaScript from any source
outside the code-owned action library. The GPT has **no** field anywhere in the request
schema that can express an action, a selector, or a script — and §16 adds an invariant
that makes adding one a test failure.

### Logging

Every executed action emits: `page_url`, `action`, `reason_code`, `selector_class`
(the YAML rule that matched, never a raw user-influenced selector), `element_index`,
`duration_ms`, `text_delta`, `links_delta`, `navigated` (bool). This lands in the `debug`
block and in structured logs. Interaction is the highest-risk subsystem; it should also be
the most observable.

---

## 5. Image discovery and filtering

### Principle: metadata, never bytes

**The API never downloads image bytes in the research path.** It collects metadata from
the DOM and returns URLs. This removes an entire SSRF and bandwidth surface — there is no
image fetcher to protect — and image bytes were never the evidence anyway. The GPT cannot
see an image; what it can use is *"a case-study page shows a photo captioned 'Boeing wing
assembly line, 40 % faster changeover'"*. That is the alt text and the caption, not the
pixels.

The one consequence: `naturalWidth`/`naturalHeight` are unavailable when image loading is
blocked (which it is, for render speed). Dimensions therefore come from `width`/`height`
attributes and `srcset` descriptors, and are `null` when absent. Filtering must not depend
on dimensions alone — hence the layered rules below.

### Collection

From the **post-interaction** rendered DOM (so lazy-loaded portfolio images are included):
`<img>`, `<picture><source srcset>`, `<figure>`, and `[itemprop="image"]`. Background
images in inline styles are collected only when the element also carries a caption or
heading sibling — otherwise they are almost always decorative.

Per image: `url` (absolutised, normalised), `alt`, `caption` (`<figcaption>`, or an
adjacent `<p class*=caption>`, or the `title` attribute), `nearest_heading` (nearest
preceding `h1`–`h3` in document order), `width`/`height` when known, `source_page_url`,
`same_origin` (bool), `context` (the page type it was found on).

### Filtering — layered, all deterministic

1. **Dimension floor** — drop if a known dimension is < 200 px on either axis.
2. **URL pattern blocklist** — `icon`, `logo`, `sprite`, `favicon`, `pixel`, `1x1`,
   `spacer`, `badge`, `avatar`, `placeholder`, `loader`, `arrow`, `chevron`, `bullet`,
   `divider`, `pattern`, `bg-`, `background`, `/emoji/`, `/flags/`.
3. **Extension** — drop `.svg`, `.gif` by default (icons and spinners); keep `.jpg`,
   `.png`, `.webp`, `.avif`.
4. **Tracking pixels** — drop 1×1, zero-dimension, and images on known analytics hosts.
5. **Cross-page frequency** — **drop any image URL appearing on ≥ 60 % of crawled pages.**
   This is the same trick that already kills text boilerplate, applied to images, and it
   is what removes the global logo, the nav graphic and the footer badge *without a
   hard-coded list* — which no pattern blocklist can do reliably. It only works because we
   hold N pages from one site, and it is the single most effective rule here.
6. **Duplicate collapse** — same URL across pages returns once, with the highest-value
   source page recorded.

### Prioritisation

Score = source page value + context signals:

| Signal | Weight |
|---|---|
| Source page type is `case_study`, `portfolio`, `product_detail` | +40 |
| Source page type is `products`, `services`, `customers` | +20 |
| Has a caption | +25 |
| Has meaningful `alt` (> 3 words, not the filename) | +20 |
| Inside `<figure>` | +15 |
| Inside `<main>` | +10 |
| Filename hints: `case-study`, `project`, `result`, `before`, `after`, `chart`, `infographic`, `award`, `screenshot` | +20 |
| Source page type is `homepage` | +5 |
| No alt and no caption | −30 |

**Caps:** ≤ 4 images per page, ≤ 12 per response, ≤ 140 characters of metadata each.
Below a minimum score, images are simply not returned — an image with no alt, no caption
and no heading is not evidence.

### Explicitly not done

No OCR, no vision model, no captioning, no classification of image *content*, no
aesthetic scoring. The crawler exposes image evidence; the GPT decides what it means, and
mostly it will use the caption and alt text — which is correct.

---

## 6. Screenshot design

### Gate — deterministic, all conditions in config

A screenshot is captured only when **at least one** of these holds, and the cost budget
and deadline both allow it:

1. **Text-weak but visually dense** — after tier 3, extracted text is still below adequacy
   *and* the page has ≥ 3 content images or ≥ 1 `<canvas>` / `<svg>` over 300 px.
2. **Visual page type** — `portfolio`, `case_study`, `product_detail` **and** ≥ 3 content
   images survived filtering.
3. **Chart or infographic markers** — `<canvas>`, `.chart`, `.infographic`, `.graph`,
   `[class*="visualization"]`, or a `<svg>` containing `<text>` nodes (a chart, not an icon).
4. **Explicitly requested** — `include_screenshots: true` (advanced scope / async mode).

Never by default on `quick` or `standard`. Never more than **3 per research call**.

### Kind — prefer targeted

| Kind | When | Typical size |
|---|---|---|
| **Element** (`main`, `[role=main]`, `article`, or the chart container) | **Default.** Smaller, no chrome, no cookie banner, higher signal | 80–250 KB |
| **Viewport** (1280×900) | No usable main container | 100–300 KB |
| **Full page** | Only when page height < 4000 px **and** explicitly requested | 200 KB–2 MB |

Element screenshots of the chart or figure that triggered the gate are the ideal case:
they are small, and they contain exactly the thing text extraction could not express.

### Delivery — signed URLs, never base64

Embedding a 200 KB image as base64 would consume 270 000 characters against a 100 000
character payload limit. It is not a trade-off; it is impossible. Firecrawl reaches the
same conclusion — their screenshot URLs expire after 24 hours.

**Artifact service:**

```
GET /v1/artifacts/{artifact_id}?exp={unix}&sig={hmac_sha256}
```

- `artifact_id` is a random 128-bit token; **no request input appears in the path**
- `sig = HMAC-SHA256(secret, f"{artifact_id}:{exp}")`, constant-time compared
- TTL **30 minutes** (short enough to bound exposure, long enough for a chat turn);
  files swept on expiry
- Unauthenticated by design — an image fetch cannot carry a bearer header — but
  unguessable, expiring, single-object and read-only
- No listing endpoint, no directory traversal possible (the id is the whole key)
- Response pins `Content-Type: image/webp`, `Content-Disposition: inline`,
  `X-Content-Type-Options: nosniff`, `Cache-Control: private, max-age=1800`
- Storage: local volume in V2, S3-compatible behind the same interface later; a global
  disk quota with LRU eviction so screenshots cannot fill the disk

**WebP at quality 80**, not PNG — typically 60–70 % smaller for rendered pages.

In the response, each screenshot costs ~200 characters:

```json
{
  "id": "shot_9f3a...",
  "url": "https://research.example.com/v1/artifacts/shot_9f3a...?exp=1787220000&sig=...",
  "source_page_url": "https://acme.com/case-studies/boeing",
  "kind": "element",
  "selector_class": "main_content",
  "width": 1280, "height": 1840, "bytes": 184320,
  "expires_at": "2026-08-20T09:30:00Z",
  "reason": "visual_page_type"
}
```

### An honest limitation

A URL in an Action response does **not** guarantee ChatGPT will look at the image. The
GPT can surface the link to the user, and some clients will render it, but Action results
are consumed as text and vision over an arbitrary fetched URL is not something to rely on.

**Therefore screenshots are primarily for the human reviewing the research, and only
opportunistically for the model.** They are correspondingly the lowest-priority evidence
class and the first thing dropped under budget pressure. Anyone expecting the GPT to
"read the chart" should be told plainly that it probably will not.


---

## 7. PDF and DOCX design

### Discovery — documents become a separate candidate pool

v1.1 excludes `.pdf` and `.docx` by file extension. That changes: those extensions are no
longer *excluded*, they are *routed* into a **document candidate pool** that is ranked and
budgeted independently of the page pool. Everything else in the extension blocklist stays.

Sources: anchors on crawled pages (with anchor text — the strongest signal), sitemap
entries, and `Content-Type` discovered at fetch time when a page URL turns out to serve a
PDF.

### Ranking — the same concept lexicon, applied to link text and filename

A document scores from its anchor text, its filename tokens, and the heading nearest the
link, using the existing `lexicon.yaml` concepts. `acme-boeing-case-study.pdf` linked
under "Client Results" ranks very high; `terms-of-business.pdf` is excluded outright.

| Signal | Adjustment |
|---|---|
| Concept match (case study, results, portfolio, capabilities, overview, brochure, report) | concept weight |
| Linked from a `case_study*` / `portfolio` / `services` page | +25 |
| Filename contains a year within 3 years | +10 |
| Anchor text is generic ("Download", "PDF", "Click here") with no other signal | −25 |
| Concept match on: terms, privacy, policy, gdpr, invoice, purchase-order, application-form | **excluded** |
| Size (from `Content-Length`) > limit | **excluded before download** |

### Fetch and parse limits

| Limit | Value | Rationale |
|---|---|---|
| Documents per response | 1 (`standard`) / 2 (`deep` sync) / 5 (async) | Each is 3–6 cost units and 1–4 s |
| Max download size | 10 MB | Enforced by the same streaming byte cap as HTML |
| Max pages parsed | 40 | Beyond this the marginal evidence is near zero |
| Parse timeout | 8 s | In a worker thread, hard-cancelled |
| Text extracted per document | 12 000 chars (`deep`), 8 000 (`standard`) | Budgeted like page content |
| Total document text per response | 12 000 chars | §10 priority rules |

**Fetched through the existing guarded httpx fetcher**, not the browser — so the full
v1.1 SSRF stack (pinned DNS, per-hop redirect revalidation, scope enforcement, streaming
byte cap) applies unchanged. This is why documents are downloaded rather than opened in a
tab.

### Parsing — library choice, with a licensing warning

| Format | Library | Licence | Note |
|---|---|---|---|
| PDF | **`pdfplumber`** (on `pdfminer.six`) | MIT | Good text and table fidelity, pure Python, no native deps |
| DOCX | **`python-docx`** | MIT | Paragraphs, tables, headings |

**Do not use PyMuPDF / `fitz`.** It is faster and tempting, and it is **AGPL-3.0** — for a
commercial API served over a network, the AGPL's network clause is a genuine obligation,
not a formality. A commercial licence is available for purchase; if you would not buy it,
do not build on it. Flagging this now because it is exactly the kind of dependency that is
painful to remove after it is load-bearing.

`pypdf` is also excluded — weaker extraction, and the internal guidance for this project
rules it out.

### Safety

| Control | Implementation |
|---|---|
| **MIME validation** | Magic-byte sniff, not the header: PDF must start `%PDF-`; DOCX must be `PK\x03\x04` **and** contain `word/document.xml`. A file that lies about its type is rejected. |
| **Size cap** | Streaming abort at 10 MB, pre-checked against `Content-Length` |
| **Zip-bomb defence (DOCX)** | Inspect the zip central directory *before* extracting; reject if total uncompressed size > 50 MB or compression ratio > 100:1 |
| **XXE defence (DOCX)** | XML parsed with external entity resolution disabled |
| **Encrypted PDFs** | Rejected, `reason_code: document_encrypted` — never attempt a password |
| **Embedded scripts** | Never executed. `pdfplumber` does not execute PDF JavaScript; embedded JS, launch actions and embedded files are ignored entirely |
| **Resource limits** | Parsing runs in a bounded thread pool with a hard timeout; a hung parse cannot hold the request |
| **Page cap** | Stop at 40 pages regardless of document length |

### Output — traceability preserved

Page numbers are preserved inline as lightweight markers rather than as a nested
structure, which keeps the payload flat and lets the GPT cite precisely:

```json
{
  "url": "https://acme.com/downloads/boeing-case-study.pdf",
  "source_page_url": "https://acme.com/case-studies",
  "doc_type": "pdf",
  "title": "Boeing: 40% Lead Time Reduction",
  "page_count": 8,
  "pages_parsed": 8,
  "word_count": 1420,
  "truncated": false,
  "content": "[p.1] Boeing: 40% Lead Time Reduction\n\n[p.2] The Challenge\nBoeing's..."
}
```

`title` comes from PDF metadata or the first heading — never generated. No summarisation,
no restructuring, no OCR (a scanned PDF with no text layer yields
`reason_code: document_no_text_layer`, honestly reported rather than silently empty).

---

## 8. SPA and JS-heavy website strategy

### Framework detection (deterministic, for telemetry and tactics)

Detected from `<meta name="generator">`, script `src` patterns and global markers:
Next.js (`/_next/`, `__NEXT_DATA__`), Nuxt (`__NUXT__`), React, Vue, Angular (`ng-version`),
Gatsby (`page-data.json`), Webflow (`wf-`), Framer, Squarespace, HubSpot, Shopify.

Recorded in `debug.framework`. This is worth having even before it changes behaviour,
because "which frameworks are we failing on" is the question that decides what to build
next.

### Route discovery from embedded state — links only, never content

Modern SPAs ship their routing data in the page source. Mining it is deterministic and
cheap:

| Source | Extract |
|---|---|
| `__NEXT_DATA__` | `props.pageProps` link-shaped strings, `buildManifest` route list |
| `__NUXT__` | payload route and slug fields |
| `__APOLLO_STATE__` / Redux preload | fields matching `slug`, `path`, `href`, `url` |
| Gatsby `page-data.json` | linked page paths |
| `<link rel="canonical">`, `rel="alternate"` | canonical and locale routes |

**Hard rule: embedded state is used for link discovery only, never for content
extraction.** Mining arbitrary JSON for text produces junk — serialised component props,
analytics config, duplicated fragments — and it would be extraction the crawler cannot
justify. Content always comes from the rendered DOM, where it is attributable to what a
visitor actually sees.

Extraction is conservative: only strings that parse as same-scope paths, capped at 200
candidates, each passed through the normal normalisation, exclusion and guard pipeline.

### No private framework APIs

`_buildManifest.js` route *patterns* (`/case-studies/[slug]`) are not concrete URLs and
are not worth the coupling. Skipped, per the brief's instruction to avoid
framework-specific private APIs without substantial benefit.

### JS-site mode, carried forward and extended

v1.1's `js_site` mode (homepage needed rendering ⇒ treat the whole site as a JS app,
reduce page count, render-first) stays, and gains: interaction is enabled by default for
index page types; embedded-state route discovery is enabled; and the cost budget is
weighted toward fewer, better pages, because on a JS site every page costs 8+ units
instead of 1.

---

## 9. Revised evidence model

Four evidence classes, each a first-class array, none of them merged:

| Class | Carries | Cardinality |
|---|---|---|
| `pages[]` | Cleaned first-party page text | 0–16 |
| `documents[]` | Extracted PDF/DOCX text | 0–5 |
| `images[]` | Image metadata (no bytes) | 0–12 |
| `screenshots[]` | Signed URLs to rendered captures | 0–3 |

**Every evidence object carries, without exception:**

- `evidence_type` — `page` \| `document` \| `image` \| `screenshot`
- `url` — the artifact's own URL (the page, the PDF, the image, the capture)
- `source_page_url` — the in-scope page where this evidence was found
  (for a page, these are the same; for a PDF it is the page that linked to it)
- type-specific metadata

For pages, `source_page_url == url`. For everything else the two differ, and the
distinction is what lets the GPT say *"their case-studies page links a PDF that claims a
40 % reduction"* rather than asserting it free-floating.

Invariant 2 extends from "no text without a URL" to **"no evidence object without both a
URL and a source page"**.

---

## 10. Revised response schema (v2.0)

```json
{
  "schema_version": "2.0",
  "request_id": "req_01j8k2m9x",
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
    "organization": { "founding_date": "1998", "locations": ["Austin, TX, US"] },
    "site_wide_content": "Recurring text across most pages, stated once."
  },

  "pages": [
    {
      "evidence_type": "page",
      "url": "https://www.example.com/case-studies/boeing",
      "source_page_url": "https://www.example.com/case-studies/boeing",
      "page_type": "case_study",
      "title": "Boeing: 40% lead time reduction",
      "headings": ["The Challenge", "Our Approach", "Results"],
      "word_count": 743,
      "truncated": false,
      "enrichment": ["interaction:expand_disclosures"],
      "content": "## The Challenge\nBoeing was facing..."
    }
  ],

  "documents": [
    {
      "evidence_type": "document",
      "url": "https://www.example.com/downloads/boeing-case-study.pdf",
      "source_page_url": "https://www.example.com/case-studies",
      "doc_type": "pdf",
      "title": "Boeing: 40% Lead Time Reduction",
      "page_count": 8,
      "pages_parsed": 8,
      "word_count": 1420,
      "truncated": false,
      "content": "[p.1] Boeing: 40% Lead Time Reduction\n\n[p.2] The Challenge\n..."
    }
  ],

  "images": [
    {
      "evidence_type": "image",
      "url": "https://cdn.example.com/img/boeing-line.jpg",
      "source_page_url": "https://www.example.com/case-studies/boeing",
      "alt": "Boeing wing assembly line after changeover redesign",
      "caption": "Changeover time fell from 6 hours to 3.5 hours",
      "nearest_heading": "Results",
      "width": 1600, "height": 900,
      "same_origin": false
    }
  ],

  "screenshots": [
    {
      "evidence_type": "screenshot",
      "id": "shot_9f3a2c",
      "url": "https://research.example.com/v1/artifacts/shot_9f3a2c?exp=1787220000&sig=...",
      "source_page_url": "https://www.example.com/portfolio",
      "kind": "element",
      "width": 1280, "height": 1840, "bytes": 184320,
      "expires_at": "2026-08-20T09:30:00Z",
      "reason": "visual_page_type"
    }
  ],

  "coverage": {
    "found": ["homepage", "about", "services", "case_study", "industries"],
    "missing": ["pricing", "testimonials"],
    "evidence_available": { "pages": 11, "documents": 3, "images": 24, "screenshots": 1 },
    "evidence_returned": { "pages": 9, "documents": 1, "images": 8, "screenshots": 1 },
    "dropped_for_budget": ["documents:2", "images:16", "pages:2"]
  },

  "crawl_summary": {
    "pages_scraped": 9, "pages_failed": 1,
    "tier_usage": { "http": 14, "render": 3, "interact": 2, "document": 1, "screenshot": 1 },
    "actions_performed": 7,
    "cost_units_spent": 54,
    "max_depth_reached": 2,
    "total_words": 6820, "content_chars": 46200,
    "elapsed_ms": 29140,
    "stopped_reason": "budget_reached",
    "skipped_evidence": ["screenshots:deadline"],
    "cache_hit": false
  },

  "notes": ["js_site_mode", "interaction_used"]
}
```

### What changed from v1.1, and why

| Change | Reason |
|---|---|
| `schema_version` → `2.0` | Breaking: three new arrays, `coverage` restructured |
| `evidence_type` on every object | The GPT must be able to weight a PDF claim differently from body copy |
| `source_page_url` everywhere | Traceability for non-page evidence (invariant 2 extended) |
| `enrichment[]` on pages | Tells the GPT this text only exists because a disclosure was expanded — useful when judging whether something is prominent on the site or buried |
| `coverage.evidence_available` vs `evidence_returned` | **The GPT must know that evidence exists but was not returned.** Otherwise absence reads as non-existence, which is exactly the inference this system exists to prevent |
| `dropped_for_budget[]`, `skipped_evidence[]` | Same reason; makes invariant 5 visible in the payload |
| `tier_usage`, `cost_units_spent` | Operational visibility — the leading indicator for when to add proxies or move to async |

### Priority rules under the 100 000-character limit

Evidence is admitted in strict order, each class with a reserved floor and a hard ceiling:

| Rank | Class | Floor | Ceiling | Note |
|---|---|---|---|---|
| 1 | Homepage text | 6 000 | 8 000 | Always present if the crawl succeeded |
| 2 | Case-study & portfolio page text | 12 000 | 24 000 | Highest-value evidence type |
| 3 | Services / about / industries / pricing text | 10 000 | 22 000 | |
| 4 | Remaining page text | 0 | 8 000 | |
| 5 | Document text | 0 | 12 000 | Ranked; only the top document at `standard` |
| 6 | `site_wide_content` | 0 | 1 500 | |
| 7 | Image metadata | 0 | 1 700 | ~140 chars × 12 |
| 8 | Screenshot references | 0 | 800 | ~200 chars × 3 (metadata only) |
| — | Envelope, coverage, summary | — | ~4 000 | |
| | **Total ceiling** | | **~82 000** | 18 % headroom under 100 000 |

**Drop order under pressure is the reverse: screenshots → images → low-rank pages →
documents → high-rank pages.** Text always beats visuals; a case study always beats a
team page. And a class that is dropped is always *reported* as dropped.


---

## 11. Revised caching strategy

The brief asks not to over-engineer this. The honest answer: of the five caches proposed,
**three are worth building and two are not.**

| Cache | Build? | Key | TTL | Rationale |
|---|---|---|---|---|
| **Assembled response** | **Have (V1)** | `sha256(domain + depth + focus + CONFIG_VERSION + config fingerprint)` | 14 d / 3 d / 7 d / 24 h by outcome | Already the highest-value cache; covers the common path entirely |
| **Rendered HTML** | **Add — V1.5** | `sha256(url + interaction_plan_version + CONFIG_VERSION)` | 7 d | A render costs 8 units and 3–6 s. Reused across `force_refresh`, across depth presets, and during development iteration on ranking — where it turns a 30 s test loop into a 2 s one |
| **Parsed documents** | **Add — V1.5** | `sha256(document_url)` | 30 d | Documents are immutable in practice, expensive to parse, and frequently linked from several pages within one crawl |
| **Screenshots** | **Add — V2** | `artifact_id` on disk | 30 min | Not really a cache — it is the artifact store the signed URL serves from. Same mechanism, different purpose |
| **Raw HTML** | **Skip** | — | — | Cheap to refetch (1 unit, <1 s) and large to store. Caching it buys almost nothing and costs Redis memory that the render cache uses better |

Cache invalidation stays exactly as v1.1 designed it: `CONFIG_VERSION` plus a fingerprint
of the YAML files, so editing a weight cannot be masked by stale entries. The render cache
additionally keys on `interaction_plan_version`, because a change to `actions.yaml`
changes what a rendered page contains.

Storage: one Redis for response/render/document caches (rendered HTML compressed with
zstd before storing — typically 8:1, which is what makes it affordable), one disk volume
for artifacts.

---

## 12. Revised deadline and resource budgeting

### Phase budgeting with hard cuts

The single global deadline is no longer sufficient, because there are now four evidence
classes competing for it and the expensive ones arrive last. Split the deadline into
phases with reserved shares and **hard cuts**:

| Phase | Share of deadline | Work | If it overruns |
|---|---|---|---|
| **0 — Map** | 8 % | robots, sitemap, homepage anchors; build the page universe | Proceed with whatever was mapped |
| **A — Acquire** | 55 % | Tier 1/2 fetch of selected pages; wave 1 and wave 2 | Cut wave 2; B and C are skipped entirely |
| **B — Enrich** | 22 % | Tier 3 interaction on inadequate high-value pages; document fetch and parse | Skip remaining enrichment, report it |
| **C — Visual** | 8 % | Screenshots (image metadata is free — it came from the DOM) | Skip, report `skipped_evidence` |
| **D — Assemble** | 7 % | Boilerplate removal, budgeting, serialisation | Never cut; this is the reserve |

At `standard` (32 s): map 2.5 s, acquire 17.5 s, enrich 7 s, visual 2.5 s, assemble 2.2 s.

Every phase boundary is a checkpoint: if the elapsed time already exceeds the cumulative
share, the phase is skipped in full rather than started and abandoned. **A skipped phase
is always named in `skipped_evidence`** — the GPT needs to know that documents were not
examined, not merely that none were returned.

### Two budgets, both enforced

- **Wall-clock (the deadline)** — hard, cancels in-flight work, unchanged from v1.1
- **Cost units** — bounds *money and load* rather than time; prevents a fast site from
  triggering 15 renders just because it responded quickly

Escalation requires **both**: enough remaining units *and* enough remaining wall-clock for
that tier's estimated duration plus the assembly reserve.

### Concurrency

| Resource | Limit |
|---|---|
| HTTP per host | 4 (1 when a crawl-delay is declared) |
| Browser contexts per worker | 2 |
| Document parses in flight | 2 (thread pool, CPU-bound) |
| Screenshot encodes | 1 (CPU-bound, and rides on an existing render) |
| Crawls per worker | 4 |

### Where this leads

At `standard`, the phase table permits roughly: 9–11 pages, 2–3 renders, 1–2 interactions,
1 document, 0 screenshots. That is a genuinely richer result than V1 and still fits the
GPT Actions window.

**Everything beyond that needs the async job API.** Section 0 is not a caveat; it is the
plan.

---

## 13. Security implications

Every v1.1 control is preserved. The new capabilities add four attack surfaces, each with
a specific control.

### 13.1 Browser interaction — the largest new surface

| Risk | Control |
|---|---|
| Action navigates off-scope | Navigation guard around every action (§4): URL compared before/after, revalidated through `UrlGuard` with the frozen scope, plan aborted on mismatch |
| Action triggers a download | `accept_downloads=False` on every context; download events cancelled |
| Action submits a form or types | Structurally impossible — no `write`/`press` action exists, and form contexts are in the eligibility blocklist |
| GPT injects a selector or script | **Structurally impossible** — the request schema has no field expressing an action, selector or script; `plan_actions` takes only `(DomSnapshot, PageType, ActionConfig)`; enforced by new invariant 7 |
| Interaction loop never terminates | Three caps: action groups, total interactions, wall-clock — plus the convergence rule |
| Page opens a new tab/window | Contexts reject `popup` events; `--disable-popup-blocking` is *not* set |

### 13.2 Browser subresources

Route interception extends: `document` requests are guard-validated and scope-checked as
in v1.1. When image loading is enabled for a visual page, `image` requests are permitted
but **never** followed as navigations and never fetched by our own client. Chromium still
does its own DNS for subresources — unchanged from v1.1's documented limitation, and still
closed by the **network-level egress rules**, which become more load-bearing here, not
less. `--host-resolver-rules` remains available for deployments that want browser-side
pinning at the cost of a browser per render.

### 13.3 Document downloads

Downloaded through the **guarded httpx fetcher**, never the browser — so pinned DNS,
per-hop redirect revalidation, scope enforcement and the streaming byte cap all apply
unchanged. Plus: magic-byte MIME validation, zip-bomb ratio checks, XXE-disabled XML,
encrypted-PDF rejection, no script execution, bounded parse threads with hard timeouts.

### 13.4 Screenshot artifacts

| Risk | Control |
|---|---|
| Enumeration | 128-bit random id; HMAC signature; constant-time compare |
| Indefinite exposure | 30-minute expiry enforced on both signature and file sweep |
| Path traversal | The id *is* the key; no request input reaches the filesystem path |
| Disk exhaustion | Global quota with LRU eviction + TTL sweeper |
| Serving hostile content | `Content-Type` pinned to `image/webp`, `nosniff`, `inline`, no listing endpoint |
| Leaking a private page | Screenshots only ever capture in-scope, publicly-fetched pages — the same pages whose text is already in the response |

### 13.5 Images

**No image bytes are ever downloaded by the API.** There is no image fetcher, therefore no
image SSRF surface. Image URLs are normalised and returned as metadata; whether they are
third-party CDN hosts is recorded in `same_origin` and left to the consumer.

### 13.6 Unchanged and non-negotiable

Scheme/port allowlists, credential rejection, IP-literal and shorthand-encoding rejection,
private/link-local/CGNAT/IPv4-mapped blocking, DNS pinning against rebinding, manual
per-hop redirect revalidation, frozen crawl scope, robots policy, bearer auth, per-key rate
limiting, HTTP-200 crawl-failure semantics.

---

## 14. V1 vs V1.5 vs V2

### V1 — shipped

Tiers 0–2. HTML pages, deterministic ranking with category quotas, two-wave depth-2 crawl,
robots policy behind config, JSON-LD allowlist, cross-page boilerplate removal, response
cache, full SSRF stack, 505 tests.

### V1.5 — "rich acquisition"

The highest value-per-unit-of-risk work. Everything here fits the sync endpoint.

1. **Page-map stage** (§8 of v1.1 discovery, formalised) — internal, cheap, improves every
   subsequent decision
2. **Adequacy-by-page-type**, including link-adequacy — a small change that makes tier-3
   escalation trigger correctly
3. **Cost-unit budgeting** — replaces `max_renders`
4. **Tier 3 interaction**: `scroll_to_bottom`, `click_load_more`, `expand_disclosures`,
   `activate_tabs`, `expand_read_more`, `suppress_overlays`, with the pure planner and the
   navigation guard
5. **Image metadata layer** with cross-page frequency filtering
6. **PDF + DOCX** document pool, ranking, limits and parsing
7. **Embedded-state route discovery** (links only) and framework detection
8. **Render and document caches**
9. **Schema v2.0** with `documents[]`, `images[]`, `coverage.evidence_available`

### V2 — "rich delivery and scale"

1. **Async job API** (`POST /v1/research/jobs` → `GET /v1/research/jobs/{id}`) with a Redis
   queue — *the prerequisite for everything else here*
2. **Screenshots** + the artifact service with signed expiring URLs
3. **`/v1/map` endpoint** exposed (internal-only until then)
4. **Renderer extracted into its own service** behind the existing adapter
5. **Postgres** for request history and per-domain analytics
6. Proxies **if and only if** the measured block rate justifies it

### V2+ — candidates, not commitments

Change tracking (the most compelling: "they launched a new service line" is a strong
outreach trigger), OCR for scanned PDFs (needs a model — would require a hard architectural
argument), multi-locale evidence, `Product`/`FAQPage` JSON-LD expansion.

### Recommended sequencing note

If forced to choose only two things from V1.5: **tier-3 interaction and PDF parsing**.
Interaction unlocks portfolio and case-study grids that are currently invisible; PDFs
unlock the case studies that were never HTML in the first place. Both hit the same
top-ranked research concept — client evidence — which is the material personalised
outreach is actually written from.

---

## 15. Revised build order

Same discipline as v1.1: the riskiest, most foundational component first, each stage
independently testable.

**V1.5**

1. `crawl/adequacy.py` — page-type adequacy including link-adequacy. Pure, no I/O,
   testable against static fixtures. Slots into the existing sufficiency call site.
2. `config/actions.yaml` + `fetch/action_planner.py` — the **pure planner**. No browser.
   Unit-tested against HTML fixtures for accordions, tabs, load-more, lazy grids, and
   against adversarial fixtures (a "Load more" that navigates off-domain, a button inside
   a form, a login link dressed as a disclosure).
3. `fetch/action_executor.py` — the small, dangerous part. Navigation guard, per-action
   timeouts, convergence rule, logging. Tested against live fixture sites with real
   Chromium.
4. `crawl/budget.py` — cost units and phase budgeting. Replaces the render counter.
5. `extract/images.py` — collection, layered filtering, cross-page frequency, scoring.
6. `fetch/documents.py` + `extract/pdf.py` + `extract/docx.py` — guarded download, MIME
   sniffing, zip-bomb and XXE defences, bounded parsing.
7. `crawl/routes.py` — embedded-state route discovery and framework detection.
8. `cache/render_cache.py`, `cache/document_cache.py`.
9. `models/response.py` v2.0 + the priority/drop budgeting in the orchestrator.
10. Regenerate the GPT Action schema; extend the GPT instructions for the new evidence types.

**New fixture sites required** (added to the existing eight):

- an accordion/tabs site where the real service detail is collapsed
- a "Load more" portfolio grid with 12 items behind 3 clicks
- an infinite-scroll case-study index
- a Next.js-style site with `__NEXT_DATA__` routes and no server-rendered anchors
- a site whose "Load more" button navigates off-domain (the adversarial case)
- a site linking three PDFs of varying research value, one of them a zip bomb
- an image-heavy portfolio page with a global logo on every page (frequency filter)
- a consent-overlay site that hides all content behind a modal

**V2**

11. `api/jobs.py` + queue + worker entrypoint (arq or RQ)
12. `artifacts/` store + signed URL service + sweeper
13. `fetch/screenshot.py` riding on the existing render
14. `/v1/map` endpoint
15. Renderer service extraction

---

## 16. Changes to the six invariants

Four are **extended in scope but unchanged in meaning**; two new ones are added for the
two genuinely new risks. The count goes from six to eight.

| # | Invariant | Status |
|---|---|---|
| 1 | **No model, no LLM, no external inference in the request path** | **Extended.** Now explicitly covers: no model decides what to click, no vision model interprets an image or screenshot, no model reads a document. Action planning, image filtering and document ranking are all pure functions over config. |
| 2 | **No evidence is returned that is not attributable to a URL** | **Extended and strengthened.** Was "no text without a URL"; now **every evidence object carries both `url` and `source_page_url`**. |
| 3 | **The deadline always wins** | **Extended.** Now also binds interactions, document parsing and screenshot capture — each individually cancellable, each behind a phase checkpoint. |
| 4 | **Nothing outside crawl scope is ever fetched** | **Extended.** Now also covers interaction-triggered navigation, document downloads, embedded-state-discovered routes and browser subresources. Every dynamically discovered destination passes through the same `UrlGuard`. |
| 5 | **The payload never exceeds the budget silently** | **Extended.** Now per-evidence-class, and strengthened by invariant 8. |
| 6 | **The crawler never rewrites, summarises or judges company content** | **Extended.** Now covers document text (no summarisation, no OCR) and images (no captioning, no classification of content). |
| **7** | **Every browser action comes from a closed, versioned allowlist, and no action target or script is ever derived from request input** | **NEW.** The interaction subsystem is the largest new capability and by far the largest new risk. This invariant is what keeps it from becoming an agent. Enforced by a signature-level test on `plan_actions` and a schema test asserting no request field can express an action, selector or script. |
| **8** | **No evidence is silently dropped** | **NEW.** If evidence was collected but not returned, or a phase was skipped, the response says so in `coverage.dropped_for_budget` or `crawl_summary.skipped_evidence`. With one evidence type, absence plausibly meant non-existence. With four, silence would make the GPT infer "they have no case studies" when the truth was "we ran out of budget" — which is precisely the class of false inference this system exists to prevent. |

### One tension worth naming

Invariant 6 says the crawler never judges company content. Tier-3 interaction and image
filtering both make *choices* about what to surface — which accordion to open, which image
is decorative. That is a judgement about **page structure**, not about **the company**, and
the line holds as long as every such choice is (a) made by a pure function, (b) driven by
config a human wrote, and (c) logged with a reason code. If a future feature ever needs to
decide *what the company is like* in order to decide what to fetch, that feature belongs in
the GPT, not here.

---

## 17. What is not changing

Restated because the risk in a document this size is that something load-bearing gets
quietly renegotiated:

- 45-second GPT Actions timeout and 100 000-character payload limit remain the binding
  constraints on the sync endpoint
- HTTP 200 for every crawl outcome, including failure
- Deterministic ranking from `lexicon.yaml`; no weight hard-coded in application code
- Category quotas and the diversity pass
- Crawl scope frozen once from the homepage's final URL
- robots.txt respected, behind configuration
- The full SSRF stack, unchanged
- No LLM, no embeddings, no search, no enrichment, no Scrapling
- The renderer stays behind the adapter interface
- The orchestrator stays framework-independent

