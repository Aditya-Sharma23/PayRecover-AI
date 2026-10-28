"""Turn an OpenAPI 3.x or Swagger 2.0 document into reviewable API check suggestions.

Nothing here makes a network request: the spec is supplied by the user, and
remote $ref documents are never fetched. Only GET operations are runnable,
because the API runner never sends state-changing requests.
"""

import copy
import json
from typing import Any
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.schemas import (
    MAX_SCHEMA_BYTES,
    OpenApiImportRequest,
    OpenApiImportResult,
    OpenApiOperation,
)

MAX_OPERATIONS = 100
MAX_REF_DEPTH = 8
MAX_RESOLVED_NODES = 20_000
METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


class OpenApiError(ValueError):
    """The supplied document is not a usable OpenAPI description."""


def _load(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            # Anchors and aliases are refused: they let a small file expand into a huge tree.
            if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
                raise OpenApiError("YAML anchors and aliases are not supported; expand them first.")
            value = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise OpenApiError("The document is neither valid JSON nor valid YAML.") from exc
    if not isinstance(value, dict) or not ("openapi" in value or "swagger" in value):
        raise OpenApiError("The document has no openapi or swagger version field.")
    if not isinstance(value.get("paths"), dict):
        raise OpenApiError("The document has no paths object.")
    return value


def _absolute_http(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def _base_url(spec: dict[str, Any], supplied: str | None) -> str | None:
    if supplied:
        return supplied.rstrip("/")
    if "swagger" in spec and spec.get("host"):
        scheme = "https" if "https" in spec.get("schemes", ["https"]) else spec["schemes"][0]
        return f"{scheme}://{spec['host']}{spec.get('basePath', '')}".rstrip("/")
    for server in spec.get("servers") or []:
        url = str(server.get("url", "")) if isinstance(server, dict) else ""
        if _absolute_http(url) and "{" not in url:
            return url.rstrip("/")
    return None


class _Budget:
    def __init__(self) -> None:
        self.remaining = MAX_RESOLVED_NODES

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise OpenApiError("The document's references expand too far to import safely.")


def _resolve(
    spec: dict[str, Any], node: Any, depth: int = 0, seen: frozenset[str] = frozenset(),
    budget: _Budget | None = None,
) -> Any:
    """Inline local #/ references, bounded in depth and size, and translate OpenAPI 3.0 nullable."""
    budget = budget or _Budget()
    budget.spend()
    if depth > MAX_REF_DEPTH * 4:
        return {}
    if isinstance(node, list):
        return [_resolve(spec, item, depth + 1, seen, budget) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if not ref.startswith("#/") or ref in seen or len(seen) >= MAX_REF_DEPTH:
            return {}
        target: Any = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
        return _resolve(spec, target or {}, depth + 1, seen | {ref}, budget)
    resolved = {key: _resolve(spec, value, depth + 1, seen, budget) for key, value in node.items()}
    if resolved.pop("nullable", False) is True and isinstance(resolved.get("type"), str):
        resolved["type"] = [resolved["type"], "null"]
    return resolved


def _success_response(spec: dict[str, Any], operation: dict[str, Any]) -> tuple[int | None, str | None, Any]:
    responses = operation.get("responses") or {}
    codes = sorted(str(code) for code in responses if str(code).isdigit() and str(code).startswith("2"))
    if not codes:
        return None, None, None
    code = codes[0]
    response = _resolve(spec, responses[code]) or {}
    if "swagger" in spec:
        produces = operation.get("produces") or spec.get("produces") or []
        content_type = next((item for item in produces if "json" in item), produces[0] if produces else None)
        return int(code), content_type, response.get("schema")
    content = response.get("content") or {}
    content_type = next((item for item in content if "json" in item), next(iter(content), None))
    schema = (content.get(content_type) or {}).get("schema") if content_type else None
    return int(code), content_type, schema


def _usable_schema(schema: Any, warnings: list[str], label: str) -> dict[str, Any] | None:
    if not isinstance(schema, dict) or not schema:
        return None
    schema = copy.deepcopy(schema)
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        warnings.append(f"{label}: response schema is larger than 20 KB and was left out.")
        return None
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        warnings.append(f"{label}: response schema is not valid JSON Schema and was left out.")
        return None
    return schema


def import_openapi(request: OpenApiImportRequest) -> OpenApiImportResult:
    spec = _load(request.spec)
    base_url = _base_url(spec, str(request.base_url) if request.base_url else None)
    warnings: list[str] = []
    if base_url is None:
        warnings.append("No absolute server URL was found; enter a base URL to make operations runnable.")
    operations: list[OpenApiOperation] = []
    for path, item in spec["paths"].items():
        if not isinstance(item, dict):
            continue
        shared_parameters = item.get("parameters") or []
        for method in METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            if len(operations) >= MAX_OPERATIONS:
                warnings.append(f"Only the first {MAX_OPERATIONS} operations were imported.")
                break
            label = f"{method.upper()} {path}"
            parameters = [_resolve(spec, p) for p in [*shared_parameters, *(operation.get("parameters") or [])]]
            required_query = [
                p.get("name") for p in parameters
                if isinstance(p, dict) and p.get("in") == "query" and p.get("required")
            ]
            reason = None
            if method != "get":
                reason = "Only GET operations are run; this one could change data on the server."
            elif "{" in path:
                reason = "Needs path parameter values; edit the URL before running."
            elif required_query:
                reason = f"Needs required query parameter(s): {', '.join(map(str, required_query))}."
            elif base_url is None:
                reason = "No base URL is known."
            status, content_type, schema = _success_response(spec, operation)
            operations.append(OpenApiOperation(
                operation_id=str(operation.get("operationId") or label)[:200],
                method=method.upper(),
                path=str(path)[:500],
                summary=str(operation.get("summary") or operation.get("description") or "")[:300],
                url=f"{base_url}{path}" if base_url else None,
                runnable=reason is None,
                reason=reason,
                expected_status_code=status,
                expected_content_type=content_type,
                response_schema=_usable_schema(_resolve(spec, schema), warnings, label) if method == "get" else None,
            ))
    info = spec.get("info") or {}
    return OpenApiImportResult(
        title=str(info.get("title") or "Untitled API")[:200],
        version=str(info.get("version") or spec.get("openapi") or spec.get("swagger"))[:50],
        base_url=base_url,
        operations=operations,
        warnings=list(dict.fromkeys(warnings)),
    )
