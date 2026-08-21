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
* exposes **one action by default**: ``research``.  The batch job endpoints
  still exist on the API, but handing them to a GPT asks the model to run a
  three-step state machine -- create a job, carry its id, poll it, paginate the
  results -- and a language model is a poor state machine.  Every observed
  failure was the model abandoning that machine partway and inventing "research
  unavailable" rows.  One synchronous call per company is how WebPilot works,
  and it has no state to lose.  Pass ``--with-batch`` to expose the job
  endpoints as well;
* pins the server URL to the deployment domain;
* enforces the 300/700-character description limits GPT Actions impose;
* declares bearer authentication, which is the only auth style that works given
  that GPT Actions do not support custom headers;
* marks every operation ``x-openai-isConsequential: false``.  Without it ChatGPT
  treats *every* non-GET as consequential, which means a confirmation prompt on
  every single call and no "always allow" button -- unusable for a 30-company
  batch.  Nothing here mutates the caller's data; it reads public web pages;
* flattens the ``anyOf: [X, null]`` unions Pydantic emits for optional fields,
  drops the auto-generated ``title`` noise, and removes the FastAPI validation
  error schemas.  None of it is actionable by the model and all of it competes
  for attention with the parts that are;
* pins ``openapi: 3.1.1``, the newest 3.1 patch release;
* puts the *workflow* in ``info.description``: which action to call first, how to
  poll, how to page, and what to do when one company fails.  This is the single
  most load-bearing part of the document -- the model reads it before deciding
  whether to call the action at all.

Usage:
    python scripts/export_openapi.py https://research.example.com > openapi.gpt-action.json
    python scripts/export_openapi.py https://research.example.com --with-batch
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

#: Newest 3.1 patch release.  ChatGPT accepts the whole 3.1 line.
OPENAPI_VERSION = "3.1.1"

#: The model reads this before it decides whether the action is worth calling,
#: and again when a call fails.  It is prompt, not documentation, so it states
#: the batch workflow explicitly and says what to do on failure -- the GPT's
#: observed failure mode was one bad call followed by twenty fabricated
#: "research unavailable" rows.
INFO_DESCRIPTION_SINGLE = (
    "Reads a company's own website and returns the cleaned text of its most "
    "relevant pages with source URLs, so copy can be written from evidence "
    "instead of assumption. Only the company's own domain is crawled, and "
    "nothing is invented: if a site cannot be read, the response says so.\n\n"
    "How to use it. Call research once per company, with that company's URL. "
    "For a list of companies, work down the list in the given order and call "
    "research separately for each one. Do not skip a company, do not batch "
    "several URLs into one call, and do not stop early. Each call takes a few "
    "seconds and is independent of the others.\n\n"
    "Reading the response. status is success, partial or failed. On success or "
    "partial, write only from the text in pages, and cite the url of the page "
    "the fact came from. site.site_name and site.tagline are the company's own "
    "words for itself. coverage.found and coverage.missing say which kinds of "
    "page were located.\n\n"
    "When a site cannot be read. status is failed and status_reason says why. "
    "Report that company as unresearched and move on to the next one. Never "
    "fill the gap from memory, from a search engine, from a directory, from "
    "social media, or from anything you know about the company. An honest "
    "\"could not be read\" is the correct answer; an invented detail is not.\n\n"
    "If a call errors, retry that one company once, then continue down the "
    "list. One failed call is never a reason to abandon the remaining "
    "companies or to answer the whole list from memory."
)

#: Appended only when the batch job endpoints are exposed (--with-batch).
INFO_DESCRIPTION_BATCH = (
    "\n\nLarge lists. For more than about ten companies you may instead call "
    "createResearchJob once with every URL, in the order you want them back. It "
    "returns a job_id immediately. Poll getResearchJob with that exact job_id "
    "every 10 to 20 seconds until status is \"completed\", then read "
    "getResearchResults with start=0 and limit=5, following next_start until "
    "has_more is false. Reuse the job_id you were given; never invent one. If "
    "any of that fails, fall back to calling research once per company."
)


def build(server_url: str, *, include_batch: bool = False) -> dict[str, Any]:
    settings = Settings(
        api_keys="placeholder:standard",
        environment="development",
        cache={"enabled": False},
        renderer={"enabled": False},
    )
    app = create_app(settings)
    spec = app.openapi()

    spec["openapi"] = OPENAPI_VERSION
    spec["servers"] = [{"url": server_url.rstrip("/")}]
    spec["info"]["title"] = "Company Website Research API"
    spec["info"]["description"] = INFO_DESCRIPTION_SINGLE + (
        INFO_DESCRIPTION_BATCH if include_batch else ""
    )

    # Keep only the actions the GPT may call.
    exposed = EXPOSED_PATHS if include_batch else SINGLE_ACTION_PATHS
    spec["paths"] = {
        path: operations
        for path, operations in spec["paths"].items()
        if path in exposed
    }

    schemas = spec.setdefault("components", {}).setdefault("schemas", {})

    # Remove the advanced options from the model-facing contract entirely.
    request_schema = schemas.get("ResearchRequest", {})
    request_schema.get("properties", {}).pop("advanced", None)
    # Server callers choose a content format; the model must not. It has no
    # basis for preferring markdown, and the GPT path is budgeted in characters
    # where markdown costs roughly double for the same prose.
    request_schema.get("properties", {}).pop("output_format", None)
    request_schema.get("properties", {}).pop("max_age_seconds", None)
    request_schema.get("properties", {}).pop("include_signals", None)
    schemas.get("SiteInfo", {}).get("properties", {}).pop("signals", None)
    schemas.pop("SiteSignals", None)
    schemas.pop("AdvancedOptions", None)
    schemas.pop("AdvancedOptions-Input", None)

    _strip_dangling_refs(spec, {"AdvancedOptions", "AdvancedOptions-Input"})
    _strip_auth_header_parameters(spec)
    _simplify_operation_ids(spec)
    _mark_non_consequential(spec)
    _drop_validation_error_schemas(spec)
    _flatten_nullable_unions(spec)
    _strip_generated_titles(spec)
    _describe_parameters(spec)
    _drop_unreferenced_schemas(spec)
    _enforce_description_limits(spec)

    # GPT Actions do not support custom headers; bearer is the correct choice.
    spec["components"]["securitySchemes"] = {
        "bearerAuth": {"type": "http", "scheme": "bearer"}
    }
    spec["security"] = [{"bearerAuth": []}]

    return spec


#: Path and query parameter descriptions, written for the model rather than for
#: a developer reading reference docs.
PARAMETER_DESCRIPTIONS = {
    "job_id": (
        "The exact job_id returned by createResearchJob. Copy it verbatim and "
        "reuse the same one for every poll and every results page. Never invent "
        "or guess a job_id. If this returns 404 the job has expired, so start a "
        "new one with createResearchJob."
    ),
    "start": (
        "Index of the first result to return. Use 0 for the first page, then the "
        "next_start value from the previous page. Results stay in the original "
        "URL order."
    ),
    "limit": (
        "How many companies to return in this page. Default 5, maximum 10. "
        "Responses are size limited, so ask for a page at a time rather than "
        "the whole job."
    ),
}

#: Short, stable operation ids, so the GPT calls the action by a readable name.
OPERATION_IDS = {
    ("/v1/research", "post"): "research",
    ("/v1/research/jobs", "post"): "createResearchJob",
    ("/v1/research/jobs/{job_id}", "get"): "getResearchJob",
    ("/v1/research/jobs/{job_id}/results", "get"): "getResearchResults",
}

#: Every action that *may* be exposed.  Anything else is stripped.
EXPOSED_PATHS = tuple(path for path, _ in OPERATION_IDS)

#: The default: one action, called once per company.
SINGLE_ACTION_PATHS = ("/v1/research",)


def _strip_auth_header_parameters(spec: dict[str, Any]) -> None:
    """Drop **every** header parameter from the model-facing document.

    GPT Actions do not support custom headers at all, so a declared one is at
    best noise ("parameter authorization has location header; ignoring") and at
    worst an instruction the model cannot follow. Authorization is expressed by
    securitySchemes; Idempotency-Key is a server-to-server concern the model has
    no part in.
    """
    for operations in spec["paths"].values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            parameters = operation.get("parameters")
            if not parameters:
                continue
            kept = [
                parameter for parameter in parameters if parameter.get("in") != "header"
            ]
            if kept:
                operation["parameters"] = kept
            else:
                operation.pop("parameters", None)


def _mark_non_consequential(spec: dict[str, Any]) -> None:
    """Let ChatGPT run these actions without confirming each one.

    Absent this flag ChatGPT "defaults all GET operations to false and all other
    operations to true", and a consequential operation is confirmed every time
    with no "always allow" button.  Researching a public website mutates nothing
    on the caller's behalf, and a 30-company batch behind 30 confirmation
    dialogs is not a workflow anyone completes.
    """
    for operations in spec["paths"].values():
        for operation in operations.values():
            if isinstance(operation, dict):
                operation["x-openai-isConsequential"] = False


def _drop_validation_error_schemas(spec: dict[str, Any]) -> None:
    """Remove FastAPI's 422 plumbing.

    ``HTTPValidationError`` describes a failure the model cannot act on, and it
    drags ``ValidationError`` and its ``anyOf`` location union in with it.  The
    endpoint still returns 422 for a malformed body; it just is not advertised.
    """
    for operations in spec["paths"].values():
        for operation in operations.values():
            if isinstance(operation, dict):
                operation.get("responses", {}).pop("422", None)
    schemas = spec.get("components", {}).get("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)


def _flatten_nullable_unions(node: Any) -> None:
    """Turn ``anyOf: [X, {type: null}]`` back into plain ``X``.

    Pydantic v2 emits this for every optional field.  It is correct OpenAPI 3.1
    and it is also 27 unions of pure noise in a document whose whole job is to
    be legible to a language model.  The API already omits null fields from
    responses, so the union describes a case that never reaches the model.
    """
    if isinstance(node, dict):
        variants = node.get("anyOf")
        if isinstance(variants, list):
            kept = [v for v in variants if not _is_null_type(v)]
            if len(kept) < len(variants):
                if len(kept) == 1 and isinstance(kept[0], dict):
                    node.pop("anyOf")
                    for key, value in kept[0].items():
                        node.setdefault(key, value)
                elif kept:
                    node["anyOf"] = kept
                else:
                    node.pop("anyOf")
        for value in node.values():
            _flatten_nullable_unions(value)
    elif isinstance(node, list):
        for item in node:
            _flatten_nullable_unions(item)


def _is_null_type(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "null" and len(node) == 1


def _strip_generated_titles(spec: dict[str, Any]) -> None:
    """Drop Pydantic's auto-titles ("Job Id", "Force Refresh").

    They restate the property name in title case and carry no information the
    model does not already have from the key itself.  ``info.title`` and any
    property genuinely *named* title are untouched.
    """
    for schema in spec.get("components", {}).get("schemas", {}).values():
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            if isinstance(prop, dict):
                prop.pop("title", None)
    for operations in spec["paths"].values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                schema = parameter.get("schema")
                if isinstance(schema, dict):
                    schema.pop("title", None)


def _describe_parameters(spec: dict[str, Any]) -> None:
    """Give the path and query parameters model-facing descriptions.

    ``job_id`` arrives from FastAPI with no description at all, which leaves the
    model to guess where the value comes from -- and a model guessing at an
    identifier is a model inventing one.
    """
    for operations in spec["paths"].values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                text = PARAMETER_DESCRIPTIONS.get(parameter.get("name"))
                if text:
                    parameter["description"] = text


def _simplify_operation_ids(spec: dict[str, Any]) -> None:
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if isinstance(operation, dict) and (path, method) in OPERATION_IDS:
                operation["operationId"] = OPERATION_IDS[(path, method)]


def _drop_unreferenced_schemas(spec: dict[str, Any]) -> None:
    """Remove component schemas nothing reaches.

    Hiding the job endpoints leaves their request and response models orphaned.
    They are dead weight in a document the model has to read, and a schema
    describing an action that is not offered is an invitation to try it.
    """
    schemas = spec.get("components", {}).get("schemas", {})
    reachable: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in reachable:
                    reachable.add(name)
                    visit(schemas.get(name))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(spec["paths"])
    for name in [n for n in schemas if n not in reachable]:
        schemas.pop(name)


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
    args = [a for a in sys.argv[1:] if a != "--with-batch"]
    server_url = args[0] if args else "https://research.example.com"
    spec = build(server_url, include_batch="--with-batch" in sys.argv)
    json.dump(spec, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
