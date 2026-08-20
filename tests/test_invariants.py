"""The six invariants from SS17, asserted structurally.

These are the properties that must hold at every commit.  If a change breaks one
of these, the change is wrong, not the invariant.

1. No model, no LLM, no external inference in the request path.
2. No text is returned that is not attributable to a URL.
3. The deadline always wins.
4. Nothing outside crawl scope is ever fetched.
5. The payload never exceeds the budget silently.
6. The crawler never rewrites, summarizes, or judges company content.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest

from app.config.loader import get_limits
from app.config.settings import RobotsSettings
from app.crawl.orchestrator import ResearchOrchestrator
from app.crawl.robots import RobotsPolicy
from app.fetch.renderer import NullRenderer
from app.models.internal import Deadline
from app.models.request import AdvancedOptions, ResearchRequest
from app.models.response import ResearchResponse

APP_DIR = Path(__file__).resolve().parents[1] / "app"
PY_FILES = sorted(APP_DIR.rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# Invariant 1: no model, no LLM, no external inference in the request path
# ---------------------------------------------------------------------------

FORBIDDEN_PACKAGES = {
    "openai",
    "anthropic",
    "cohere",
    "litellm",
    "langchain",
    "llama_index",
    "transformers",
    "torch",
    "tensorflow",
    "sentence_transformers",
    "sklearn",
    "faiss",
    "chromadb",
    "pinecone",
    "qdrant_client",
    "weaviate",
    "tiktoken",
    "googlesearch",
    "serpapi",
    "scrapling",
    "clearbit",
    "apollo",
}


def test_no_llm_embedding_or_enrichment_dependency() -> None:
    offenders: list[str] = []
    for path in PY_FILES:
        for module in imported_modules(path) & FORBIDDEN_PACKAGES:
            offenders.append(f"{path.relative_to(APP_DIR)} imports {module}")
    assert not offenders, offenders


def test_requirements_contain_no_forbidden_packages() -> None:
    requirements = (APP_DIR.parent / "requirements.txt").read_text().lower()
    for package in FORBIDDEN_PACKAGES:
        assert package.replace("_", "-") not in requirements, package


def test_ranking_is_pure_and_reproducible() -> None:
    """The same inputs must always produce the same score, with no I/O."""
    from app.crawl.rank import RankContext, score_candidate
    from app.models.internal import Candidate, LinkRegion

    context = RankContext.build("acme.com", "https://acme.com/", None)
    scores = set()
    for _ in range(25):
        candidate = Candidate(
            url="https://acme.com/case-studies/boeing",
            anchor_texts=["Boeing case study"],
            regions={LinkRegion.MAIN},
            in_sitemap=True,
        )
        score_candidate(candidate, context)
        scores.add(candidate.score)
    assert len(scores) == 1


def test_ranking_module_performs_no_network_io() -> None:
    source = (APP_DIR / "crawl" / "rank.py").read_text()
    for banned in ("httpx", "requests", "urlopen", "socket", "aiohttp"):
        assert banned not in source, banned


# ---------------------------------------------------------------------------
# Invariant 2: no text is returned that is not attributable to a URL
# ---------------------------------------------------------------------------


def test_only_page_objects_and_site_wide_content_carry_text() -> None:
    """The response model itself is the guarantee."""
    text_fields = {
        name
        for name, field in ResearchResponse.model_fields.items()
        if field.annotation is str
    }
    assert text_fields <= {"schema_version", "request_id"}

    from app.models.response import PageResult, SiteInfo

    assert "content" in PageResult.model_fields
    assert "url" in PageResult.model_fields
    assert PageResult.model_fields["url"].is_required()

    # site_wide_content is the single explicitly cross-page field.
    site_text = {
        name
        for name, field in SiteInfo.model_fields.items()
        if "str" in str(field.annotation) and name not in {"requested_url", "final_url"}
    }
    assert "site_wide_content" in site_text


async def test_every_returned_page_has_a_url(orchestrator, fixture_servers) -> None:
    request = ResearchRequest(url=fixture_servers["static"].url("/"))
    result = await orchestrator.run(request, request_id="req_test")
    for page in result.pages:
        assert page.url and page.url.startswith("http")
        assert page.content


async def test_page_content_is_traceable_to_its_source_page(
    orchestrator, fixture_servers
) -> None:
    """Content from one page must never appear under another page's URL."""
    request = ResearchRequest(url=fixture_servers["depth2"].url("/"))
    result = await orchestrator.run(request, request_id="req_test")

    case_studies = [p for p in result.pages if p.page_type == "case_study"]
    assert len(case_studies) >= 2
    for page in case_studies:
        client = page.url.rsplit("/", 1)[-1].split("-")[0]
        assert client.lower() in page.content.lower(), page.url


# ---------------------------------------------------------------------------
# Invariant 3: the deadline always wins
# ---------------------------------------------------------------------------


def test_deadline_arithmetic() -> None:
    deadline = Deadline(total_ms=1000)
    assert deadline.remaining_ms <= 1000
    assert deadline.has(500)
    assert not deadline.expired
    assert deadline.budget_for(10.0) <= 1.0
    assert deadline.budget_for(10.0, reserve_ms=2000) == 0.0

    expired = Deadline(total_ms=0)
    assert expired.expired
    assert expired.remaining_ms == 0
    assert expired.budget_for(5.0) == 0.0


async def test_orchestrator_respects_the_deadline_on_a_slow_site(
    guard, fetcher, fixture_servers
) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(deadline_ms=4000)
    request = ResearchRequest(url=fixture_servers["slow"].url("/"))

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await orchestrator.run(request, request_id="req_test", budget=budget)
    elapsed_ms = (loop.time() - started) * 1000

    assert elapsed_ms < 10000, elapsed_ms
    assert isinstance(result, ResearchResponse)  # always a valid response


async def test_every_deadline_stop_still_returns_a_valid_response(
    guard, fetcher, fixture_servers
) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    for deadline_ms in (1000, 2000, 3500, 5000):
        budget = get_limits().budget("standard").with_overrides(deadline_ms=deadline_ms)
        request = ResearchRequest(url=fixture_servers["slow"].url("/"))
        result = await orchestrator.run(request, request_id="req_test", budget=budget)
        ResearchResponse.model_validate(result.model_dump())
        assert result.status in ("success", "partial", "failed")


def test_no_unbounded_sleeps_or_retry_loops_in_the_crawler() -> None:
    """SS5: no endless retrying of difficult websites."""
    for name in ("crawl/orchestrator.py", "fetch/http.py", "fetch/renderer.py"):
        source = (APP_DIR / name).read_text()
        assert "while True" not in source, name
        assert "time.sleep" not in source, name


# ---------------------------------------------------------------------------
# Invariant 4: nothing outside crawl scope is ever fetched
# ---------------------------------------------------------------------------


def test_no_module_bypasses_the_guard_with_a_raw_http_client() -> None:
    """Only the fetch layer may construct an HTTP client."""
    allowed = {"fetch/http.py", "security/transport.py"}
    pattern = re.compile(r"httpx\.AsyncClient|requests\.(get|post)|urlopen|aiohttp\.")
    for path in PY_FILES:
        relative = str(path.relative_to(APP_DIR))
        if relative in allowed:
            continue
        assert not pattern.search(path.read_text()), relative


def test_http_client_never_follows_redirects_automatically() -> None:
    source = (APP_DIR / "fetch" / "http.py").read_text()
    assert "follow_redirects=False" in source
    assert "follow_redirects=True" not in source


def test_tldextract_makes_no_network_calls() -> None:
    """A PSL fetch at request time would be an unguarded outbound request."""
    source = (APP_DIR / "security" / "domains.py").read_text()
    assert "suffix_list_urls=()" in source


async def test_off_domain_links_never_enter_the_candidate_pool(
    orchestrator, fixture_servers
) -> None:
    request = ResearchRequest(
        url=fixture_servers["static"].url("/"), advanced=AdvancedOptions(debug=True)
    )
    result = await orchestrator.run(request, request_id="req_test")
    for page in result.pages:
        assert "staticsite.com" in page.url


async def test_third_party_hosts_are_never_contacted(
    orchestrator, fixture_servers
) -> None:
    others = {n: s for n, s in fixture_servers.items() if n != "leaky"}
    for server in others.values():
        server.clear_requests()

    request = ResearchRequest(url=fixture_servers["leaky"].url("/"))
    await orchestrator.run(request, request_id="req_test")

    for name, server in others.items():
        assert server.requests == [], f"{name} was contacted"


# ---------------------------------------------------------------------------
# Invariant 5: the payload never exceeds the budget silently
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("global_budget", [2000, 5000, 12000])
async def test_content_budget_is_enforced_and_reported(
    guard, fetcher, fixture_servers, global_budget: int
) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = (
        get_limits().budget("standard").with_overrides(global_content_budget=global_budget)
    )
    request = ResearchRequest(url=fixture_servers["static"].url("/"))
    result = await orchestrator.run(request, request_id="req_test", budget=budget)

    total = sum(len(page.content) for page in result.pages)
    assert total <= global_budget
    assert result.crawl_summary.content_chars == total
    if total >= global_budget * 0.9:
        assert any(page.truncated for page in result.pages)


async def test_truncation_is_always_flagged(guard, fetcher, fixture_servers) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = (
        get_limits()
        .budget("standard")
        .with_overrides(page_content_cap_top=300, page_content_cap_rest=300)
    )
    request = ResearchRequest(url=fixture_servers["static"].url("/"))
    result = await orchestrator.run(request, request_id="req_test", budget=budget)

    for page in result.pages:
        assert len(page.content) <= 300
        assert page.truncated is True


async def test_response_fits_the_gpt_actions_payload_limit(
    orchestrator, fixture_servers
) -> None:
    hard_limit = get_limits().response["hard_payload_chars"]
    for name in ("static", "depth2", "locale", "thin"):
        request = ResearchRequest(url=fixture_servers[name].url("/"))
        result = await orchestrator.run(request, request_id="req_test")
        assert len(result.model_dump_json(exclude_none=True)) < hard_limit


def test_default_budgets_cannot_overflow_the_payload_limit() -> None:
    """Configured budgets must be internally consistent, not just individually sane."""
    limits = get_limits()
    hard_limit = limits.response["hard_payload_chars"]
    for name, budget in limits.presets.items():
        worst_case_content = budget.global_content_budget
        metadata_allowance = budget.max_pages * 400 + 3000
        assert worst_case_content + metadata_allowance < hard_limit, name


def test_default_deadlines_fit_the_gpt_actions_timeout() -> None:
    """45 seconds round trip, with margin for network and serialisation."""
    for name, budget in get_limits().presets.items():
        assert budget.deadline_ms <= 40000, name
        assert budget.total_timeout_s * 1000 < budget.deadline_ms, name


# ---------------------------------------------------------------------------
# Invariant 6: the crawler never rewrites, summarizes or judges content
# ---------------------------------------------------------------------------


async def test_returned_content_is_a_subset_of_the_source_page(
    orchestrator, fixture_servers
) -> None:
    """Every alphabetic token returned must occur in the fetched page."""
    import httpx

    from app.fetch.sufficiency import visible_text

    request = ResearchRequest(url=fixture_servers["static"].url("/"))
    result = await orchestrator.run(request, request_id="req_test")

    page = result.pages[0]
    async with httpx.AsyncClient(follow_redirects=False) as client:
        raw = await client.get(
            page.url.replace("staticsite.com", "127.0.0.1"),
            headers={"Host": "staticsite.com"},
        )
    source = re.sub(r"[^a-z0-9]+", " ", visible_text(raw.text).lower())
    source_tokens = set(source.split())

    returned = re.sub(r"[^a-z0-9]+", " ", page.content.lower()).split()
    unknown = [token for token in returned if token not in source_tokens]
    assert not unknown, unknown[:10]


def _executable_source(path: Path) -> str:
    """Return the module's code with docstrings removed.

    Scanning raw text is useless here: the docstrings *describing* invariant 6
    necessarily contain the very words the check looks for.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)


def test_extraction_modules_contain_no_generation_code() -> None:
    """No calls to anything that could produce text the source page did not contain."""
    for name in ("content.py", "metadata.py", "jsonld.py", "boilerplate.py"):
        code = _executable_source(APP_DIR / "extract" / name).lower()
        for banned in ("summar", "rewrite", "paraphras", "openai", "anthropic", "prompt", "completion"):
            assert banned not in code, f"{name}: {banned}"


def test_page_type_is_a_closed_enum_of_page_shapes_not_judgements() -> None:
    """page_type labels the page, never the company."""
    from typing import get_args

    from app.models.response import PageType

    types = set(get_args(PageType))
    for judgement in ("good_fit", "icp_match", "high_value", "quality", "score"):
        assert judgement not in types


def test_no_company_scoring_fields_exist_in_the_response() -> None:
    from app.models.response import SiteInfo

    for banned in ("industry", "icp", "fit_score", "segment", "classification", "summary"):
        assert banned not in SiteInfo.model_fields


def test_orchestrator_is_framework_independent() -> None:
    """SS14: adding the async job API must be a wiring change, not a rewrite."""
    modules = imported_modules(APP_DIR / "crawl" / "orchestrator.py")
    for framework in ("fastapi", "starlette", "uvicorn", "flask", "django"):
        assert framework not in modules


def test_ranking_weights_are_not_hardcoded_in_the_ranker() -> None:
    """SS7: weights, penalties and quotas live in YAML, not in code.

    Rather than banning every numeric literal (which would flag calendar
    arithmetic and array indices), this asserts that no *configured* weight,
    penalty or quota value appears as a literal in the ranking module -- which is
    exactly what inlining a tunable would look like.
    """
    from app.config.loader import get_lexicon

    lexicon = get_lexicon()
    configured: set[float] = {float(concept.weight) for concept in lexicon.concepts}
    configured |= {
        float(value)
        for value in lexicon.structural.values()
        if isinstance(value, (int, float))
    }
    configured |= {
        float(value)
        for value in lexicon.penalties.values()
        if isinstance(value, (int, float))
    }
    # Small values are ambiguous with ordinary indices and arities.
    configured = {value for value in configured if abs(value) > 5}

    tree = ast.parse((APP_DIR / "crawl" / "rank.py").read_text(encoding="utf-8"))
    literals = {
        float(node.value)
        for node in ast.walk(tree)
        if isinstance(node.__class__ and node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    }
    inlined = literals & configured
    assert not inlined, f"configured tunables inlined in rank.py: {sorted(inlined)}"


def test_lexicon_and_limits_are_loaded_from_yaml() -> None:
    source = (APP_DIR / "crawl" / "rank.py").read_text()
    assert "structural = lexicon.structural" in source
    assert "penalties = lexicon.penalties" in source
    assert 'structural["' in source
    assert 'penalties["' in source
    assert "lexicon.quotas" in source

    orchestrator_source = (APP_DIR / "crawl" / "orchestrator.py").read_text()
    assert "self.limits." in orchestrator_source
    assert "state.budget." in orchestrator_source
