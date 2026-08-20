"""Generate the OpenAPI document to import into a Custom GPT Action.

Architecture reference: SS9.

The runtime schema at ``/openapi.json`` is not what the GPT should be given.
This script produces a GPT-facing variant that:

* strips the ``advanced`` options object, so the model cannot pass numeric crawl
  knobs it has no basis for choosing;
* removes the ``Authorization`` header *parameter*.  FastAPI documents it because
  the auth dependency reads that header, but GPT Actions supply it themselves from
  the configured API key and warn "parameter authorization has location header;
  ignoring".  The bearer ``securitySchemes`` entry below is the correct way to
  declare it, and it is already present;
* gives the operation a short ``operationId`` so the action is named ``research``
  in the GPT rather than ``research_v1_research_post``;
* strips ``/v1/health``, ``/privacy`` and ``/bot``, exposing exactly four
  actions: ``research``, ``createResearchJob``, ``getResearchJob`` and
  ``getResearchResults``;
* pins the server URL to the deployment domain;
* enforces the 300/700-character description limits GPT Actions impose;
* declares bearer authentication, which is the only auth style that works given
  that GPT Actions do not support custom headers.

Usage:
    python scripts/export_openapi.py https://research.example.com > openapi.gpt-action.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

sys.path.insert(0, ".")

from app.config.settings import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

MAX_ENDPOINT_DESCRIPTION = 300
MAX_PARAMETER_DESCRIPTION = 700


def build(server_url: str) -> dict[str, Any]:
    settings = Settings(
        api_keys="placeholder:standard",
        environment="development",
        cache={"enabled": False},
        renderer={"enabled": False},
    )
    app = create_app(settings)
    spec = app.openapi()

    spec["servers"] = [{"url": server_url.rstrip("/")}]
    spec["info"]["title"] = "Company Website Research API"
    spec["info"]["description"] = (
        "Reads a single company's own website and returns cleaned page text with "
        "source URLs, so that content can be written from evidence rather than "
        "assumption. Only the company's own domain is crawled."
    )

    # Keep only the actions the GPT may call.
    spec["paths"] = {
        path: operations
        for path, operations in spec["paths"].items()
        if path in EXPOSED_PATHS
    }

    schemas = spec.setdefault("components", {}).setdefault("schemas", {})

    # Remove the advanced options from the model-facing contract entirely.
    request_schema = schemas.get("ResearchRequest", {})
    request_schema.get("properties", {}).pop("advanced", None)
    schemas.pop("AdvancedOptions", None)
    schemas.pop("AdvancedOptions-Input", None)

    _strip_dangling_refs(spec, {"AdvancedOptions", "AdvancedOptions-Input"})
    _strip_auth_header_parameters(spec)
    _simplify_operation_ids(spec)
    _enforce_description_limits(spec)

    # GPT Actions do not support custom headers; bearer is the correct choice.
    spec["components"]["securitySchemes"] = {
        "bearerAuth": {"type": "http", "scheme": "bearer"}
    }
    spec["security"] = [{"bearerAuth": []}]

    return spec


#: Header parameters GPT Actions provide themselves and warn about if declared.
AUTH_HEADER_NAMES = {"authorization", "x-api-key", "api-key"}

#: Short, stable operation ids, so the GPT calls the action by a readable name.
OPERATION_IDS = {
    ("/v1/research", "post"): "research",
    ("/v1/research/jobs", "post"): "createResearchJob",
    ("/v1/research/jobs/{job_id}", "get"): "getResearchJob",
    ("/v1/research/jobs/{job_id}/results", "get"): "getResearchResults",
}

#: Exactly the actions the GPT may call.  Anything else is stripped.
EXPOSED_PATHS = tuple(path for path, _ in OPERATION_IDS)


def _strip_auth_header_parameters(spec: dict[str, Any]) -> None:
    """Drop declared auth headers; authentication is expressed by securitySchemes.

    Leaving these in produces the ChatGPT import warning "parameter authorization
    has location header; ignoring" -- harmless but noisy, and it implies the model
    should supply the header, which it must not.
    """
    for operations in spec["paths"].values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            parameters = operation.get("parameters")
            if not parameters:
                continue
            kept = [
                parameter
                for parameter in parameters
                if not (
                    parameter.get("in") == "header"
                    and str(parameter.get("name", "")).lower() in AUTH_HEADER_NAMES
                )
            ]
            if kept:
                operation["parameters"] = kept
            else:
                operation.pop("parameters", None)


def _simplify_operation_ids(spec: dict[str, Any]) -> None:
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if isinstance(operation, dict) and (path, method) in OPERATION_IDS:
                operation["operationId"] = OPERATION_IDS[(path, method)]


def _strip_dangling_refs(node: Any, removed: set[str]) -> None:
    """Drop any ``$ref`` pointing at a schema that was removed."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            value = node[key]
            if (
                key == "allOf"
                and isinstance(value, list)
                and any(_is_removed_ref(item, removed) for item in value)
            ):
                node[key] = [item for item in value if not _is_removed_ref(item, removed)]
                if not node[key]:
                    node.pop(key)
                continue
            if isinstance(value, dict) and _is_removed_ref(value, removed):
                node.pop(key)
                continue
            _strip_dangling_refs(value, removed)
    elif isinstance(node, list):
        for item in node:
            _strip_dangling_refs(item, removed)


def _is_removed_ref(node: Any, removed: set[str]) -> bool:
    return (
        isinstance(node, dict)
        and isinstance(node.get("$ref"), str)
        and node["$ref"].rsplit("/", 1)[-1] in removed
    )


def _enforce_description_limits(spec: dict[str, Any]) -> None:
    for operations in spec["paths"].values():
        for operation in operations.values():
            if isinstance(operation, dict) and "description" in operation:
                operation["description"] = operation["description"][:MAX_ENDPOINT_DESCRIPTION]

    for schema in spec.get("components", {}).get("schemas", {}).values():
        for prop in schema.get("properties", {}).values():
            if isinstance(prop, dict) and "description" in prop:
                prop["description"] = prop["description"][:MAX_PARAMETER_DESCRIPTION]


def main() -> int:
    server_url = sys.argv[1] if len(sys.argv) > 1 else "https://research.example.com"
    spec = build(server_url)
    json.dump(spec, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
