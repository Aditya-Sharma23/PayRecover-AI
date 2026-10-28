"""Canonical API models shared by website requests and run reports."""

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class CheckName(StrEnum):
    HTTP_STATUS = "http_status"
    PAGE_TITLE = "page_title"
    H1 = "h1"
    CONSOLE_ERRORS = "console_errors"
    SAME_ORIGIN_LINKS = "same_origin_links"
    SECURITY_HEADERS = "security_headers"
    TEXT_PRESENT = "text_present"
    ACCESSIBILITY = "accessibility"
    USER_FLOW = "user_flow"
    RESPONSE_TIME = "response_time"
    RESPONSE_HEADERS = "response_headers"
    BODY_NOT_EMPTY = "body_not_empty"
    KEYBOARD_FOCUS = "keyboard_focus"
    COOKIE_FLAGS = "cookie_flags"
    EXPOSED_RESOURCES = "exposed_resources"
    ACTIVE_PROBES = "active_probes"
    OPENAPI_CONTRACT = "openapi_contract"
    API_STATUS = "api_status"
    API_RESPONSE_TIME = "api_response_time"
    API_CONTENT_TYPE = "api_content_type"
    API_REDIRECTS = "api_redirects"
    API_RESPONSE_SIZE = "api_response_size"
    API_JSON_ASSERTIONS = "api_json_assertions"
    API_JSON_SCHEMA = "api_json_schema"
    API_JSON_SHAPE = "api_json_shape"


WEBSITE_CHECK_NAMES = (
    CheckName.HTTP_STATUS,
    CheckName.PAGE_TITLE,
    CheckName.H1,
    CheckName.CONSOLE_ERRORS,
    CheckName.SAME_ORIGIN_LINKS,
    CheckName.SECURITY_HEADERS,
    CheckName.TEXT_PRESENT,
    CheckName.ACCESSIBILITY,
    CheckName.USER_FLOW,
    CheckName.RESPONSE_TIME,
    CheckName.RESPONSE_HEADERS,
    CheckName.BODY_NOT_EMPTY,
    CheckName.KEYBOARD_FOCUS,
    CheckName.COOKIE_FLAGS,
    CheckName.EXPOSED_RESOURCES,
    CheckName.ACTIVE_PROBES,
    CheckName.OPENAPI_CONTRACT,
)
# One-URL "full scan": every website check that needs no extra input. Active
# probes stay opt-in, so they are not part of the preset.
FULL_SCAN_CHECKS = tuple(
    name for name in WEBSITE_CHECK_NAMES
    if name not in {CheckName.TEXT_PRESENT, CheckName.USER_FLOW, CheckName.ACTIVE_PROBES}
)
API_CHECK_NAMES = (
    CheckName.API_STATUS,
    CheckName.API_RESPONSE_TIME,
    CheckName.API_CONTENT_TYPE,
    CheckName.API_REDIRECTS,
    CheckName.API_RESPONSE_SIZE,
    CheckName.API_JSON_ASSERTIONS,
    CheckName.API_JSON_SCHEMA,
    CheckName.API_JSON_SHAPE,
    CheckName.OPENAPI_CONTRACT,
)
MAX_FLOW_STEPS = 10
MAX_JSON_ASSERTIONS = 20
MAX_REQUEST_HEADERS = 10
MAX_SCHEMA_BYTES = 20_000
# Headers the runner controls itself, or that would change the connection.
FORBIDDEN_REQUEST_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "upgrade", "te", "trailer", "proxy-authorization", "proxy-connection",
    "expect", "accept-encoding",
})
HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
KEY_NAME = re.compile(r"^[A-Za-z0-9+]{1,40}$")
JSON_PATH = re.compile(r"^[A-Za-z0-9_\-$]+(\.[A-Za-z0-9_\-$]+)*$")


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"
    ERROR = "error"


class OverallStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    ERROR = "error"
    BLOCKED = "blocked"
    INCONCLUSIVE = "inconclusive"


def _reject_credentials(value: HttpUrl) -> HttpUrl:
    parsed = urlsplit(str(value))
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing embedded credentials are not allowed")
    return value


def _require_authorization(value: bool) -> bool:
    if not value:
        raise ValueError("authorization must be confirmed before a run can be requested")
    return value


def _unique_checks(value: list["CheckName"], allowed: tuple["CheckName", ...], label: str) -> list["CheckName"]:
    if len(value) != len(set(value)):
        raise ValueError("checks must not contain duplicates")
    if any(check not in allowed for check in value):
        raise ValueError(f"{label} checks must use {label} check names")
    return value


class FlowStep(BaseModel):
    """One user-specified browser action or assertion in a multi-step flow."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["click", "fill", "press", "wait_for", "expect_text", "expect_url"]
    selector: str | None = Field(default=None, min_length=1, max_length=200)
    value: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_fields(self) -> "FlowStep":
        needs_selector = {"click", "fill", "wait_for"}
        needs_value = {"fill", "press", "expect_text", "expect_url"}
        if self.action in needs_selector and not self.selector:
            raise ValueError(f"{self.action} steps need a CSS selector")
        if self.action in needs_value and not self.value:
            raise ValueError(f"{self.action} steps need a value")
        if self.action == "press" and not KEY_NAME.fullmatch(self.value or ""):
            raise ValueError("press steps need a key name such as Enter or Control+A")
        return self


class WebsiteRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    authorization_confirmed: bool
    checks: list[CheckName] = Field(min_length=1, max_length=len(WEBSITE_CHECK_NAMES))
    max_links: int = Field(default=10, ge=0, le=25)
    timeout_seconds: int = Field(default=20, ge=3, le=60)
    max_load_time_ms: int = Field(default=5000, ge=100, le=60000)
    expected_text: str | None = Field(default=None, min_length=1, max_length=500)
    click_selector: str | None = Field(default=None, min_length=1, max_length=200)
    flow_steps: list[FlowStep] | None = Field(default=None, min_length=1, max_length=MAX_FLOW_STEPS)
    capture_screenshot_on_failure: bool = False

    @field_validator("url")
    @classmethod
    def reject_embedded_credentials(cls, value: HttpUrl) -> HttpUrl:
        return _reject_credentials(value)

    @field_validator("authorization_confirmed")
    @classmethod
    def require_authorization(cls, value: bool) -> bool:
        return _require_authorization(value)

    @field_validator("checks")
    @classmethod
    def require_unique_checks(cls, value: list[CheckName]) -> list[CheckName]:
        return _unique_checks(value, WEBSITE_CHECK_NAMES, "website")

    @model_validator(mode="after")
    def require_check_inputs(self) -> "WebsiteRunRequest":
        if CheckName.TEXT_PRESENT in self.checks and not (self.expected_text or "").strip():
            raise ValueError("expected_text is required when text_present is selected")
        if self.click_selector and CheckName.TEXT_PRESENT not in self.checks:
            raise ValueError("click_selector is only used with the text_present check")
        if CheckName.USER_FLOW in self.checks and not self.flow_steps:
            raise ValueError("flow_steps are required when user_flow is selected")
        if self.flow_steps and CheckName.USER_FLOW not in self.checks:
            raise ValueError("flow_steps are only used with the user_flow check")
        if self.flow_steps and not any(step.action in {"expect_text", "expect_url"} for step in self.flow_steps):
            raise ValueError("a user flow needs at least one expect_text or expect_url step to decide pass or fail")
        return self


class JsonAssertion(BaseModel):
    """An explicit, user-written condition on one field of a JSON response."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=200)
    operator: Literal["exists", "not_exists", "equals", "not_equals", "contains", "type", "length"]
    expected: str | int | float | bool | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if value != "$" and not JSON_PATH.fullmatch(value):
            raise ValueError("path must be dot-separated keys or list indexes, for example data.items.0.id")
        return value

    @model_validator(mode="after")
    def require_expected(self) -> "JsonAssertion":
        if self.operator in {"type"} and self.expected not in {
            "string", "number", "integer", "boolean", "null", "object", "array",
        }:
            raise ValueError("type assertions expect string, number, integer, boolean, null, object or array")
        if self.operator == "length" and (isinstance(self.expected, bool) or not isinstance(self.expected, int)):
            raise ValueError("length assertions expect an integer")
        if self.operator == "contains" and not isinstance(self.expected, str):
            raise ValueError("contains assertions expect a string")
        return self


class ApiRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    authorization_confirmed: bool
    checks: list[CheckName] = Field(min_length=1, max_length=len(API_CHECK_NAMES))
    expected_status_code: int = Field(default=200, ge=100, le=599)
    expected_content_type: str | None = Field(default=None, min_length=1, max_length=200)
    max_response_time_ms: int = Field(default=5000, ge=1, le=60000)
    max_response_bytes: int = Field(default=2_000_000, ge=1, le=2_000_000)
    timeout_seconds: int = Field(default=20, ge=3, le=60)
    request_headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_REQUEST_HEADERS)
    json_assertions: list[JsonAssertion] | None = Field(default=None, min_length=1, max_length=MAX_JSON_ASSERTIONS)
    response_schema: dict[str, Any] | None = None
    expected_json_example: Any = None

    @field_validator("url")
    @classmethod
    def reject_embedded_credentials(cls, value: HttpUrl) -> HttpUrl:
        return _reject_credentials(value)

    @field_validator("authorization_confirmed")
    @classmethod
    def require_authorization(cls, value: bool) -> bool:
        return _require_authorization(value)

    @field_validator("checks")
    @classmethod
    def require_api_checks(cls, value: list[CheckName]) -> list[CheckName]:
        return _unique_checks(value, API_CHECK_NAMES, "API")

    @field_validator("request_headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, header_value in value.items():
            if not HEADER_NAME.fullmatch(name) or name.lower() in FORBIDDEN_REQUEST_HEADERS:
                raise ValueError(f"request header {name!r} is not allowed")
            if len(header_value) > 2000 or any(ch in header_value for ch in "\r\n\0"):
                raise ValueError(f"request header {name!r} has an invalid value")
        return value

    @field_validator("response_schema")
    @classmethod
    def validate_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        if len(json.dumps(value)) > MAX_SCHEMA_BYTES:
            raise ValueError("response_schema must be smaller than 20 KB")
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError(f"response_schema is not a valid JSON Schema: {exc.message}") from exc
        return value

    @model_validator(mode="after")
    def require_check_inputs(self) -> "ApiRunRequest":
        if CheckName.API_CONTENT_TYPE in self.checks and not self.expected_content_type:
            raise ValueError("expected_content_type is required when api_content_type is selected")
        if CheckName.API_JSON_ASSERTIONS in self.checks and not self.json_assertions:
            raise ValueError("json_assertions are required when api_json_assertions is selected")
        if CheckName.API_JSON_SCHEMA in self.checks and not self.response_schema:
            raise ValueError("response_schema is required when api_json_schema is selected")
        if CheckName.API_JSON_SHAPE in self.checks and self.expected_json_example is None:
            raise ValueError("expected_json_example is required when api_json_shape is selected")
        if self.expected_json_example is not None and len(json.dumps(self.expected_json_example)) > MAX_SCHEMA_BYTES:
            raise ValueError("expected_json_example must be smaller than 20 KB")
        return self


class TargetPreflightRequest(BaseModel):
    """Request a DNS and URL policy preflight only; this does not run checks."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    authorization_confirmed: bool

    @field_validator("authorization_confirmed")
    @classmethod
    def require_authorization(cls, value: bool) -> bool:
        if not value:
            raise ValueError("authorization must be confirmed before checking a target")
        return value

    @field_validator("url")
    @classmethod
    def reject_embedded_credentials(cls, value: HttpUrl) -> HttpUrl:
        return _reject_credentials(value)


class OpenApiImportRequest(BaseModel):
    """An OpenAPI document to turn into reviewable API check suggestions."""

    model_config = ConfigDict(extra="forbid")

    spec: str = Field(min_length=2, max_length=500_000)
    base_url: HttpUrl | None = None

    @field_validator("base_url")
    @classmethod
    def reject_embedded_credentials(cls, value: HttpUrl | None) -> HttpUrl | None:
        return value if value is None else _reject_credentials(value)


class OpenApiOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    method: str
    path: str
    summary: str
    url: str | None
    runnable: bool
    reason: str | None = None
    expected_status_code: int | None = None
    expected_content_type: str | None = None
    response_schema: dict[str, Any] | None = None


class OpenApiImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    version: str
    base_url: str | None
    operations: list[OpenApiOperation]
    warnings: list[str] = Field(default_factory=list)


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: CheckName
    status: CheckStatus
    duration_ms: int | None = Field(default=None, ge=0)
    message: str = Field(min_length=1, max_length=2000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class SummaryCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class WebsiteRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    run_id: UUID
    target_url: HttpUrl
    started_at: datetime
    finished_at: datetime
    overall_status: OverallStatus
    summary: SummaryCounts
    checks: list[CheckResult]
    warnings: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def verify_summary(self) -> "WebsiteRunReport":
        expected = {
            "passed": sum(c.status == CheckStatus.PASSED for c in self.checks),
            "failed": sum(c.status == CheckStatus.FAILED for c in self.checks),
            "warnings": sum(c.status == CheckStatus.WARNING for c in self.checks),
            "errors": sum(c.status == CheckStatus.ERROR for c in self.checks),
            "skipped": sum(c.status == CheckStatus.SKIPPED for c in self.checks),
        }
        actual = self.summary.model_dump()
        if actual != expected:
            raise ValueError(f"summary counts {actual} do not match check records {expected}")
        return self

    @model_validator(mode="after")
    def verify_time_order(self) -> "WebsiteRunReport":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        return self

    @model_validator(mode="after")
    def verify_overall_status(self) -> "WebsiteRunReport":
        statuses = [check.status for check in self.checks]
        if not statuses or all(status == CheckStatus.SKIPPED for status in statuses):
            expected = OverallStatus.BLOCKED
        elif CheckStatus.FAILED in statuses:
            expected = OverallStatus.FAILED
        elif CheckStatus.ERROR in statuses:
            expected = OverallStatus.ERROR
        elif CheckStatus.WARNING in statuses or CheckStatus.SKIPPED in statuses:
            expected = OverallStatus.WARNING
        else:
            expected = OverallStatus.PASSED
        if self.overall_status != expected:
            raise ValueError(f"overall_status {self.overall_status} does not match check records ({expected})")
        return self


class ApiRunReport(WebsiteRunReport):
    """Evidence report for one bounded API request."""


class JobType(StrEnum):
    WEBSITE = "website"
    API = "api"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: JobType
    website: WebsiteRunRequest | None = None
    api: ApiRunRequest | None = None

    @model_validator(mode="after")
    def require_matching_request(self) -> "JobRequest":
        if self.job_type == JobType.WEBSITE and self.website is None:
            raise ValueError("website request is required for a website job")
        if self.job_type == JobType.API and self.api is None:
            raise ValueError("api request is required for an API job")
        if self.job_type == JobType.WEBSITE and self.api is not None:
            raise ValueError("api request is not allowed for a website job")
        if self.job_type == JobType.API and self.website is not None:
            raise ValueError("website request is not allowed for an API job")
        return self


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    job_type: JobType
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    report_id: UUID | None = None
    error: str | None = None
    target_url: str | None = None


class CheckChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    base_status: str | None
    head_status: str | None
    change: Literal["fixed", "regressed", "changed", "unchanged", "added", "removed"]


class RunComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_run_id: UUID
    head_run_id: UUID
    base_status: str
    head_status: str
    same_target: bool
    changes: list[CheckChange]
    regressions: int
    fixes: int


class GemmaPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=2000)
    target_url: HttpUrl | None = None
    test_type: str = Field(default="website", pattern="^(website|api)$")
    authorized: bool

    @field_validator("authorized")
    @classmethod
    def require_authorization(cls, value: bool) -> bool:
        if not value:
            raise ValueError("authorization must be confirmed before planning a test")
        return value


class GemmaPlan(BaseModel):
    """Gemma's reviewable proposal. Suggested inputs are drafts the user must confirm."""

    model_config = ConfigDict(extra="ignore")

    goal: str
    test_type: str = Field(pattern="^(website|api)$")
    checks: list[CheckName] = Field(min_length=1, max_length=len(WEBSITE_CHECK_NAMES))
    expectations: list[str] = Field(min_length=1, max_length=10)
    limitations: list[str] = Field(max_length=10)
    rationale: str = Field(min_length=1, max_length=2000)
    suggested_expected_text: str | None = Field(default=None, max_length=500)
    suggested_flow_steps: list[FlowStep] | None = Field(default=None, max_length=MAX_FLOW_STEPS)
    suggested_json_assertions: list[JsonAssertion] | None = Field(default=None, max_length=MAX_JSON_ASSERTIONS)


class GemmaExplanationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: WebsiteRunReport | ApiRunReport


class GemmaExplanation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(min_length=1, max_length=2000)
    findings: list[str] = Field(max_length=10)
    limitations: list[str] = Field(max_length=10)
    next_steps: list[str] = Field(max_length=10)
    risk_level: Literal["HIGH", "REVIEW", "LOW", "UNKNOWN"] = "UNKNOWN"
    source: str = "Gemma"
