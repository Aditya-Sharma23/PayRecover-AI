"""Discover a same-origin OpenAPI schema and run bounded GET-only Schemathesis checks.

Discovery goes through the pinned probe client. Schemathesis itself runs as a
subprocess with one example per operation, one worker, a rate limit, no
redirects and a hard time limit.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from app import security_checks
from app.url_policy import UnsafeTarget, validate_target_url

MAX_SCHEMA_BYTES = 512_000
SCHEMA_PATHS = ("openapi.json", "swagger.json", "api/openapi.json", "v3/api-docs")
RUN_TIMEOUT_SECONDS = 25


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def schema_candidates(target: str) -> list[str]:
    parts = urlsplit(target)
    origin = _origin(target) + "/"
    candidates = []
    if parts.path.endswith(".json"):
        candidates.append(target)
    parent = parts.path.rsplit("/", 1)[0].strip("/")
    if parent:
        candidates.append(urljoin(origin, f"{parent}/openapi.json"))
    candidates.extend(urljoin(origin, path) for path in SCHEMA_PATHS)
    return list(dict.fromkeys(candidates))


def has_remote_refs(value) -> bool:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#/"):
            return True
        return any(has_remote_refs(item) for item in value.values())
    if isinstance(value, list):
        return any(has_remote_refs(item) for item in value)
    return False


async def discover_schema(target: str) -> tuple[str | None, dict | None]:
    for url in schema_candidates(target):
        try:
            response = await security_checks.fetch_probe(url, "GET", MAX_SCHEMA_BYTES + 1)
        except Exception:  # unreachable, blocked or malformed candidates are simply skipped
            continue
        if response.status != 200 or "json" not in response.headers.get("content-type", "").lower():
            continue
        if len(response.body) > MAX_SCHEMA_BYTES:
            continue
        try:
            schema = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(schema, dict) and (
            str(schema.get("openapi", "")).startswith("3.") or schema.get("swagger") == "2.0"
        ):
            return url, schema
    return None, None


def _get_operations(schema: dict) -> int:
    return sum(
        1 for item in (schema.get("paths") or {}).values() if isinstance(item, dict)
        for method in item if method.lower() == "get"
    )


async def _base_url(schema: dict, target: str) -> str | None:
    origin = _origin(target)
    servers = schema.get("servers") or []
    server = servers[0].get("url") if servers and isinstance(servers[0], dict) else None
    if server and "{" in server:
        return None
    base = urljoin(origin + "/", server or "/")
    if _origin(base) != origin:
        return None
    await validate_target_url(base)
    return base.rstrip("/") or origin


def _cli() -> str | None:
    found = shutil.which("st") or shutil.which("schemathesis")
    if found:
        return found
    scripts = Path(sys.executable).parent
    for name in ("st.exe", "schemathesis.exe", "st", "schemathesis"):
        if (scripts / name).is_file():
            return str(scripts / name)
    return None


def _run_cli(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS, check=False,
                          env={**os.environ, "NO_COLOR": "1"})


async def run_contract(target: str) -> dict:
    """Return {'outcome': passed|failed|skipped|error, 'message': str, ...evidence}."""
    schema_url, schema = await discover_schema(target)
    if not schema:
        return {"outcome": "skipped", "message": "No OpenAPI JSON schema was found at the standard same-origin locations."}
    get_count = _get_operations(schema)
    evidence = {"schema_url": schema_url, "get_operations": get_count, "method_scope": "GET only"}
    if not get_count:
        return {"outcome": "skipped", "message": "The schema has no GET operations; only read-only operations are tested.", **evidence}
    if has_remote_refs(schema):
        return {"outcome": "skipped", "message": "The schema references external documents, which are never fetched.", **evidence}
    try:
        base_url = await _base_url(schema, target)
    except UnsafeTarget:
        base_url = None
    if not base_url:
        return {"outcome": "skipped", "message": "The schema declares an off-origin or templated server URL; it was not followed.", **evidence}
    cli = _cli()
    if not cli:
        return {"outcome": "error", "message": "The Schemathesis CLI is not installed on this server.", **evidence}
    with tempfile.TemporaryDirectory(prefix="testpilot-schema-") as folder:
        schema_path = Path(folder) / "openapi.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [
            cli, "run", str(schema_path), "--url", base_url, "--include-method", "GET",
            "--max-examples", "1", "--workers", "1", "--rate-limit", "2/s",
            "--max-redirects", "0", "--request-timeout", "3", "--no-color",
        ]
        try:
            completed = await asyncio.to_thread(_run_cli, command)
        except subprocess.TimeoutExpired:
            return {"outcome": "error", "message": f"The contract run exceeded its {RUN_TIMEOUT_SECONDS}-second limit.", **evidence}
        except OSError as exc:
            return {"outcome": "error", "message": f"Schemathesis could not start ({type(exc).__name__}).", **evidence}
    evidence.update({"base_url": base_url, "return_code": completed.returncode, "max_examples_per_operation": 1})
    tail = [line for line in (completed.stdout or "").splitlines() if line.strip()][-8:]
    evidence["output_tail"] = [line[:200] for line in tail]
    if completed.returncode == 0:
        return {"outcome": "passed", "message": f"Schemathesis checks passed for {get_count} documented GET operation(s).", **evidence}
    if completed.returncode == 1:
        return {"outcome": "failed", "message": f"Schemathesis reported contract problems across up to {get_count} GET operation(s).", **evidence}
    return {"outcome": "error", "message": "Schemathesis could not complete the discovered schema checks.", **evidence}
