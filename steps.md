# Steps for you (things Claude can't do for you)

These need your accounts, your judgement, or the event's own instructions. The event is **Sunday 25 October 2026, 10:00–17:00 IST**. All code work is done; nothing has been committed yet.

## 1. Make sure the old Google key is revoked
An earlier key was once pasted into `backend/.env.example` (since removed). In Google AI Studio → API keys, delete any key you no longer use. **Done when** only the key in `backend/.env` is active and `http://127.0.0.1:8000/api/ai/status` shows `"configured": true`.

## 2. Commit and push to a public GitHub repository
```powershell
cd "C:\Users\ojasva koolwal\OneDrive\Desktop\projects\TestPilotAI"
git status                 # backend/.env and backend/data must NOT be listed
git add .
git commit -m "TestPilot AI 1.0: evidence-first website and API testing with Gemma 4"
git branch -M main
git remote add origin https://github.com/<you>/testpilot-ai.git
git push -u origin main
```
**Done when** the **Actions** tab shows "Validate TestPilot AI" green for backend, frontend and docker. CI has never run on GitHub, so send me the log if a job fails. (Or ask me to commit for you.)

## 3. Turn on the demo site (GitHub Pages)
Repository **Settings → Pages → Source: GitHub Actions**. Then re-run the "Publish demo site" workflow (Actions tab → Run workflow). **Done when** `https://<you>.github.io/testpilot-ai/` shows "Sign in to the demo" and `…/broken.html` loads.

## 4. Graphify (event requirement)
`docs/codebase-graph.json` comes from our script (`python scripts/generate_graph.py`, 62 nodes / 81 edges), not the official tool. Follow the Graphify instructions on the event page (https://events.mlh.com/events/15448-hacktoberfest-hack-day-jaipur-x-hack-it-sapiens), run it on the repo and post the graph in the Graphify Discord.

## 5. Confirm how the event wants Gemma used
We call **Gemma 4** (`gemma-4-26b-a4b-it`) through Google's API. If local inference is required, set `TESTPILOT_GEMMA_PROVIDER=ollama` and `TESTPILOT_GEMMA_MODEL=<your Ollama Gemma tag>` in `backend/.env` (Ollama must be running) — no code change needed.

## 6. Rehearse the demo (about 5 minutes)
Start: `cd backend; python -m uvicorn app.main:app --port 8000` and `cd frontend; npm run dev`, open http://127.0.0.1:5173. Or `docker compose up --build` and open http://127.0.0.1:8000. Use **your hosted demo site** as the target.

| # | Scenario | What to do | Expected real result |
|---|---|---|---|
| 1 | Gemma plans a flow | Goal: "Fill the email field with ada@example.com, click #sign-in-button and check that Password is required appears". Tick permission → **Ask Gemma** → **Use this plan** | Gemma proposes `user_flow` with fill / click / expect_text steps filled into the editor, with its guesses labelled |
| 2 | Flow passes | Check destination → Run selected checks | `user_flow` passed, each step shown green; the typed email is not stored, only its length |
| 3 | Real failures + evidence | URL `…/broken.html`; checks Main heading, Browser console, **Accessibility**; tick screenshot | `failed`: 0 H1s, a console error, critical axe violations (image-alt, button-name), a screenshot |
| 4 | AI explains | **Explain with Gemma**, then **Open HTML report** | Advisory explanation; status unchanged; self-contained HTML report |
| 5 | Regression view | In history tick the passing and failing runs → **Compare selected** | Per-check fixed / regressed table |
| 6 | API + OpenAPI | API tab → Import → paste https://petstore3.swagger.io/api/v3/openapi.json contents → **Use** on `GET /store/inventory`; or `https://httpbin.org/json` with JSON field `slideshow.slides length 2` | Real status, content type, schema / assertion results |

Record a short screen video as a backup in case the venue Wi-Fi fails.

## 7. Pitch
Use `TestPilot_AI_Workflow_and_Diagrams_Guide.docx` §11. Key lines: Gemma plans and explains but never decides; real Chromium and HTTP runners decide; SSRF-safe by construction (pinned connections, browser with no network of its own, read-only tests); open source (MIT); one-container deploy that refuses to start unsafely.

## 8. Optional: deploy publicly
Follow `docs/deployment.md` (Render blueprint included: New → Blueprint, then set `TESTPILOT_PUBLIC_URL` and `TESTPILOT_GOOGLE_API_KEY`). Check `/api/deployment/readiness` shows `"ready": true`, then share the URL and token only with people you trust. Render's persistent disk needs a paid plan.

## At the event
Check the submission process (Devpost or similar) and submit the GitHub link, demo video and Graphify post.
