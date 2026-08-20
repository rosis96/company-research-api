"""Calibrate the crawler against a list of real company websites.

The test suite proves the logic is correct against fixture sites.  It cannot
prove the *thresholds* are right, because those depend on what real websites in
your market actually look like.  This script closes that gap: it runs your real
prospect list through a deployed API with debug enabled, then reports what the
crawler struggled with and which configuration values to change.

Usage:

    export CRA_BASE_URL=https://research.example.com
    export CRA_ADVANCED_KEY=<your advanced-scope key>

    python scripts/calibrate.py domains.txt
    python scripts/calibrate.py domains.txt --depth deep --concurrency 2
    python scripts/calibrate.py domains.txt --out reports/run-01

Produces:
    <out>.json  every raw response, for re-analysis without re-crawling
    <out>.md    the tuning report

Nothing here changes the crawler.  It reads, it counts, it suggests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_TIMEOUT = 90.0


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    domain: str
    ok: bool
    elapsed_s: float
    response: dict[str, Any] | None = None
    transport_error: str | None = None
    http_status: int | None = None


def read_domains(path: Path) -> list[str]:
    domains: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            domains.append(line)
    # De-duplicate, preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for domain in domains:
        key = domain.lower()
        if key not in seen:
            seen.add(key)
            out.append(domain)
    return out


async def research_one(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    domain: str,
    depth: str,
    force_refresh: bool,
) -> RunResult:
    payload = {
        "url": domain,
        "research_depth": depth,
        "force_refresh": force_refresh,
        "advanced": {"debug": True},
    }
    started = time.monotonic()
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/v1/research",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        return RunResult(domain, False, time.monotonic() - started, transport_error=str(exc))

    elapsed = time.monotonic() - started
    if response.status_code != 200:
        return RunResult(
            domain,
            False,
            elapsed,
            transport_error=response.text[:300],
            http_status=response.status_code,
        )
    return RunResult(domain, True, elapsed, response=response.json(), http_status=200)


async def run_all(
    domains: list[str],
    base_url: str,
    key: str,
    depth: str,
    concurrency: int,
    force_refresh: bool,
) -> list[RunResult]:
    semaphore = asyncio.Semaphore(concurrency)
    results: list[RunResult] = []

    async with httpx.AsyncClient() as client:

        async def worker(index: int, domain: str) -> None:
            async with semaphore:
                result = await research_one(
                    client, base_url, key, domain, depth, force_refresh
                )
                results.append(result)
                status = (
                    result.response.get("status", "?") if result.response else "TRANSPORT"
                )
                pages = (
                    result.response["crawl_summary"]["pages_scraped"]
                    if result.response
                    else 0
                )
                print(
                    f"[{index + 1:>3}/{len(domains)}] {domain[:46]:48s} "
                    f"{status:8s} pages={pages:<3} {result.elapsed_s:5.1f}s",
                    flush=True,
                )

        await asyncio.gather(*(worker(i, d) for i, d in enumerate(domains)))

    order = {d.lower(): i for i, d in enumerate(domains)}
    results.sort(key=lambda r: order.get(r.domain.lower(), 0))
    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@dataclass
class Analysis:
    total: int = 0
    transport_failures: list[RunResult] = field(default_factory=list)
    status_counts: Counter = field(default_factory=Counter)
    reason_counts: Counter = field(default_factory=Counter)
    reason_examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    stopped_reasons: Counter = field(default_factory=Counter)
    page_types: Counter = field(default_factory=Counter)
    missing_types: Counter = field(default_factory=Counter)
    elapsed: list[float] = field(default_factory=list)
    words: list[int] = field(default_factory=list)
    chars: list[int] = field(default_factory=list)
    pages_per_site: list[int] = field(default_factory=list)
    renders_per_site: list[int] = field(default_factory=list)
    depth_reached: Counter = field(default_factory=Counter)
    evidence_sufficient: int = 0
    js_sites: int = 0
    #: (domain, observed_words, threshold) for homepages that failed the word test.
    thin_gaps: list[tuple[str, int, int]] = field(default_factory=list)
    #: (domain, page_words, threshold, page_type) for pages dropped at assembly.
    dropped_pages: list[tuple[str, int, int, str]] = field(default_factory=list)
    #: Anchor/URL vocabulary the lexicon did not recognise.
    unmatched_anchors: Counter = field(default_factory=Counter)
    unmatched_examples: dict[str, str] = field(default_factory=dict)
    near_miss: list[tuple[str, float, str, str]] = field(default_factory=list)
    boilerplate_lengths: list[int] = field(default_factory=list)
    notes: Counter = field(default_factory=Counter)


def _parse_detail(detail: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in (detail or "").split():
        if "=" in token:
            k, _, v = token.partition("=")
            out[k] = v
    return out


def analyse(results: list[RunResult], min_score: int) -> Analysis:
    analysis = Analysis(total=len(results))

    for result in results:
        if not result.ok or result.response is None:
            analysis.transport_failures.append(result)
            continue

        body = result.response
        summary = body.get("crawl_summary", {})
        debug = body.get("debug") or {}

        analysis.status_counts[body.get("status", "?")] += 1
        analysis.elapsed.append(result.elapsed_s)
        analysis.words.append(summary.get("total_words", 0))
        analysis.chars.append(summary.get("content_chars", 0))
        analysis.pages_per_site.append(summary.get("pages_scraped", 0))
        analysis.renders_per_site.append(summary.get("browser_renders", 0))
        analysis.depth_reached[summary.get("max_depth_reached", 0)] += 1
        if summary.get("stopped_reason"):
            analysis.stopped_reasons[summary["stopped_reason"]] += 1
        if body.get("evidence_sufficient"):
            analysis.evidence_sufficient += 1
        for note in body.get("notes", []):
            analysis.notes[note] += 1
            if note == "js_site_mode":
                analysis.js_sites += 1

        if body.get("status_reason"):
            reason = body["status_reason"]
            analysis.reason_counts[reason] += 1
            if len(analysis.reason_examples[reason]) < 4:
                analysis.reason_examples[reason].append(result.domain)

        for page in body.get("pages", []):
            analysis.page_types[page.get("page_type", "?")] += 1
        for missing in body.get("coverage", {}).get("missing", []):
            analysis.missing_types[missing] += 1

        site_wide = body.get("site", {}).get("site_wide_content")
        if site_wide:
            analysis.boilerplate_lengths.append(len(site_wide))

        # Threshold gaps, from the detail strings the orchestrator records.
        for failure in debug.get("failures", []):
            detail = _parse_detail(failure.get("detail", ""))
            if failure.get("reason") == "too_thin" and "words" in detail:
                try:
                    observed = int(detail["words"])
                    threshold = int(detail.get("threshold", 0))
                except ValueError:
                    continue
                if "page_type" in detail:
                    analysis.dropped_pages.append(
                        (result.domain, observed, threshold, detail["page_type"])
                    )
                else:
                    analysis.thin_gaps.append((result.domain, observed, threshold))

        # Lexicon gaps: recurring candidates the lexicon could not classify, and
        # "near misses" -- classified correctly but scoring just under the bar.
        # The near-miss window is proportional (60-100% of the threshold) so that
        # genuinely irrelevant pages scoring near zero are not reported as
        # tuning candidates.
        for candidate in debug.get("unselected_candidates", []):
            if candidate.get("concept") is None:
                anchor = (candidate.get("anchor") or "").strip().lower()
                slug = candidate["url"].rstrip("/").rsplit("/", 1)[-1][:40].lower()
                key = anchor or slug
                if key and len(key) > 2:
                    analysis.unmatched_anchors[key] += 1
                    analysis.unmatched_examples.setdefault(key, candidate["url"])
            elif min_score * 0.6 <= candidate.get("score", 0) < min_score:
                analysis.near_miss.append(
                    (
                        result.domain,
                        candidate["score"],
                        candidate.get("page_type", "?"),
                        candidate["url"],
                    )
                )

    return analysis


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.0f}%" if whole else "n/a"


def _stat(values: list[float], fmt: str = "{:.0f}") -> str:
    if not values:
        return "n/a"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return (
        f"median {fmt.format(statistics.median(values))} · "
        f"p95 {fmt.format(p95)} · "
        f"max {fmt.format(max(values))}"
    )


def build_report(analysis: Analysis, config: dict[str, Any]) -> str:
    a = analysis
    crawled = a.total - len(a.transport_failures)
    lines: list[str] = []
    w = lines.append

    w("# Crawler calibration report")
    w("")
    w(f"- **Domains:** {a.total}")
    w(f"- **Depth:** `{config['depth']}` · **Endpoint:** `{config['base_url']}`")
    w(f"- **Generated:** {config['generated_at']}")
    w("")

    # --- 1. Outcomes -------------------------------------------------------
    w("## 1. Outcomes")
    w("")
    w("| Status | Count | Share |")
    w("|---|---|---|")
    for status in ("success", "partial", "failed"):
        count = a.status_counts.get(status, 0)
        w(f"| {status} | {count} | {_pct(count, crawled)} |")
    if a.transport_failures:
        w(f"| transport error | {len(a.transport_failures)} | {_pct(len(a.transport_failures), a.total)} |")
    w("")
    w(f"**Evidence sufficient:** {a.evidence_sufficient}/{crawled} ({_pct(a.evidence_sufficient, crawled)})")
    w("")
    w("This is the number that matters. It is the share of prospects the GPT can")
    w("actually write about. Below ~70% something is mistuned, not merely unlucky.")
    w("")

    # --- 2. Failures -------------------------------------------------------
    if a.reason_counts:
        w("## 2. Why sites failed")
        w("")
        w("| Reason | Count | Examples |")
        w("|---|---|---|")
        for reason, count in a.reason_counts.most_common():
            examples = ", ".join(a.reason_examples[reason][:3])
            w(f"| `{reason}` | {count} | {examples} |")
        w("")

    # --- 3. Threshold tuning ----------------------------------------------
    w("## 3. Threshold diagnostics")
    w("")
    if a.thin_gaps:
        gaps = sorted(a.thin_gaps, key=lambda g: -g[1])
        threshold = gaps[0][2]
        near = [g for g in gaps if g[1] >= threshold * 0.5]
        w(f"**Homepages that failed the word test:** {len(gaps)} (threshold {threshold})")
        w("")
        w("| Domain | Words seen | Short by |")
        w("|---|---|---|")
        for domain, observed, thr in gaps[:12]:
            w(f"| {domain} | {observed} | {thr - observed} |")
        w("")
        if near:
            suggested = max(60, min(g[1] for g in near) - 10)
            w(f"> **{len(near)} of {len(gaps)}** were within half the threshold — these are real")
            w("> sites with terse copy, not empty shells. Lowering")
            w(f"> `limits.yaml → sufficiency.min_words_homepage` from **{threshold}** to **{suggested}**")
            w("> would recover them. Re-run afterwards to confirm they yield usable text and not chrome.")
            w("")
    else:
        w("No homepages failed the word test. `min_words_homepage` is not the constraint.")
        w("")

    if a.dropped_pages:
        w(f"**Internal pages dropped as too thin:** {len(a.dropped_pages)}")
        w("")
        by_type = Counter(t for _, _, _, t in a.dropped_pages)
        w("| Page type | Dropped |")
        w("|---|---|")
        for page_type, count in by_type.most_common(8):
            w(f"| `{page_type}` | {count} |")
        w("")
        index_like = sum(
            c for t, c in by_type.items()
            if t in ("case_study_index", "portfolio", "products", "services")
        )
        if index_like >= 3:
            w("> Several of these are **index pages**, which are link hubs rather than prose.")
            w("> This is the case the V2 architecture calls *link-adequacy*: they are doing")
            w("> their job if they yield child links. Until that ships they are correctly")
            w("> used for hub expansion and then dropped — check `max_depth_reached` below")
            w("> to confirm depth 2 is still being reached on those sites.")
            w("")

    # --- 4. Deadline pressure ---------------------------------------------
    w("## 4. Deadline and budget pressure")
    w("")
    w(f"- **Elapsed (s):** {_stat(a.elapsed, '{:.1f}')}")
    w(f"- **Pages per site:** {_stat([float(p) for p in a.pages_per_site])}")
    w(f"- **Renders per site:** {_stat([float(r) for r in a.renders_per_site])}")
    w(f"- **Content chars:** {_stat([float(c) for c in a.chars])}")
    w("")
    if a.stopped_reasons:
        w("| Stopped because | Count |")
        w("|---|---|")
        for reason, count in a.stopped_reasons.most_common():
            w(f"| `{reason}` | {count} |")
        w("")
    deadline_hits = a.stopped_reasons.get("deadline", 0)
    if deadline_hits and deadline_hits / max(1, crawled) > 0.10:
        w(f"> **{_pct(deadline_hits, crawled)} of crawls hit the deadline.** Above 10% this is")
        w("> the signal that the async job API is overdue (V2 architecture §0). Short term,")
        w("> reduce `limits.yaml → presets.standard.max_pages`.")
        w("")
    slow = [e for e in a.elapsed if e > 35]
    if slow:
        w(f"> **{len(slow)} crawls exceeded 35s.** GPT Actions time out at 45s round trip,")
        w("> so these will surface to the user as a broken tool. Reduce the standard preset.")
        w("")

    # --- 5. Coverage -------------------------------------------------------
    w("## 5. What the crawler found")
    w("")
    w("| Page type | Sites with one |")
    w("|---|---|")
    for page_type, count in a.page_types.most_common(15):
        w(f"| `{page_type}` | {count} |")
    w("")
    case_studies = a.page_types.get("case_study", 0)
    if crawled and case_studies / crawled < 0.3:
        w(f"> **Case studies found on only {_pct(case_studies, crawled)} of sites.** This is the")
        w("> highest-value evidence type for personalisation. Before assuming these sites")
        w("> have none, check whether they are behind a *Load more* grid (V2 §4, tier-3")
        w("> interaction) or published only as PDFs (V2 §7) — those are exactly the two")
        w("> V1.5 features, and this number is how you decide they are worth building.")
        w("")
    w(f"- **JS-mode sites:** {a.js_sites}/{crawled} ({_pct(a.js_sites, crawled)})")
    w(f"- **Depth 2 reached:** {a.depth_reached.get(2, 0)}/{crawled}")
    w("")
    if crawled and a.js_sites / crawled > 0.25:
        w(f"> **{_pct(a.js_sites, crawled)} of sites needed a browser.** The renderer is")
        w("> load-bearing at this rate; keep it enabled and watch memory.")
        w("")

    # --- 6. Lexicon gaps ---------------------------------------------------
    w("## 6. Lexicon gaps")
    w("")
    if a.unmatched_anchors:
        w("Pages discovered repeatedly that the concept lexicon could not classify.")
        w("Recurring entries are vocabulary worth adding to `lexicon.yaml`.")
        w("")
        w("| Anchor / slug | Seen on N sites | Example |")
        w("|---|---|---|")
        for term, count in a.unmatched_anchors.most_common(20):
            if count < 2:
                continue
            w(f"| {term} | {count} | {a.unmatched_examples[term][:60]} |")
        w("")
        w("> Add a term to the closest existing concept rather than creating a new one,")
        w("> unless it needs its own quota. Bump `CRA_CONFIG_VERSION` afterwards.")
        w("")
    else:
        w("No recurring unclassified pages. The lexicon covers this sample.")
        w("")

    if a.near_miss:
        w("### Near-miss candidates")
        w("")
        w("Classified correctly but scored just below the selection threshold:")
        w("")
        w("| Score | Type | URL |")
        w("|---|---|---|")
        for _, score, page_type, url in sorted(a.near_miss, key=lambda n: -n[1])[:12]:
            w(f"| {score:.0f} | `{page_type}` | {url[:64]} |")
        w("")
        w("> If these look valuable, lower `presets.<depth>.min_score_select` or raise")
        w("> the relevant concept weight. Prefer the concept weight — it is more targeted.")
        w("")

    # --- 7. Boilerplate ----------------------------------------------------
    w("## 7. Boilerplate removal")
    w("")
    if a.boilerplate_lengths:
        w(f"- Sites where cross-page boilerplate was lifted: {len(a.boilerplate_lengths)}")
        w(f"- `site_wide_content` length: {_stat([float(b) for b in a.boilerplate_lengths])}")
        w("")
        w("> Spot-check two or three responses. If `site_wide_content` contains real")
        w("> positioning copy rather than CTAs and legal text, the 60% document-frequency")
        w("> threshold is too aggressive for small sites — raise")
        w("> `limits.yaml → boilerplate.document_frequency` toward 0.75.")
        w("")
    else:
        w("No boilerplate lifted — expected when most sites yielded fewer than 4 pages.")
        w("")

    # --- 8. Next actions ---------------------------------------------------
    w("## 8. Suggested next actions")
    w("")
    actions: list[str] = []
    if a.thin_gaps:
        actions.append(
            f"Lower `sufficiency.min_words_homepage` (currently {a.thin_gaps[0][2]}) — "
            f"{len(a.thin_gaps)} sites failed on it."
        )
    blocked = a.reason_counts.get("bot_blocked", 0)
    if blocked and blocked / max(1, crawled) > 0.08:
        actions.append(
            f"`bot_blocked` is {_pct(blocked, crawled)} — above the 8% threshold at which "
            "the architecture says to evaluate proxies."
        )
    if deadline_hits and deadline_hits / max(1, crawled) > 0.10:
        actions.append("Deadline pressure is high — reduce `max_pages` or plan the async job API.")
    if a.unmatched_anchors and max(a.unmatched_anchors.values()) >= 3:
        actions.append("Add the recurring unclassified terms in §6 to `lexicon.yaml`.")
    if crawled and case_studies / crawled < 0.3:
        actions.append(
            "Case-study discovery is low — this is the evidence for building tier-3 "
            "interaction and PDF parsing (V1.5)."
        )
    if not actions:
        actions.append("Nothing is obviously mistuned. Spot-check three responses by hand and ship.")

    for i, action in enumerate(actions, 1):
        w(f"{i}. {action}")
    w("")
    w("---")
    w("")
    w("After changing any YAML file, **bump `CRA_CONFIG_VERSION`** or the cache will serve")
    w("the previous results and it will look like nothing happened.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domains", type=Path, help="file with one domain per line")
    parser.add_argument("--base-url", default=os.environ.get("CRA_BASE_URL", ""))
    parser.add_argument("--key", default=os.environ.get("CRA_ADVANCED_KEY", ""))
    parser.add_argument("--depth", default="standard", choices=["quick", "standard", "deep"])
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--out", type=Path, default=Path("calibration"))
    parser.add_argument("--min-score", type=int, default=25)
    parser.add_argument("--no-refresh", action="store_true", help="allow cached results")
    parser.add_argument("--from-json", type=Path, help="re-analyse a previous run")
    args = parser.parse_args()

    if args.from_json:
        raw = json.loads(args.from_json.read_text(encoding="utf-8"))
        results = [
            RunResult(
                domain=item["domain"],
                ok=item["ok"],
                elapsed_s=item["elapsed_s"],
                response=item.get("response"),
                transport_error=item.get("transport_error"),
                http_status=item.get("http_status"),
            )
            for item in raw["results"]
        ]
        config = raw["config"]
    else:
        if not args.base_url or not args.key:
            print(
                "error: set CRA_BASE_URL and CRA_ADVANCED_KEY (or pass --base-url/--key)",
                file=sys.stderr,
            )
            return 2
        domains = read_domains(args.domains)
        if not domains:
            print("error: no domains found", file=sys.stderr)
            return 2

        print(f"Researching {len(domains)} domains at concurrency {args.concurrency}...\n")
        results = asyncio.run(
            run_all(
                domains,
                args.base_url,
                args.key,
                args.depth,
                args.concurrency,
                force_refresh=not args.no_refresh,
            )
        )
        config = {
            "base_url": args.base_url,
            "depth": args.depth,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.out.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {
                "config": config,
                "results": [
                    {
                        "domain": r.domain,
                        "ok": r.ok,
                        "elapsed_s": round(r.elapsed_s, 2),
                        "http_status": r.http_status,
                        "transport_error": r.transport_error,
                        "response": r.response,
                    }
                    for r in results
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = build_report(analyse(results, args.min_score), config)
    md_path = args.out.with_suffix(".md")
    md_path.write_text(report, encoding="utf-8")

    print(f"\nRaw responses : {json_path}")
    print(f"Tuning report : {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
