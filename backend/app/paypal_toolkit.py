"""PayPal's Agent Toolkit (paypal-agent-toolkit): PayPal's own AI tools, run against the Sandbox.

The toolkit gives each PayPal action a name, a description written for AI models, and a
parameter schema. PayRecover uses it two ways: its own code calls the toolkit for disputes and
transactions, and the PayRecover assistant hands the read-only tools to Gemini to call.

Only the toolkit's framework-free core (paypal_agent_toolkit.shared) is used; it is installed
with `pip install --no-deps -r requirements-toolkit.txt`, because its OpenAI, LangChain and
CrewAI adapters are not needed. The toolkit is synchronous, so each call runs on a thread.
"""

import asyncio
import json
from functools import lru_cache
from typing import Any

from app import paypal

# Tools the assistant may call on its own: each one only reads from PayPal.
READ_ONLY_TOOLS = (
    "get_order_details",
    "list_disputes",
    "get_dispute",
    "list_transactions",
    "list_invoices",
    "get_invoice",
)


class ToolkitUnavailable(RuntimeError):
    """paypal-agent-toolkit is not installed."""


class ToolkitError(RuntimeError):
    """PayPal refused a toolkit call; status and issue say why."""

    def __init__(self, message: str, status: int | None = None, issue: str | None = None):
        super().__init__(message)
        self.status = status
        self.issue = issue


def _toolkit():
    try:
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context
        from paypal_agent_toolkit.shared.tools import tools
    except ImportError as exc:
        raise ToolkitUnavailable(
            "PayPal's Agent Toolkit is not installed. Run: pip install --no-deps -r requirements-toolkit.txt"
        ) from exc
    return PayPalAPI, Context, tools


def available() -> bool:
    try:
        _toolkit()
    except ToolkitUnavailable:
        return False
    return True


@lru_cache(maxsize=4)
def _api(client_id: str, secret: str):
    PayPalAPI, Context, _ = _toolkit()
    # Sandbox only, like the rest of PayRecover.
    return PayPalAPI(client_id, secret, Context(sandbox=True, source="PAYRECOVER"))


def _tool(method: str) -> dict[str, Any]:
    _, _, tools = _toolkit()
    for tool in tools:
        if tool.get("method") == method:
            return tool
    raise ToolkitError(f"PayPal's Agent Toolkit has no tool named {method}.")


def _error(exc: Exception, method: str) -> ToolkitError:
    response = getattr(exc, "response", None)
    if response is None:
        return ToolkitError(f"PayPal could not complete {method}: {exc}")
    try:
        body = response.json()
        details = body.get("details") or [{}]
        issue = str(details[0].get("issue") or body.get("name") or "")
    except ValueError:
        issue = ""
    return ToolkitError(f"PayPal refused {method} (HTTP {response.status_code}{', ' + issue if issue else ''}).",
                        response.status_code, issue or None)


async def run(method: str, params: dict[str, Any]) -> Any:
    """Run one toolkit tool and return PayPal's JSON."""
    client_id, secret = paypal._credentials()
    api = _api(client_id, secret)

    def call() -> Any:
        try:
            return api.run(method, params)
        except ToolkitError:
            raise
        except ValueError as exc:  # the toolkit's own parameter checks
            raise ToolkitError(f"{method}: {exc}") from exc
        except Exception as exc:  # requests errors from PayPal, raised by the toolkit
            raise _error(exc, method) from exc

    result = await asyncio.to_thread(call)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"text": result}
    return result


def declarations(methods: tuple[str, ...] = READ_ONLY_TOOLS) -> list[dict[str, Any]]:
    """Gemini function declarations for toolkit tools, using the toolkit's own descriptions and schemas."""
    found = []
    for method in methods:
        tool = _tool(method)
        found.append({
            "name": method,
            "description": f"[PayPal Agent Toolkit] {tool['description']}",
            "parametersJsonSchema": tool["args_schema"].model_json_schema(),
        })
    return found
