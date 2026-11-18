"""`make openapi-check`: the published contract agrees with the server (docs/spec.md 5, 10, 11, 18).

Each check takes its inputs as arguments and returns a list of problems, so the tests can prove
each one fails on a broken fixture. `run_all` feeds them the real documents and registries.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from django.conf import settings
from django.urls import URLPattern, URLResolver, get_resolver
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

from octoreview.core.errors import ERROR_CODES
from octoreview.events.services import EVENT_TYPES

DOCS = Path(settings.BASE_DIR) / "docs"
OPENAPI = DOCS / "openapi.yaml"
ERRORS_MD = DOCS / "errors.md"
EVENTS_MD = DOCS / "events.md"
SURFACE = DOCS / "sdk-surface.yaml"
SURFACE_MD = DOCS / "sdk-surface.md"

API_PREFIX = "api/v1/"
METHODS = ("get", "put", "post", "patch", "delete")
LANGUAGES = ("go", "php", "js")
ERROR_LABELS = ("Meaning", "Details", "Fix", "Recovery")
_CONVERTER = re.compile(r"<(?:\w+:)?(\w+)>")
_HEADING = re.compile(r"^### (\S+)\s*$")
_FIELD_ROW = re.compile(r"^\| `([a-z_]+)` \|")


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# --- the OpenAPI document ------------------------------------------------------------------------


def operations(document: dict) -> dict[str, tuple[str, str, dict]]:
    found = {}
    for path, item in document.get("paths", {}).items():
        for method in METHODS:
            if method in item:
                found[item[method].get("operationId")] = (method, path, item[method])
    return found


def _resolve(document: dict, node):
    while isinstance(node, dict) and "$ref" in node:
        target = document
        for token in node["$ref"].removeprefix("#/").split("/"):
            target = target[token.replace("~1", "/").replace("~0", "~")]
        node = target
    return node


def operation_parameters(document: dict, operation_id: str) -> tuple[list[str], list[str]]:
    """Required and optional path, query and JSON body fields; headers are client settings."""

    method, path, operation = operations(document)[operation_id]
    required: list[str] = []
    optional: list[str] = []
    shared = document["paths"][path].get("parameters", [])
    for parameter in [*shared, *operation.get("parameters", [])]:
        parameter = _resolve(document, parameter)
        if parameter["in"] == "header":
            continue
        (required if parameter.get("required") else optional).append(parameter["name"])
    body = operation.get("requestBody", {}).get("content", {}).get("application/json")
    if body:
        schema = _resolve(document, body["schema"])
        for name in schema.get("properties", {}):
            if name in schema.get("required", []):
                if name in optional:
                    optional.remove(name)
                if name not in required:
                    required.append(name)
            elif name not in required and name not in optional:
                optional.append(name)
    return required, optional


def check_document(document: dict) -> list[str]:
    """Valid OpenAPI 3.1 (the validator also refuses duplicate operationIds), and every
    operation has an operationId: the SDK surface and the contract gates key on it."""

    try:
        validate(document)
    except OpenAPIValidationError as exc:
        return [f"openapi.yaml is not valid OpenAPI: {exc.message}"]
    return [
        f"{method.upper()} {path} has no operationId"
        for path, item in document["paths"].items()
        for method in METHODS
        if method in item and not item[method].get("operationId")
    ]


# --- routes --------------------------------------------------------------------------------------


def _view_methods(callback) -> set[str]:
    view_class = getattr(callback, "cls", None)
    if view_class is not None:
        return {
            method.upper()
            for method in METHODS
            if method in view_class.http_method_names and hasattr(view_class, method)
        }
    # Plain Django views declare what they accept (octoreview.api.media's token endpoints).
    return {method.upper() for method in getattr(callback, "http_methods", ())}


def django_routes(resolver=None, prefix: str = "") -> set[tuple[str, str]]:
    """Every (METHOD, OpenAPI path) the URLconf serves under /api/v1."""

    resolver = resolver or get_resolver()
    routes = set()
    for pattern in resolver.url_patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            routes |= django_routes(pattern, route)
        elif isinstance(pattern, URLPattern) and route.startswith(API_PREFIX):
            path = "/" + _CONVERTER.sub(r"{\1}", route.removeprefix(API_PREFIX))
            routes |= {(method, path) for method in _view_methods(pattern.callback)}
    return routes


def check_routes(document: dict, routes: set[tuple[str, str]]) -> list[str]:
    documented = {(method.upper(), path) for method, path, _ in operations(document).values()}
    problems = [
        f"{method} {path} is served but missing from openapi.yaml"
        for method, path in sorted(routes - documented)
    ]
    problems += [
        f"{method} {path} is in openapi.yaml but the server does not serve it"
        for method, path in sorted(documented - routes)
    ]
    return problems


# --- errors --------------------------------------------------------------------------------------


def check_error_codes(document: dict, registry: dict) -> list[str]:
    enum = set(document["components"]["schemas"]["ErrorCode"]["enum"])
    codes = set(registry)
    return [
        f"error code {code} is missing from openapi.yaml ErrorCode" for code in sorted(codes - enum)
    ] + [
        f"openapi.yaml ErrorCode lists {code}, which the server never sends"
        for code in sorted(enum - codes)
    ]


def _sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = heading.group(1)
            if current in sections:
                sections[current].append("\x00duplicate")
            sections.setdefault(current, [])
        elif line.startswith("## "):
            current = None
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def check_errors_md(text: str, registry: dict) -> list[str]:
    sections = _sections(text)
    problems = [
        f"errors.md has no section for {code}" for code in sorted(set(registry) - set(sections))
    ]
    problems += [
        f"errors.md documents {name}, which is not an error code"
        for name in sorted(set(sections) - set(registry))
    ]
    for code, body in sorted(sections.items()):
        if code not in registry:
            continue
        entry = registry[code]
        if "\x00duplicate" in body:
            problems.append(f"errors.md has more than one section for {code}")
        status = re.search(r"\*\*Status:\*\* (\d{3})", body)
        if not status or int(status.group(1)) != entry.status:
            problems.append(f"errors.md {code}: status must be {entry.status}")
        retryable = re.search(r"\*\*Retryable:\*\* (yes|no)", body)
        if not retryable or (retryable.group(1) == "yes") != entry.retryable:
            problems.append(
                f"errors.md {code}: retryable must be {'yes' if entry.retryable else 'no'}"
            )
        for label in ERROR_LABELS:
            if f"**{label}:**" not in body:
                problems.append(f"errors.md {code}: missing **{label}:**")
    return problems


# --- events --------------------------------------------------------------------------------------


def documented_event_fields(text: str) -> dict[str, set[str]]:
    """The payload fields each `### <event>` section's table lists."""

    return {
        name: {match.group(1) for line in body.splitlines() if (match := _FIELD_ROW.match(line))}
        for name, body in _sections(text).items()
    }


def check_events_md(text: str, event_types) -> list[str]:
    documented = documented_event_fields(text)
    problems = [
        f"events.md has no section for {name}"
        for name in sorted(set(event_types) - set(documented))
    ]
    problems += [
        f"events.md documents {name}, which the server never emits"
        for name in sorted(set(documented) - set(event_types))
    ]
    problems += [
        f"events.md {name} lists no payload fields"
        for name, fields in sorted(documented.items())
        if name in event_types and not fields
    ]
    return problems


# --- SDK surface ---------------------------------------------------------------------------------


def check_surface(surface: dict, document: dict) -> list[str]:
    problems: list[str] = []
    known = operations(document)
    retry_classes = set(surface.get("retry_classes", {}))
    entries = surface.get("operations", [])
    listed = [entry.get("operation") for entry in entries]
    for operation_id in sorted(set(known) - set(listed)):
        problems.append(f"sdk-surface.yaml has no entry for {operation_id}")
    for operation_id in sorted(set(listed) - set(known)):
        problems.append(
            f"sdk-surface.yaml lists {operation_id}, which openapi.yaml does not define"
        )
    for operation_id in sorted({name for name in listed if listed.count(name) > 1}):
        problems.append(f"sdk-surface.yaml lists {operation_id} more than once")

    names: dict[str, dict[str, str]] = {language: {} for language in LANGUAGES}
    callable_ops = set()
    for entry in entries:
        operation_id = entry.get("operation")
        if operation_id not in known:
            continue
        if entry.get("sdk") == "none":
            if not entry.get("reason"):
                problems.append(f"{operation_id}: `sdk: none` needs a reason")
            if any(language in entry for language in LANGUAGES):
                problems.append(f"{operation_id}: `sdk: none` cannot name SDK methods")
            continue
        callable_ops.add(operation_id)
        for language in LANGUAGES:
            method = entry.get(language)
            if not method:
                problems.append(f"{operation_id}: missing the {language} method")
            elif method in names[language]:
                problems.append(
                    f"{operation_id}: {language} method {method} is also {names[language][method]}"
                )
            else:
                names[language][method] = operation_id
        retry = entry.get("retry")
        if retry not in retry_classes:
            problems.append(f"{operation_id}: retry must be one of {sorted(retry_classes)}")
        required, optional = operation_parameters(document, operation_id)
        if set(entry.get("required", [])) != set(required):
            problems.append(f"{operation_id}: required must be {sorted(required)}")
        if set(entry.get("optional", [])) != set(optional):
            problems.append(f"{operation_id}: optional must be {sorted(optional)}")
        if retry == "keyed" and entry.get("idempotency_key") not in optional:
            problems.append(
                f"{operation_id}: keyed retry needs an optional idempotency_key parameter"
            )

    for helper in surface.get("helpers", []):
        name = helper.get("name")
        for language in LANGUAGES:
            method = helper.get(language)
            if not method:
                problems.append(f"helper {name}: missing the {language} method")
            elif method in names[language]:
                problems.append(
                    f"helper {name}: {language} method {method} is also {names[language][method]}"
                )
            else:
                names[language][method] = f"helper {name}"
        for operation_id in helper.get("composes", []):
            if operation_id not in callable_ops:
                problems.append(f"helper {name} composes {operation_id}, which no SDK method calls")
    return problems


def render_surface_md(surface: dict) -> str:
    lines = [
        "# SDK surface",
        "",
        "Generated from `docs/sdk-surface.yaml` by `make sdk-surface`; edit the YAML, not this",
        "file. `make openapi-check` fails when it is stale.",
        "",
        "## Retry classes",
        "",
        "| Class | Rule |",
        "| --- | --- |",
    ]
    lines += [f"| `{name}` | {rule} |" for name, rule in surface["retry_classes"].items()]
    lines += [
        "",
        "## Operations",
        "",
        "| Operation | Go | PHP | JS | Retry | Required | Optional |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in surface["operations"]:
        if entry.get("sdk") == "none":
            lines.append(f"| `{entry['operation']}` | — | — | — | — | {entry['reason']} | |")
            continue
        retry = entry["retry"]
        if entry.get("idempotency_key"):
            retry += f" (`{entry['idempotency_key']}`)"
        required = ", ".join(f"`{name}`" for name in entry["required"])
        optional = ", ".join(f"`{name}`" for name in entry["optional"])
        lines.append(
            f"| `{entry['operation']}` | `{entry['go']}` | `{entry['php']}` | `{entry['js']}` "
            f"| {retry} | {required} | {optional} |"
        )
    lines += [
        "",
        "## Helpers",
        "",
        "| Helper | Go | PHP | JS | Calls | What it does |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for helper in surface["helpers"]:
        calls = ", ".join(f"`{name}`" for name in helper["composes"]) or "none (local)"
        lines.append(
            f"| `{helper['name']}` | `{helper['go']}` | `{helper['php']}` | `{helper['js']}` "
            f"| {calls} | {helper['summary']} |"
        )
    return "\n".join(lines) + "\n"


def check_surface_md(surface: dict, text: str | None) -> list[str]:
    if text != render_surface_md(surface):
        return ["docs/sdk-surface.md is stale; run `make sdk-surface`"]
    return []


# --- everything ----------------------------------------------------------------------------------


def run_all() -> list[str]:
    document = load_yaml(OPENAPI)
    problems = check_document(document)
    if problems:
        return problems
    surface = load_yaml(SURFACE)
    surface_md = SURFACE_MD.read_text() if SURFACE_MD.exists() else None
    return [
        *check_routes(document, django_routes()),
        *check_error_codes(document, ERROR_CODES),
        *check_errors_md(ERRORS_MD.read_text(), ERROR_CODES),
        *check_events_md(EVENTS_MD.read_text(), EVENT_TYPES),
        *check_surface(surface, document),
        *check_surface_md(surface, surface_md),
    ]
