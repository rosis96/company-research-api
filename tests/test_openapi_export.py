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


def test_only_the_research_endpoint_is_exposed(spec: dict) -> None:
    assert list(spec["paths"]) == ["/v1/research"]


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
