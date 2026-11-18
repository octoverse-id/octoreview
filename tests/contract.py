"""Validate real API responses against `docs/openapi.yaml`.

The published contract stays open to additive fields, but these tests check it strictly: every
object schema that lists properties is closed here, so a field the server sends without
documenting it fails just like a documented field the server forgets.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "openapi.yaml"
BASE_URI = "urn:octoreview:openapi"
METHODS = ("get", "put", "post", "patch", "delete")


def load_contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text())


def _closed(node):
    if isinstance(node, dict):
        if "properties" in node and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for value in node.values():
            _closed(value)
    elif isinstance(node, list):
        for value in node:
            _closed(value)
    return node


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


class Contract:
    def __init__(self, document: dict) -> None:
        self.document = document
        strict = _closed(copy.deepcopy(document))
        resource = Resource.from_contents(strict, default_specification=DRAFT202012)
        self.registry = Registry().with_resource(BASE_URI, resource)
        self.operations = {}
        for path, item in document["paths"].items():
            for method in METHODS:
                if method in item:
                    self.operations[item[method]["operationId"]] = (method, path, item[method])
        self.exercised: set[str] = set()

    def _validator(self, pointer: str) -> Draft202012Validator:
        return Draft202012Validator(
            {"$ref": f"{BASE_URI}#{pointer}"},
            registry=self.registry,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )

    def check(self, response, operation_id: str):
        """Assert the response matches the operation's documented response; returns it."""

        method, path, operation = self.operations[operation_id]
        status = str(response.status_code)
        responses = operation["responses"]
        if status in responses:
            spec, pointer = (
                responses[status],
                (f"/paths/{_escape(path)}/{method}/responses/{status}"),
            )
        else:
            assert int(status) >= 400, f"{operation_id} answered {status}, not in the contract"
            spec, pointer = (
                responses["default"],
                f"/paths/{_escape(path)}/{method}/responses/default",
            )
        if "$ref" in spec:
            pointer = spec["$ref"].removeprefix("#")
            spec = self.resolve(spec["$ref"])
        content = spec.get("content")
        if not content:
            assert not response.content, f"{operation_id} {status} must have no body"
        elif "application/json" in content:
            body = response.json()
            errors = sorted(
                self._validator(f"{pointer}/content/application~1json/schema").iter_errors(body),
                key=lambda error: list(error.absolute_path),
            )
            assert not errors, f"{operation_id} {status}: " + "; ".join(
                f"{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in errors
            )
        else:
            media_type = response["Content-Type"].split(";")[0]
            assert media_type in content, f"{operation_id} {status} sent {media_type}"
        self.exercised.add(operation_id)
        return response

    def resolve(self, ref: str) -> dict:
        node = self.document
        for token in ref.removeprefix("#/").split("/"):
            node = node[token.replace("~1", "/").replace("~0", "~")]
        return node
