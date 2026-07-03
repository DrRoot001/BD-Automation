# BD Automator — Stakeholder Demo Runbook

Everything below was verified working end-to-end on 2026-07-03 (branch `our-fixes-merged`).

---

## 1. Start the stack (15 min before the demo)

**Recommended — production frontend (much snappier page loads):**
```bash
cd frontend && npm run build && cd ..     # do this BEFORE starting dev.sh — never build while the dev server is running
FRONTEND_PROD=1 ./dev.sh
```

**Fallback — plain dev mode:**
```bash
./dev.sh
```
If you use dev mode, click through every page once before the audience arrives — Next.js compiles each route on first visit (5–15 s the first time, fast afterwards).

Sanity check: `http://localhost:8002/api/health` returns `{"status":"ok"}` and `http://localhost:3000/login` renders.

## 2. Demo logins

| Login | Role | Sees |
|---|---|---|
| `demo-admin@bdautomator.dev` | Admin | Admin console, all candidates/applications, discovery & import |
| `demo@bdautomator.dev` | BD User | BD dashboard (owns no candidates — assign one first or use a real BD account) |

Password for both: `DemoStakeholder2026!`

These are real Supabase users created for the demo. Delete them afterwards in Supabase → Authentication → Users.

## 3. Clean demo data (optional, before the demo)

873 of 889 applications in the DB are `FAILED` — mostly `INFRA_ERROR` rows from June dev testing. They make the admin feed a wall of red. To remove them (dry-run first):

```bash
cd backend
PYTHONPATH=..:. python3 -m app.scripts.demo_cleanup --infra          # dry-run, shows counts
PYTHONPATH=..:. python3 -m app.scripts.demo_cleanup --infra --apply  # actually deletes
```

Without `--infra` it only removes the 5 "(untitled — ad-hoc test run)" placeholder rows.

## 4. Suggested demo script

1. **Login as admin** → Admin Console: global KPIs, live activity feed (WebSocket — point out updates arrive without refresh).
2. **Job Discovery** (`/admin/discovery`): show the scraper console. *Run the actual scrape ~30 min before the demo* so fresh jobs are already ingested — don't gamble on live scraping against job boards mid-presentation.
3. **Candidates** → open the candidate profile: parsed resume, tech stack, match history.
4. **The wow moment — Auto-Apply:** on the candidate page, trigger Auto-Apply (small `max_apps`, e.g. 2). It returns instantly and streams pipeline progress live: scoring (Gemini) → resume tailoring → cover letter → browser automation. Status changes push to the dashboard over WebSocket in real time.
5. **Applications tracker** → open an application: tailored resume PDF, cover letter, status history timeline.
6. **Interviews** (Module 5): shows Gmail-detected interview invites.

## 5. Known constraints — don't trip on these live

- **Failed applications now retry automatically.** A job whose application FAILED for a transient reason (`INFRA_ERROR`, `EMAIL_VERIFICATION`, unknown) is re-selected on the next Auto-Apply run and its record is reset — no manual cleanup needed. Terminal failures (`JOB_EXPIRED`, `BOT_DETECTED`, `LOGIN_REQUIRED`, `ROBOTS_BLOCKED`) stay excluded.
- **Dice jobs need a login session.** `DICE_EMAIL` / `DICE_PASSWORD` are not set in `backend/.env` — until they are (then run `backend/app/scripts/bootstrap_dice_session.py`), Dice applications will fail with `LOGIN_REQUIRED`. Dice is the largest source in the pool, so either add creds or steer the demo toward remoterocketship/greenhouse/lever jobs.

- **Fit gate is 75/100.** Jobs scoring below it stop at `ANALYZED` (by design). For a guaranteed on-stage application, pick a job you know fits the candidate's resume well. Score distribution in the current DB runs 5–97, so good fits do pass.
- **Matching excludes jobs whose company contains** `test`, `demo`, `sample`, `example`, or has `source='manual'` / empty source URL. Seed demo jobs with realistic company names.
- **Admin "Run Match" button is synchronous** — it blocks until all jobs are scored/tailored (minutes for uncached jobs). Prefer the candidate-page Auto-Apply (asynchronous, streams progress). Run-matching is also rate-limited to 5/min.
- **Module 5 / interviews:** the Gmail token for candidate `cac4cc9b…` (Sabih Haider) is expired — reconnect Gmail from the candidate page before demoing interview detection.
- **A full browser-automation run takes 2–6 min** per application and up to 2 can run concurrently. Fill the gap by showing the tailored PDFs / activity feed while it runs.
- **Never run `npm run build` while the dev frontend is running** — they share `.next` and it breaks the running server (restart fixes it).

## 6. If something goes wrong live

- **Page hangs on skeletons** → refresh once; first-load fetches take 2–4 s (Supabase + Upstash are remote).
- **Application stuck in QUEUED/STARTED** → the watchdog auto-fails it within ~15 min; or use the admin "Fail Stuck" button.
- **Anything else** → `Ctrl+C` on dev.sh, run it again. Startup is ~30 s and it kills orphaned workers itself.
- Logs live in `./logs/` (`backend.log`, `celery-worker.log`, `frontend.log`).
