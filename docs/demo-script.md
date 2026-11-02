# PayRecover AI: demo video script (about 2:50)

Record on localhost (no Render wake-up delays). Before recording:

- Start the backend and all three stores (Main, Vulnerable, Risky); open the dashboard once so Gemini has answered.
- In the Vulnerable store, create the leaks below a few minutes before recording (so the "waiting" clocks show).
- Have one Sandbox dispute open (sign in to sandbox.paypal.com as the buyer and report a problem on a payment).
- Clear old recoveries (Money recovered → Clear history) so the totals start at zero.
- Turn **Auto-scan** on (every 5 min) a few minutes early, so the Timeline already has coloured scans.
- Screen at 1920×1080, browser zoom 110%, close other tabs. Speak slowly; cut pauses while Gemini thinks.

| Time | Screen | Say |
|---|---|---|
| 0:00 – 0:12 | Title card: "PayRecover AI" + one line | "Online stores lose money on PayPal without noticing: payments never collected, orders marked paid that PayPal never charged, carts charged twice. PayRecover AI is an agent that finds that money, proves it with PayPal's own records, and gets it back." |
| 0:12 – 0:30 | Vulnerable store page: tick "Show paid without capturing" and "Let the same cart be paid multiple times", pay once with the Sandbox buyer | "This is a demo store with real checkout bugs, paying through the PayPal Sandbox. The store says 'Payment complete', but is it?" |
| 0:30 – 0:50 | Dashboard top: the four cards, then click each **demo store** card: Main turns 🟢, Risky 🟠, Vulnerable 🔴 | "Three demo stores, three answers. Main: every payment matches PayPal. Risky: payments are fine, but its website has holes that could leak money. Vulnerable: money is leaking right now." |
| 0:50 – 1:15 | **Ask PayRecover**: "Check my vulnerable store. What am I losing and what should I fix first?" Expand the tool calls | "I just ask. Gemini decides which tools to use: PayRecover's checks and PayPal's own Agent Toolkit. Every call is listed. It found the leaks, with amounts, and it can only propose a fix." |
| 1:15 – 1:32 | Click **Approve** on the proposal; show "recovered" and **Money at risk** dropping | "Nothing moves until I approve. Then PayRecover captures the payment through PayPal's Server SDK and reads PayPal again to confirm the money really arrived." |
| 1:32 – 1:50 | **Check payments → Whole store** (Vulnerable): red banner with the holes and fixes, AI plan, grid; open an **Evidence pack** | "The scan ranks the leaks by urgency, shows the hole behind them, and each leak has a printable evidence pack with a fingerprint, ready for a dispute." |
| 1:50 – 2:15 | **Dispute defender** → Load disputes: facts, AI recommendation, edit the message, approve | "When a buyer opens a dispute, PayRecover reads it with PayPal's Agent Toolkit, links it to the order, re-checks it, and Gemini recommends an answer from the evidence. I edit and approve, and PayRecover confirms the change with PayPal." |
| 2:15 – 2:30 | **Timeline** (Bryntum Scheduler): coloured scans per store, recoveries, the dispute bar running to its deadline | "The timeline shows what happened and what's due: every scan, every recovery, and each dispute running to PayPal's deadline, red when it's close." |
| 2:30 – 2:42 | Top bar **Auto-scan on**; then phone or inbox: the 🔴 email with the Approve link | "And I don't need the dashboard open. Auto-scan checks every store on a timer, and the moment something changes I get an email or a Zapier alert, with an approve link." |
| 2:42 – 2:50 | **Money recovered** totals; end card with the GitHub link and "Built with PayPal Server SDK, PayPal Agent Toolkit, Gemini, APIMatic, AG Grid, Bryntum, Zapier" | "PayRecover AI: find the leak, prove it with PayPal, get the money back, with a human approving every move." |

## Backup lines if something is slow

- Gemini taking long: "Gemini is checking PayPal right now." (cut the wait in editing)
- A store asleep on Render: record locally instead; never wait on camera.
- No 🔴 email yet: show the Alerts feed on the dashboard instead; it lists the same alert with how it was delivered.
