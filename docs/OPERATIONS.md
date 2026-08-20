# Operations Guide — getting the API live and connected to a GPT

Companion to the architecture documents. Covers hosting on Railway, git setup, API-key
management, the GPT Action wiring, and the privacy policy you need to publish a GPT.

> Nothing here is legal advice. The privacy policy in §6 is a starting template to take to
> whoever advises you.

---

## 1. Hosting: Railway vs VPS

You have both available. They are genuinely different topologies, and Railway is simpler.

| | **Railway** | **VPS + Docker + Caddy** |
|---|---|---|
| TLS + domain | Provided automatically | You run Caddy; Let's Encrypt |
| Redis | Managed service, one click | You run the container |
| Deploys | Push to git | `git pull && docker compose up -d` |
| Scaling | Change plan / replicas | Resize the box |
| Persistent disk | Volumes (must be attached explicitly) | Native |
| Cost at your volume | Usage-based | ~€5–20/month flat |
| Egress firewall (SS12 layer 2) | **Not available** — you cannot set iptables rules | **Available** — the documented `DOCKER-USER` rules |

**Recommendation: start on Railway.** It removes Caddy, certificate renewal and Redis
operations, and 20 000 crawls/month is well inside its comfort zone.

**But understand the one real trade-off:** on Railway you cannot install the egress
firewall rules from README §4. The application-level SSRF guard — pinned DNS, per-hop
redirect revalidation, IP-range blocking — is fully intact and is the primary control. What
you lose is the second layer, which mattered most for *Chromium's* subresource DNS, since
the browser cannot use the pinned transport.

Three ways to close that gap on Railway, in order of preference:

1. **Set `CRA_RENDERER__HOST_RESOLVER_RULES`** so Chromium resolves only through a mapping
   you control (costs a browser launch per render — acceptable at your volume).
2. **Run with `CRA_RENDERER__ENABLED=false` initially.** JS-heavy sites fail honestly with
   `too_thin`; measure how often that happens before deciding rendering is worth it.
3. **Move to the VPS** if and when the browser tier becomes load-bearing.

This is a real decision, not a formality — write down which one you chose.

### 1.1 Railway deployment

Railway does **not** execute `docker-compose.yml`. Each compose service becomes a separate
Railway service, so the topology changes:

```
docker-compose.yml              Railway
├── caddy      ────────────►    (deleted — Railway terminates TLS)
├── api        ────────────►    Service "api", built from ./Dockerfile
└── redis      ────────────►    Railway managed Redis
```

**Steps:**

1. Push the repo to GitHub (§2).
2. Railway → **New Project** → **Deploy from GitHub repo** → pick the repo.
   It detects the `Dockerfile` automatically.
3. Add Redis: **New** → **Database** → **Redis**.
4. In the `api` service → **Variables**, set:

   ```
   CRA_API_KEYS          = <generated>:standard,<generated>:advanced
   CRA_USER_AGENT        = CompanyResearchBot/1.0 (+https://<your-domain>/bot)
   CRA_ENVIRONMENT       = production
   CRA_CONFIG_VERSION    = 1
   CRA_CACHE__REDIS_URL  = ${{Redis.REDIS_URL}}      # reference variable, stays in sync
   PORT                  = 8000
   ```

   `${{Redis.REDIS_URL}}` is Railway's reference-variable syntax — it resolves to the
   private `redis.railway.internal` address and updates itself if the database is recreated.

5. **Networking** → **Generate Domain**. You get `something.up.railway.app` with valid TLS
   on 443 — which is exactly what GPT Actions require. Add a custom domain later if you
   want a branded URL.
6. **Settings → Deploy**: set the health check path to `/v1/health`.
7. Memory: give the service at least **2 GB**, and 4 GB if the renderer is enabled.
   Chromium is the constraint, not FastAPI.

**Railway-specific gotchas:**

- The filesystem is ephemeral. Fine for V1 and V1.5; **screenshots (V2) will need a
  Railway Volume** mounted at the artifact path, or S3-compatible storage.
- `--workers 2` in the Dockerfile `CMD` assumes ~4 GB. On a 1 GB plan, drop to 1 worker.
- Railway injects `PORT`; uvicorn is already bound to `0.0.0.0:8000`, so either set
  `PORT=8000` as above or change the `CMD` to honour `$PORT`.

### 1.2 VPS alternative

Unchanged from README §"Production deployment" — including the `DOCKER-USER` iptables
rules, which is the reason to choose it.

---

## 2. Git

```bash
cd /path/to/company-research-api
git init
git add .
git commit -m "V1: company website research API"
git branch -M main
git remote add origin git@github.com:<you>/company-research-api.git
git push -u origin main
```

**Before the first push, confirm `.env` is not staged.** It is in `.gitignore`, but check:

```bash
git ls-files | grep -c '^\.env$'    # must print 0
```

If a key ever lands in git history, rotate it — do not just delete the file. Anything
pushed to a remote should be treated as public from that moment.

Commit `.env.example` (no secrets), never `.env`.

---

## 3. API keys

**Generate:**

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Make two, one per scope:

```
CRA_API_KEYS=<key1>:standard,<key2>:advanced
```

- **`standard`** — what the GPT Action uses. Cannot pass `advanced` options or `debug`.
- **`advanced`** — your own tooling. Unlocks the debug block and the numeric crawl
  overrides deliberately hidden from the model.

Keys are never stored in plaintext at rest in the app — they are SHA-256 hashed at load and
compared in constant time. They *are* in your environment variables, so treat the Railway
Variables panel as a secret store.

**Rotation:** add the new key alongside the old one, update the GPT Action, confirm
traffic has moved (the logs carry a `key_id` prefix per request), then remove the old one.
No downtime.

**If a key leaks:** remove it from `CRA_API_KEYS` and redeploy. That is the whole
revocation mechanism — deliberately simple. Anyone with a key can make your server fetch
arbitrary public URLs, which is why rate limits and a monthly quota exist.

---

## 4. Connecting the GPT Action

```bash
python scripts/export_openapi.py https://<your-domain> > openapi.gpt-action.json
```

In ChatGPT → **Create a GPT** → **Configure** → **Create new action**:

| Field | Value |
|---|---|
| Schema | paste `openapi.gpt-action.json` |
| Authentication | **API Key** |
| Auth Type | **Bearer** — *not* a custom header; GPT Actions do not support them |
| API Key | your `standard`-scope key |
| Privacy policy | the URL from §6 (required to publish publicly) |

Then paste the GPT instructions from README §"Connecting the GPT Action". Those
instructions are not optional garnish — they are the half of the failure model that stops
the GPT falling back on training knowledge when the crawl fails.

**Test with a site you know**, then with a site you know is JavaScript-heavy, then with a
domain that does not exist. All three should behave sensibly; the third should return
`status: "failed"` and the GPT should say `N/A` rather than inventing a company.

---

## 5. What to watch after launch

The metrics that decide what to build next (all already in the structured logs):

| Metric | Why it matters | Action threshold |
|---|---|---|
| `bot_blocked` rate | Sites refusing the crawler | > 8 % over a week → evaluate proxies |
| `too_thin` rate | JS sites failing without a renderer | > 15 % → the browser tier is worth its cost |
| `browser_renders` per request | Cost driver | > 30 % of pages → extract the renderer service |
| `stopped_reason: deadline` rate | Deadline pressure | > 10 % → the async job API is overdue |
| `cache_hit` rate | Cost saving | < 15 % → check `CONFIG_VERSION` churn |
| p95 `elapsed_ms` | Timeout risk | > 35 s → reduce `standard` budgets |

Do not add proxies, stealth rendering or extra tiers on intuition. Each of these has a
number attached; wait for the number.

---

## 6. Privacy policy

OpenAI requires a privacy policy URL to publish a GPT with Actions publicly. It must be a
real, reachable page.

### What yours actually needs to cover

This system has an unusually clean privacy story, and the policy should say so plainly
rather than hiding behind boilerplate:

- The Action receives **a company website URL**, nothing else. No personal data by design.
- The API fetches **publicly accessible pages** from that website.
- It stores **cached page content** (up to 14 days) and **request logs** (domain, status,
  timings — never page content).
- It does not sell, share or enrich data, and it contacts no third-party data provider.

Serve it at a stable URL (e.g. `https://<your-domain>/privacy`) — a static page or a
GitHub Pages file is fine.

### Template

```markdown
# Privacy Policy — Company Website Research API

Last updated: <DATE>

## Who we are
This service is operated by <LEGAL ENTITY>, <ADDRESS>. Contact: <EMAIL>.

## What this service does
The Company Website Research API accepts a single company website URL and retrieves
publicly available pages from that website. It returns the text of those pages so that
research and written summaries can be based on the company's own published information.

## What data we receive
When the service is used through a Custom GPT Action, we receive:
- the company website URL submitted in the request
- the API key identifying the calling application
- standard request metadata (timestamp, IP address of the calling service)

We do not request, require or intentionally collect personal data. We do not receive the
content of your conversation.

## What data we store
- **Request logs** — the domain requested, the outcome, timing and error codes. Retained
  for <N> days. Logs do not contain retrieved page content.
- **Cached research results** — cleaned text extracted from the public pages of the
  website that was researched. Retained for up to 14 days, then deleted automatically.
- We do not store credentials, payment data, or special-category personal data.

## Content retrieved from third-party websites
The service retrieves publicly accessible pages from the website whose URL is submitted.
It respects `robots.txt`, identifies itself with a descriptive User-Agent, and does not
attempt to access password-protected, private or paywalled areas. If a page happens to
contain personal data (for example a staff biography on a public "About" page), that data
is processed only to return the page text to the requesting application, and is deleted
when the cache expires.

## Legal basis (where UK/EU GDPR applies)
Where any personal data is processed, we rely on legitimate interests — providing a
research tool that summarises publicly published business information — balanced against
the limited, public and business-context nature of the data.

## Sharing
We do not sell or share data with third parties. Data is processed by our hosting provider
(<Railway / your VPS provider>) solely to operate the service.

## Your rights
If you believe we hold personal data about you, contact <EMAIL> to request access,
correction or deletion. Because retained data expires within <N> days, most requests are
satisfied by the automatic deletion schedule.

## Website owners
If you operate a website and do not want this service to retrieve your pages, add a
`Disallow` rule for the user agent `CompanyResearchBot` in your `robots.txt`; the service
honours it. You may also contact <EMAIL> to request exclusion.

## Changes
We will update this page and the "last updated" date if this policy changes.
```

The "Website owners" section is not required by anyone, and I would include it anyway. It
costs one paragraph, it is the honest counterpart to identifying your bot truthfully in the
User-Agent, and it is the thing that keeps a crawler operating at 20 000 sites/month on the
defensible side of the line.

### Also worth having

- **A bot info page** at the URL in your User-Agent (`/bot`) — one paragraph saying what
  the crawler is, that it respects robots.txt, and how to contact you. Site owners who see
  your UA in their logs will look for exactly this.
- **Terms of service** for your own API if you ever expose it beyond your own GPT.
