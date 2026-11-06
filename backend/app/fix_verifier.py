"""Run one check against a copy of a demo store, with the bug's switch on. Used by app.fixer, never imported.

Usage (from a folder holding the store's store_app.py and static/):
    python fix_verifier.py <check> [bug]
Prints one JSON object: {"ok": true when the store behaves safely, "message": what was seen}.

The store runs in-process through FastAPI's TestClient: no port is opened and no network is used.
PayPal is replaced by a fake that records every call, so the check sees what the store asked PayPal
to do. The caller runs this in a separate process with a clean environment (no keys) and a time limit.
"""

import json
import sys

sys.path.insert(0, ".")

import store_app  # noqa: E402  (the store copy in the current folder)
from fastapi.testclient import TestClient  # noqa: E402

PAYLOAD = "<script>alert('payrecover')</script>"
CART = "verify-cart-0001"
calls: list[tuple[str, str, dict | None]] = []


async def fake_paypal(method, path, body=None, request_id=None):
    calls.append((method, path, body))
    if path == "/v2/checkout/orders":
        return {"id": f"VERIFY{len(calls):010d}", "status": "CREATED"}
    if path.endswith("/authorize"):
        return {"purchase_units": [{"payments": {"authorizations": [{"id": "AUTHVERIFY1", "status": "CREATED"}]}}]}
    if "/v2/payments/authorizations/" in path and path.endswith("/capture"):
        return {"id": "CAPVERIFY2", "status": "COMPLETED"}
    if path.endswith("/capture"):
        return {"purchase_units": [{"payments": {"captures": [{"id": "CAPVERIFY1", "status": "COMPLETED"}]}}]}
    return {}


def order(client, unit_price="18.00"):
    response = client.post("/api/orders", json={"items": [{"product_id": "tee", "quantity": 1, "unit_price": unit_price}],
                                                 "cart_id": CART})
    return response.status_code, response.json()


def captured() -> bool:
    return any(path.endswith("/capture") for _, path, _ in calls)


def check_xss(client):
    body = client.get("/", params={"q": PAYLOAD}).text
    return PAYLOAD not in body, ("The search text came back escaped." if PAYLOAD not in body
                                 else "The search text came back unescaped, so a script would run.")


def check_headers(client):
    headers = {key.lower(): value for key, value in client.get("/").headers.items()}
    missing = [name for name in ("content-security-policy", "x-content-type-options") if name not in headers]
    if "x-frame-options" not in headers and "frame-ancestors" not in headers.get("content-security-policy", ""):
        missing.append("x-frame-options")
    return not missing, ("Every security header is sent." if not missing else f"Still missing: {', '.join(missing)}.")


def check_price(client):
    status, _ = order(client, unit_price="1.00")
    sent = [body for _, path, body in calls if path == "/v2/checkout/orders"]
    charged = sent[0]["purchase_units"][0]["amount"]["value"] if sent else None
    ok = charged == "18.00"
    return ok, (f"A browser sent 1.00 for an 18.00 T-shirt; the store asked PayPal for {charged}." if sent
                else f"The store refused the order (HTTP {status}).")


def check_capture(client):
    _, created = order(client)
    record = client.post(f"/api/orders/{created['id']}/capture").json()
    ok = record.get("status") != "PAID" or captured()
    return ok, ("The order is marked paid only after PayPal captured it." if ok
                else "The order was marked PAID without asking PayPal to capture it.")


def check_double(client):
    _, created = order(client)
    client.post(f"/api/orders/{created['id']}/capture")
    status, _ = order(client)
    return status == 409, ("Paying the same cart again is refused (HTTP 409)." if status == 409
                           else f"A cart that was already paid could be paid again (HTTP {status}).")


def check_hold(client):
    _, created = order(client)
    intent = next((body.get("intent") for _, path, body in calls if path == "/v2/checkout/orders"), None)
    record = client.post(f"/api/orders/{created['id']}/capture").json()
    ok = record.get("status") != "PAID" or captured()
    return ok, (f"The order (intent {intent}) is marked paid only once the money is captured." if ok
                else "The order was marked PAID while PayPal only held the money; nothing captured it.")


CHECKS = {"xss": check_xss, "headers": check_headers, "price": check_price, "capture": check_capture,
          "double": check_double, "hold": check_hold}


def main() -> None:
    check, bug = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    store_app.paypal_request = fake_paypal
    store_app.orders.clear()
    store_app.active_bugs.clear()
    if bug:
        store_app.active_bugs.add(bug)
    ok, message = CHECKS[check](TestClient(store_app.app))
    print(json.dumps({"ok": bool(ok), "message": message}))


if __name__ == "__main__":
    main()
