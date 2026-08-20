"""Tests for the calibration reporting logic.

The script is a developer tool, but its *conclusions* drive configuration
changes, so the aggregation and the thresholds that trigger advice are tested
against synthetic runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from calibrate import RunResult, analyse, build_report, read_domains  # noqa: E402

CONFIG = {"base_url": "https://x.test", "depth": "standard", "generated_at": "now"}


def response(
    *,
    status="success",
    reason=None,
    pages=None,
    words=800,
    chars=6000,
    scraped=5,
    renders=0,
    depth=2,
    stopped="candidates_exhausted",
    evidence=True,
    notes=None,
    failures=None,
    unselected=None,
    site_wide=None,
    missing=None,
):
    return {
        "status": status,
        "status_reason": reason,
        "evidence_sufficient": evidence,
        "site": {"site_wide_content": site_wide},
        "pages": [{"page_type": t} for t in (pages or ["homepage", "services"])],
        "coverage": {"found": [], "missing": missing or []},
        "crawl_summary": {
            "pages_scraped": scraped,
            "browser_renders": renders,
            "max_depth_reached": depth,
            "total_words": words,
            "content_chars": chars,
            "stopped_reason": stopped,
        },
        "notes": notes or [],
        "debug": {"failures": failures or [], "unselected_candidates": unselected or []},
    }


def run(domain, **kwargs):
    return RunResult(domain, True, kwargs.pop("elapsed", 12.0), response=response(**kwargs))


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def test_read_domains_strips_comments_blanks_and_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "d.txt"
    path.write_text(
        "# a comment\n"
        "acme.com\n"
        "\n"
        "  beta.com  # trailing comment\n"
        "ACME.com\n"
        "gamma.com\n",
        encoding="utf-8",
    )
    assert read_domains(path) == ["acme.com", "beta.com", "gamma.com"]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_status_and_evidence_counts() -> None:
    results = [
        run("a.com"),
        run("b.com", status="partial", evidence=False),
        run("c.com", status="failed", reason="bot_blocked", evidence=False),
    ]
    a = analyse(results, min_score=25)
    assert a.total == 3
    assert a.status_counts["success"] == 1
    assert a.status_counts["partial"] == 1
    assert a.status_counts["failed"] == 1
    assert a.evidence_sufficient == 1
    assert a.reason_counts["bot_blocked"] == 1
    assert "c.com" in a.reason_examples["bot_blocked"]


def test_transport_failures_are_separated() -> None:
    results = [run("a.com"), RunResult("b.com", False, 1.0, transport_error="boom")]
    a = analyse(results, min_score=25)
    assert len(a.transport_failures) == 1
    assert a.status_counts["success"] == 1


def test_thin_gaps_parsed_from_failure_detail() -> None:
    results = [
        run(
            "thin.com",
            status="failed",
            reason="too_thin",
            failures=[
                {
                    "reason": "too_thin",
                    "detail": "sufficiency=thin_dom words=150 threshold=200",
                }
            ],
        )
    ]
    a = analyse(results, min_score=25)
    assert a.thin_gaps == [("thin.com", 150, 200)]


def test_dropped_pages_distinguished_from_thin_homepages() -> None:
    results = [
        run(
            "a.com",
            failures=[
                {
                    "reason": "too_thin",
                    "detail": "words=12 threshold=40 page_type=case_study_index",
                }
            ],
        )
    ]
    a = analyse(results, min_score=25)
    assert a.thin_gaps == []
    assert a.dropped_pages == [("a.com", 12, 40, "case_study_index")]


def test_unmatched_anchors_collected_only_for_unclassified_candidates() -> None:
    unselected = [
        {"url": "https://a.com/our-approach", "score": 5, "concept": None, "anchor": "Our Approach"},
        {"url": "https://a.com/services", "score": 90, "concept": "services", "anchor": "Services"},
    ]
    a = analyse([run("a.com", unselected=unselected)], min_score=25)
    assert a.unmatched_anchors["our approach"] == 1
    assert "services" not in a.unmatched_anchors


def test_near_miss_candidates_are_classified_but_below_threshold() -> None:
    unselected = [
        {"url": "https://a.com/process", "score": 18, "concept": "process", "page_type": "process", "anchor": "Process"},
        {"url": "https://a.com/team", "score": 2, "concept": "team", "page_type": "team", "anchor": "Team"},
    ]
    a = analyse([run("a.com", unselected=unselected)], min_score=25)
    scores = [n[1] for n in a.near_miss]
    assert 18 in scores
    assert 2 not in scores  # outside the near-miss window


def test_js_mode_and_page_types_counted() -> None:
    results = [
        run("a.com", pages=["homepage", "case_study"], notes=["js_site_mode"]),
        run("b.com", pages=["homepage", "services"]),
    ]
    a = analyse(results, min_score=25)
    assert a.js_sites == 1
    assert a.page_types["case_study"] == 1
    assert a.page_types["homepage"] == 2


# ---------------------------------------------------------------------------
# Report advice
# ---------------------------------------------------------------------------


def test_report_renders_for_an_empty_but_valid_run() -> None:
    report = build_report(analyse([run("a.com")], 25), CONFIG)
    assert "# Crawler calibration report" in report
    assert "Evidence sufficient" in report


def test_report_suggests_lowering_the_word_threshold_on_near_misses() -> None:
    results = [
        run(
            f"site{i}.com",
            status="failed",
            reason="too_thin",
            failures=[{"reason": "too_thin", "detail": f"sufficiency=thin_dom words={150 + i} threshold=200"}],
        )
        for i in range(4)
    ]
    report = build_report(analyse(results, 25), CONFIG)
    assert "min_words_homepage" in report
    assert "within half the threshold" in report


def test_report_flags_proxy_threshold_when_block_rate_is_high() -> None:
    results = [run(f"s{i}.com", status="failed", reason="bot_blocked", evidence=False) for i in range(5)]
    results.append(run("ok.com"))
    report = build_report(analyse(results, 25), CONFIG)
    assert "evaluate proxies" in report


def test_report_flags_deadline_pressure() -> None:
    results = [run(f"s{i}.com", stopped="deadline", elapsed=31.0) for i in range(5)]
    report = build_report(analyse(results, 25), CONFIG)
    assert "async job API is overdue" in report


def test_report_flags_slow_crawls_against_the_gpt_timeout() -> None:
    report = build_report(analyse([run("slow.com", elapsed=41.0)], 25), CONFIG)
    assert "45s round trip" in report


def test_report_links_low_case_study_discovery_to_the_v1_5_decision() -> None:
    results = [run(f"s{i}.com", pages=["homepage", "services"]) for i in range(5)]
    report = build_report(analyse(results, 25), CONFIG)
    assert "Case studies found on only" in report
    assert "tier-3" in report


def test_report_does_not_flag_case_studies_when_discovery_is_healthy() -> None:
    results = [run(f"s{i}.com", pages=["homepage", "case_study"]) for i in range(5)]
    report = build_report(analyse(results, 25), CONFIG)
    assert "Case studies found on only" not in report


def test_report_recommends_lexicon_additions_for_recurring_terms() -> None:
    unselected = [{"url": "https://x.com/our-approach", "score": 5, "concept": None, "anchor": "Our Approach"}]
    results = [run(f"s{i}.com", unselected=unselected) for i in range(3)]
    report = build_report(analyse(results, 25), CONFIG)
    assert "lexicon.yaml" in report
    assert "our approach" in report


def test_report_always_reminds_about_config_version() -> None:
    report = build_report(analyse([run("a.com")], 25), CONFIG)
    assert "CRA_CONFIG_VERSION" in report


def test_report_handles_a_run_where_everything_failed() -> None:
    results = [
        RunResult(f"s{i}.com", False, 1.0, transport_error="connection refused") for i in range(3)
    ]
    report = build_report(analyse(results, 25), CONFIG)
    assert "transport error" in report
    assert "Suggested next actions" in report
