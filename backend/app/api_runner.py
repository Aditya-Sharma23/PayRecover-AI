"""Bounded, evidence-first API smoke checks."""

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from app import contract_runner
from app.schemas import ApiRunRequest, CheckName, CheckResult, CheckStatus, JsonAssertion
from app.url_policy import validate_target_url
from app.website_runner import MAX_RESPONSE_BYTES, PinnedResolver

MAX_SCHEMA_ERRORS = 10
_MISSING = object()


@dataclass
class ApiResponse:
    status_code: int
    headers: dict[str, str]
    elapsed_ms: int
    body: bytes


async def _fetch_api(url: str, timeout_seconds: int, request_headers: dict[str, str] | None = None) -> ApiResponse:
    addresses = await validate_target_url(url)
    parts = urlsplit(url)
    resolver = PinnedResolver(parts.hostname or "", addresses)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        family=0,
        limit=1,
        limit_per_host=1,
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {"Accept": "application/json, text/plain, */*", **(request_headers or {})}
    started = time.monotonic()
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
        auto_decompress=True,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        async with session.get(
            url,
            headers=headers,
            allow_redirects=False,
            ssl=True,
        ) as response:
            body = await response.content.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded the 2 MB limit")
            safe_headers = {
                name: response.headers[name]
                for name in ("content-type", "location")
                if name in response.headers
            }
            return ApiResponse(
                response.status,
                safe_headers,
                int((time.monotonic() - started) * 1000),
                body,
            )


def _result(
    check_id: CheckName,
    status: CheckStatus,
    message: str,
    evidence: dict[str, Any],
    duration_ms: int,
    limitations: list[str] | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=status,
        duration_ms=max(0, duration_ms),
        message=message,
        evidence=evidence,
        limitations=limitations or [],
    )


def _lookup(document: Any, path: str) -> Any:
    """Follow a dot path such as data.items.0.id; returns _MISSING when absent."""
    if path == "$":
        return document
    current = document
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _preview(value: Any) -> Any:
    """Keep evidence small: scalars as-is (truncated), containers summarized."""
    if value is _MISSING:
        return None
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (dict, list)):
        return f"<{_json_type(value)} with {len(value)} item(s)>"
    return value


def _same(actual: Any, expected: Any) -> bool:
    # JSON true is not the number 1, even though Python says True == 1.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def evaluate_assertion(document: Any, assertion: JsonAssertion) -> dict[str, Any]:
    actual = _lookup(document, assertion.path)
    present = actual is not _MISSING
    operator = assertion.operator
    expected = assertion.expected
    if operator == "exists":
        passed = present
    elif operator == "not_exists":
        passed = not present
    elif not present:
        passed = False
    elif operator == "equals":
        passed = _same(actual, expected)
    elif operator == "not_equals":
        passed = not _same(actual, expected)
    elif operator == "contains":
        passed = (isinstance(actual, str) and expected in actual) or (
            isinstance(actual, list) and expected in actual
        )
    elif operator == "type":
        actual_type = _json_type(actual)
        passed = actual_type == expected or (expected == "number" and actual_type == "integer")
    else:  # length
        passed = isinstance(actual, (str, list, dict)) and len(actual) == expected
    return {
        "path": assertion.path,
        "operator": operator,
        "expected": expected,
        "found": present,
        "actual": _preview(actual),
        "passed": passed,
    }


def matches_json_shape(actual: Any, example: Any) -> bool:
    """Same keys, same JSON types, and every list item shaped like the sample's first item."""
    if isinstance(example, dict):
        return isinstance(actual, dict) and actual.keys() == example.keys() and all(
            matches_json_shape(actual[key], value) for key, value in example.items()
        )
    if isinstance(example, list):
        return isinstance(actual, list) and (not example or all(matches_json_shape(item, example[0]) for item in actual))
    if isinstance(example, bool):
        return isinstance(actual, bool)
    if isinstance(example, (int, float)):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool)
    return type(actual) is type(example)


def _parse_json(body: bytes) -> tuple[Any, str | None]:
    try:
        return json.loads(body.decode("utf-8-sig")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"The response body is not valid JSON ({type(exc).__name__})."


async def run_api_checks(request: ApiRunRequest) -> list[CheckResult]:
    """Execute one safe GET request and evaluate only explicit conditions."""
    response = await _fetch_api(str(request.url), request.timeout_seconds, request.request_headers)
    status_code, headers, elapsed_ms = response.status_code, response.headers, response.elapsed_ms
    response_size = len(response.body)
    header_note = (
        [f"Sent {len(request.request_headers)} custom request header(s); their values are never stored."]
        if request.request_headers else None
    )
    document: Any = None
    parse_error: str | None = None
    if {CheckName.API_JSON_ASSERTIONS, CheckName.API_JSON_SCHEMA, CheckName.API_JSON_SHAPE} & set(request.checks):
        document, parse_error = _parse_json(response.body)

    results: list[CheckResult] = []
    for check in request.checks:
        if check == CheckName.API_STATUS:
            passed = status_code == request.expected_status_code
            results.append(_result(
                check,
                CheckStatus.PASSED if passed else CheckStatus.FAILED,
                f"Observed HTTP status {status_code}; expected {request.expected_status_code}.",
                {"status_code": status_code, "expected_status_code": request.expected_status_code},
                elapsed_ms,
                header_note,
            ))
        elif check == CheckName.API_RESPONSE_TIME:
            passed = elapsed_ms <= request.max_response_time_ms
            results.append(_result(
                check,
                CheckStatus.PASSED if passed else CheckStatus.FAILED,
                f"Observed response time {elapsed_ms} ms; limit {request.max_response_time_ms} ms.",
                {"response_time_ms": elapsed_ms, "max_response_time_ms": request.max_response_time_ms},
                elapsed_ms,
            ))
        elif check == CheckName.API_CONTENT_TYPE:
            content_type = headers.get("content-type", "")
            expected = request.expected_content_type
            passed = bool(expected and content_type.lower().startswith(expected.lower()))
            results.append(_result(
                check,
                CheckStatus.PASSED if passed else CheckStatus.FAILED,
                f"Observed content type {content_type or 'missing'}; expected {expected}.",
                {"content_type": content_type, "expected_content_type": expected},
                elapsed_ms,
            ))
        elif check == CheckName.API_REDIRECTS:
            location = headers.get("location")
            redirected = 300 <= status_code < 400 or bool(location)
            results.append(_result(
                check,
                CheckStatus.FAILED if redirected else CheckStatus.PASSED,
                "A redirect was blocked." if redirected else "No redirect was observed.",
                {"status_code": status_code, "location": location},
                elapsed_ms,
                ["Redirects are never followed by the bounded API runner."] if redirected else None,
            ))
        elif check == CheckName.API_RESPONSE_SIZE:
            passed = response_size <= request.max_response_bytes
            results.append(_result(
                check,
                CheckStatus.PASSED if passed else CheckStatus.FAILED,
                f"Observed response size {response_size} bytes; limit {request.max_response_bytes} bytes.",
                {"response_size_bytes": response_size, "max_response_bytes": request.max_response_bytes},
                elapsed_ms,
            ))
        elif check == CheckName.API_JSON_ASSERTIONS:
            if parse_error:
                results.append(_result(check, CheckStatus.FAILED, parse_error, {"status_code": status_code}, elapsed_ms))
                continue
            outcomes = [evaluate_assertion(document, item) for item in request.json_assertions or []]
            failed = [item for item in outcomes if not item["passed"]]
            results.append(_result(
                check,
                CheckStatus.FAILED if failed else CheckStatus.PASSED,
                f"{len(outcomes) - len(failed)} of {len(outcomes)} JSON assertion(s) held.",
                {"assertions": outcomes},
                elapsed_ms,
                ["Only the asserted fields were inspected; values are truncated in evidence."],
            ))
        elif check == CheckName.API_JSON_SCHEMA:
            if parse_error:
                results.append(_result(check, CheckStatus.FAILED, parse_error, {"status_code": status_code}, elapsed_ms))
                continue
            from jsonschema import Draft202012Validator

            validator = Draft202012Validator(request.response_schema or {})
            errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
            evidence_errors = [
                {
                    "path": ".".join(str(part) for part in error.absolute_path) or "$",
                    "message": error.message[:300],
                }
                for error in errors[:MAX_SCHEMA_ERRORS]
            ]
            results.append(_result(
                check,
                CheckStatus.FAILED if errors else CheckStatus.PASSED,
                "The response matched the JSON Schema." if not errors
                else f"The response violated the JSON Schema in {len(errors)} place(s).",
                {"error_count": len(errors), "errors": evidence_errors},
                elapsed_ms,
                ["JSON Schema draft 2020-12; remote $ref documents are never fetched."],
            ))
        elif check == CheckName.API_JSON_SHAPE:
            if parse_error:
                results.append(_result(check, CheckStatus.FAILED, parse_error, {"status_code": status_code}, elapsed_ms))
                continue
            matches = matches_json_shape(document, request.expected_json_example)
            results.append(_result(
                check,
                CheckStatus.PASSED if matches else CheckStatus.FAILED,
                "The response JSON has the same shape as the sample." if matches
                else "The response JSON keys, types or list shape differ from the sample.",
                {"matches": matches},
                elapsed_ms,
                ["Keys and JSON types are compared; values are not."],
            ))
        elif check == CheckName.OPENAPI_CONTRACT:
            started = time.monotonic()
            try:
                contract = await contract_runner.run_contract(str(request.url))
            except Exception as exc:
                contract = {"outcome": "error", "message": f"The contract run failed ({type(exc).__name__})."}
            evidence = {key: value for key, value in contract.items() if key not in {"outcome", "message"}}
            results.append(_result(
                check,
                {"passed": CheckStatus.PASSED, "failed": CheckStatus.FAILED, "skipped": CheckStatus.SKIPPED}.get(
                    contract["outcome"], CheckStatus.ERROR),
                contract["message"],
                evidence,
                int((time.monotonic() - started) * 1000),
                ["GET operations only, 1 generated example each, 2 requests per second."],
            ))
    return results
