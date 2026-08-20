"""Tests for the GPT-Action OpenAPI document (SS9).

The GPT Actions limits are hard: 300 characters for an endpoint description, 700
for a parameter description. Exceeding them breaks the import.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from export_openapi import (  # noqa: E402
    MAX_ENDPOINT_DESCRIPTION,
    MAX_PARAMETER_DESCRIPTION,
    build,
)


@pytest.fixture(scope="module")
def spec() -> dict:
    return build("https://research.example.com")


@pytest.fixture(scope="module")
def batch_spec() -> dict:
    return build("https://research.example.com", include_batch=True)


def test_one_action_is_exposed_by_default(spec: dict) -> None:
    """One synchronous call per company -- the shape WebPilot uses.

    Handing a GPT the job endpoints asks it to run a state machine across
    several turns: create, remember an id, poll, paginate. Observed behaviour was
    that it abandons the machine on the first error and fabricates "research
    unavailable" rows for the whole list. One call per company has no state to
    lose, so a failure costs exactly one company.
    """
    assert set(spec["paths"]) == {"/v1/research"}
    ids = [op["operationId"] for path in spec["paths"].values() for op in path.values()]
    assert ids == ["research"]


def test_the_batch_actions_are_available_behind_a_flag(batch_spec: dict) -> None:
    assert set(batch_spec["paths"]) == {
        "/v1/research",
        "/v1/research/jobs",
        "/v1/research/jobs/{job_id}",
        "/v1/research/jobs/{job_id}/results",
    }
    ids = sorted(
        op["operationId"] for path in batch_spec["paths"].values() for op in path.values()
    )
    assert ids == [
        "createResearchJob",
        "getResearchJob",
        "getResearchResults",
        "research",
    ]


def test_job_schemas_are_dropped_when_the_job_actions_are(spec: dict) -> None:
    """A schema for an action that is not offered is an invitation to try it."""
    for orphan in ("JobCreateRequest", "JobCreated", "JobProgress", "JobResultsPage"):
        assert orphan not in spec["components"]["schemas"], orphan
    assert "job_id" not in json.dumps(spec)


def test_internal_routes_are_not_exposed(spec: dict) -> None:
    for hidden in (
        "/v1/health",
        "/privacy",
        "/bot",
        "/v1/diagnostics/redis",
        "/v1/diagnostics/jobs/{job_id}",
    ):
        assert hidden not in spec["paths"]


def test_diagnostics_are_invisible_to_the_gpt(spec: dict) -> None:
    """Operator endpoints must never become actions the model can call."""
    assert "diagnostics" not in json.dumps(spec)


def test_server_url_is_pinned(spec: dict) -> None:
    assert spec["servers"] == [{"url": "https://research.example.com"}]


def test_trailing_slash_is_trimmed() -> None:
    assert build("https://x.example.com/")["servers"][0]["url"] == "https://x.example.com"


def test_bearer_auth_is_declared(spec: dict) -> None:
    """GPT Actions do not support custom headers, so bearer is the only option."""
    assert spec["components"]["securitySchemes"] == {
        "bearerAuth": {"type": "http", "scheme": "bearer"}
    }
    assert spec["security"] == [{"bearerAuth": []}]


def test_advanced_options_are_hidden_from_the_model(spec: dict) -> None:
    properties = spec["components"]["schemas"]["ResearchRequest"]["properties"]
    assert "advanced" not in properties
    assert not any("Advanced" in name for name in spec["components"]["schemas"])
    assert "AdvancedOptions" not in json.dumps(spec)


def test_no_numeric_crawl_knobs_are_exposed(spec: dict) -> None:
    properties = spec["components"]["schemas"]["ResearchRequest"]["properties"]
    assert set(properties) == {"url", "research_depth", "focus", "force_refresh"}


def test_endpoint_description_within_the_limit(spec: dict) -> None:
    description = spec["paths"]["/v1/research"]["post"]["description"]
    assert 0 < len(description) <= MAX_ENDPOINT_DESCRIPTION
    assert "never invents content" in description


def test_parameter_descriptions_within_the_limit(spec: dict) -> None:
    for schema in spec["components"]["schemas"].values():
        for prop in schema.get("properties", {}).values():
            if isinstance(prop, dict) and "description" in prop:
                assert len(prop["description"]) <= MAX_PARAMETER_DESCRIPTION


def test_no_dangling_references(spec: dict) -> None:
    defined = set(spec["components"]["schemas"])
    referenced = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                referenced.add(ref.rsplit("/", 1)[-1])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(spec)
    assert referenced <= defined, referenced - defined


def test_response_schema_is_described(spec: dict) -> None:
    responses = spec["paths"]["/v1/research"]["post"]["responses"]
    assert "200" in responses
    assert "401" in responses
    assert "429" in responses


def test_committed_schema_matches_the_generator() -> None:
    """The checked-in file must not drift from the code."""
    committed = json.loads((ROOT / "openapi.gpt-action.json").read_text())
    regenerated = build(committed["servers"][0]["url"])
    assert committed == regenerated, "run: python scripts/export_openapi.py <url> > openapi.gpt-action.json"


def test_schema_is_small_enough_to_import(spec: dict) -> None:
    assert len(json.dumps(spec)) < 100_000


# ---------------------------------------------------------------------------
# GPT Actions import cleanliness
# ---------------------------------------------------------------------------


def test_no_auth_header_parameter_is_declared(spec: dict) -> None:
    """ChatGPT warns 'parameter authorization has location header; ignoring'.

    FastAPI documents the header because the auth dependency reads it, but GPT
    Actions supply it themselves from the configured API key. Authentication is
    declared via securitySchemes instead.
    """
    for operations in spec["paths"].values():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                assert parameter.get("in") != "header", parameter


def test_no_header_parameters_anywhere(spec: dict) -> None:
    assert '"in": "header"' not in json.dumps(spec, indent=1)


def test_operation_id_is_short_and_readable(spec: dict) -> None:
    """The operationId becomes the action name the GPT instructions refer to."""
    assert spec["paths"]["/v1/research"]["post"]["operationId"] == "research"


def test_bearer_security_is_the_only_auth_declaration(spec: dict) -> None:
    assert spec["security"] == [{"bearerAuth": []}]
    assert list(spec["components"]["securitySchemes"]) == ["bearerAuth"]


def test_app_auth_behaviour_is_unchanged_by_the_export() -> None:
    """The exporter must not touch how the running API authenticates."""
    import inspect

    from app.api import auth

    source = inspect.getsource(auth)
    assert "Authorization" in source          # still read from the header
    assert "bearer" in source.lower()         # still the bearer scheme
    assert "compare_digest" in source         # still constant-time


# ---------------------------------------------------------------------------
# Model-facing document quality
# ---------------------------------------------------------------------------
#
# The schema is not documentation, it is prompt. Everything below is about the
# GPT actually calling the action, and calling it correctly, rather than about
# formal OpenAPI validity.


def test_openapi_version_is_the_newest_3_1(spec: dict) -> None:
    assert spec["openapi"] == "3.1.1"


def test_every_operation_is_marked_non_consequential(spec: dict) -> None:
    """Otherwise ChatGPT confirms every POST and offers no 'always allow'.

    OpenAI's documented default is false for GET and true for everything else, so
    without this flag a 30-company batch sits behind a confirmation dialog per
    call. Nothing here mutates the caller's data; it reads public web pages.
    """
    for operations in spec["paths"].values():
        for operation in operations.values():
            assert operation["x-openai-isConsequential"] is False, operation["operationId"]


def test_no_nullable_unions_survive(spec: dict) -> None:
    """Pydantic's anyOf/null pairs describe a case the model never sees."""
    blob = json.dumps(spec)
    assert '"anyOf"' not in blob
    assert '"type": "null"' not in blob


def test_validation_error_plumbing_is_removed(spec: dict) -> None:
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    assert "ValidationError" not in spec["components"]["schemas"]
    for operations in spec["paths"].values():
        for operation in operations.values():
            assert "422" not in operation["responses"]


def test_generated_titles_are_stripped(spec: dict) -> None:
    for schema in spec["components"]["schemas"].values():
        assert "title" not in schema
        for name, prop in schema.get("properties", {}).items():
            assert "title" not in prop, name
    assert spec["info"]["title"] == "Company Website Research API"


def test_a_property_genuinely_called_title_is_kept(spec: dict) -> None:
    """Stripping auto-titles must not delete the page title field."""
    assert "title" in spec["components"]["schemas"]["PageResult"]["properties"]


def test_every_parameter_tells_the_model_where_the_value_comes_from(spec: dict) -> None:
    for operations in spec["paths"].values():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                description = parameter.get("description", "")
                assert description, parameter["name"]
                assert len(description) <= MAX_PARAMETER_DESCRIPTION


def test_job_id_parameter_forbids_inventing_one(batch_spec: dict) -> None:
    parameter = batch_spec["paths"]["/v1/research/jobs/{job_id}"]["get"]["parameters"][0]
    assert parameter["name"] == "job_id"
    assert "never invent" in parameter["description"].lower()


def test_info_description_tells_the_model_to_loop(spec: dict) -> None:
    """The model reads this before deciding whether to call the action at all."""
    description = spec["info"]["description"].lower()
    assert "once per company" in description
    assert "do not skip a company" in description
    assert "do not batch several urls into one call" in description
    assert "do not stop early" in description


def test_info_description_forbids_filling_gaps_from_memory(spec: dict) -> None:
    """Invariant 1 stated where the model cannot miss it."""
    description = spec["info"]["description"].lower()
    assert "never fill the gap from memory" in description
    assert "search engine" in description
    assert "directory" in description
    assert "one failed call is never a reason to abandon" in description


def test_info_description_gains_the_batch_workflow_behind_the_flag(
    batch_spec: dict,
) -> None:
    description = batch_spec["info"]["description"]
    for expected in ("createResearchJob", "getResearchJob", "next_start", "has_more"):
        assert expected in description, expected


def test_the_document_stays_small(spec: dict) -> None:
    """Half the size of the four-action version, and every byte is load-bearing."""
    assert len(json.dumps(spec)) < 10_000
