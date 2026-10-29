# PayPal Sandbox setup

PayTest uses PayPal **Sandbox only**. The API base `https://api-m.sandbox.paypal.com` is hard-coded in both
the Main store and the verifier, so no setting can point either at live payments.

## 1. Create the Sandbox credentials (once)

1. Sign in at https://developer.paypal.com with any PayPal account.
2. **Apps & Credentials** → make sure the **Sandbox** toggle is selected → **Create App**
   (type: Merchant). Copy the **Client ID** and **Secret**.
3. **Testing Tools → Sandbox Accounts**: PayPal creates a default *Personal* (buyer) and *Business*
   (merchant) account. Open the Personal account → **View/Edit account** to see its email and password.
   You log in with that buyer account in the PayPal popup during checkout.

The client id is public (the browser needs it for the PayPal buttons). The secret must stay on the server.

**Developer account registered in India?** PayPal creates the default Sandbox accounts in India, and
PayPal does not support India-to-India payments: the checkout popup only says "Things don't appear to be
working at the moment". An account's country cannot be changed, so instead:

1. **Sandbox Accounts → Create account → Business**, Country/Region **United States**.
2. **Apps & Credentials → Create App** (Merchant) and pick that US business account as its Sandbox
   account. Use this app's Client ID and Secret below.
3. Pay with a Personal buyer account. A US one is safest; the default Indian buyer also works once the
   seller is in the US. PayPal may show the price in INR, but the order is charged in USD.

## 2. Configure

`main-store/.env` (the store being tested):

```
PAYPAL_CLIENT_ID=<sandbox client id>
PAYPAL_CLIENT_SECRET=<sandbox secret>
```

`backend/.env` (PayTest, which reads orders back from PayPal to verify them):

```
TESTPILOT_PAYPAL_CLIENT_ID=<sandbox client id>
TESTPILOT_PAYPAL_CLIENT_SECRET=<sandbox secret>
```

Using the same Sandbox app for both is fine: an app can only read orders it created.

## 3. Run

```powershell
cd main-store
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn store_app:app --port 8100
```

Open http://127.0.0.1:8100, pay with the Sandbox buyer account, and note the order id on the page.

Then ask PayTest what PayPal itself recorded (backend running on port 8000):

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/paypal/verify -H "Content-Type: application/json" `
  -d '{"order_id": "<order id>", "expected_amount": "18.00"}'
```

`verified: true` means PayPal reports the order `COMPLETED`, a completed capture exists, and the order
and capture amounts both equal the expected amount. Each check carries its evidence; payer and payee
details are never copied into it.

## Store endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/products` | Server-side catalog (the only source of prices) |
| `POST /api/orders` | Prices the cart and creates the PayPal order |
| `POST /api/orders/{id}/capture` | Captures the approved order (idempotent via `PayPal-Request-Id`) |
| `POST /api/orders/{id}/cancel` | Records a buyer cancellation |
| `GET /api/orders/{id}` | What the **store believes** happened, for comparison with PayPal |

Every response carries the security headers (a CSP that allows only PayPal's checkout origins, `nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`), every error code is in the OpenAPI
schema, and orders are saved to `main-store/data/orders.json` (gitignored) so they survive a restart.

## Not done yet

- Playwright scenarios that drive the checkout, including logging in to PayPal's Sandbox popup (roadmap days 4–5).
- Deliberately vulnerable stores for demonstrations are not built yet; the Main store has no switchable bugs.
