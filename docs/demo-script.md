# PayRecover AI: demo video script (about 2:45)

Record on localhost (no Render wake-up delays). Before recording:

- Start the backend, both stores and the dashboard; open the dashboard once so Gemini has answered a question.
- In the Vulnerable store, create the leaks below a few minutes before recording (so the "waiting" clocks show).
- Have one Sandbox dispute open (sign in to sandbox.paypal.com as the buyer and report a problem on a payment).
- Clear old recoveries (Money recovered → Clear history) so the totals start at zero.
- Screen at 1920×1080, browser zoom 110%, close other tabs. Speak slowly; cut pauses while Gemini thinks.

| Time | Screen | Say |
|---|---|---|
| 0:00 – 0:15 | Title card: "PayRecover AI" + one line | "Online stores lose money on PayPal without noticing: payments never collected, orders marked paid that PayPal never charged, carts charged twice. PayRecover AI is an agent that finds that money, proves it with PayPal's own records, and gets it back." |
| 0:15 – 0:35 | Vulnerable store page: tick "Show paid without capturing" and "Let the same cart be paid multiple times", pay once with the Sandbox buyer | "This is a demo store with real checkout bugs, paying through the PayPal Sandbox. The store says 'Payment complete', but is it?" |
| 0:35 – 1:05 | Dashboard → **Ask PayRecover**: type "Check my vulnerable store. What am I losing and what should I fix first?" Expand the tool calls | "I just ask. Gemini decides which tools to use: PayRecover's checks and PayPal's own Agent Toolkit. Every call is listed. It found the leaks, with amounts, and it can only propose a fix." |
| 1:05 – 1:25 | Click **Approve** on the proposal; show the result "recovered" | "Nothing moves until I approve. Then PayRecover captures the payment through PayPal's Server SDK and reads PayPal again to confirm the money really arrived." |
| 1:25 – 1:50 | **Scan my store** (Vulnerable): stats, AI recovery plan, grid with urgency and waiting clocks; open an **Evidence pack** | "The scan checks every paid order against PayPal and ranks the leaks: an approved payment still waiting is the most urgent. Each leak has a printable evidence pack with a fingerprint, ready for a dispute." |
| 1:50 – 2:20 | **Dispute defender** → Load disputes: facts, AI recommendation, edit the message, approve | "When a buyer opens a dispute, PayRecover reads it with PayPal's Agent Toolkit, links it to the order, re-checks it, and Gemini recommends an answer from the evidence. I edit and approve, and PayRecover confirms the change with PayPal." |
| 2:20 – 2:35 | Phone or email: the Zapier alert with the Approve link | "And I don't need the dashboard open: PayPal webhooks and Zapier alert me the moment something goes wrong, with an approve link." |
| 2:35 – 2:45 | **Money recovered** panel totals; end card with the GitHub link and "Built with PayPal Server SDK, PayPal Agent Toolkit, Gemini, APIMatic, Zapier, AG Grid" | "PayRecover AI: find the leak, prove it with PayPal, get the money back, with a human approving every move." |

## Backup lines if something is slow

- Gemini taking long: "Gemini is checking PayPal right now." (cut the wait in editing)
- A store asleep on Render: record locally instead; never wait on camera.
