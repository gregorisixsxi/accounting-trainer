# Accounting Trainer

FastAPI + SQLite + one-page `index.html` (Tailwind CDN). Live: https://accountingtrainer.app (Render). Repo: gregorisixsxi/accounting-trainer.

- **Incident / "the site is on fire":** follow `RUNBOOK.md` first. Health check: `GET /api/health`. Backup: `python scripts/backup_db.py`.
- Files: `app.py` (routes), `db.py` (SQLite), `rating.py` (Score/levels; Score is called `elo` in code and DB), `grading.py` (signed tasks, server-side grading),
  `generator.py` (Claude API), `email_service.py` (Resend), `twitter_auth.py` (X OAuth2), `exam.py`, `pool.py`, `index.html`.
- **Table task types** (the mid-term tables): named and listed in `table_types.py` (BS-COMPILE, BS-TOTALS, IS-NATURE, EVENTS-2, JOURNAL, TRIAL-BALANCE, T-ACCOUNTS).
  Fresh tasks of every type come from the generators in `scripts/build_midterm_tasks.py` (`rng -> task body`); the exemplars and their KEEP/VARY rules are in
  `midterm_templates.py`; `midterm.py` seals and marks them (Mid-term test paper: 30 minutes, auto hand-in; Balance Sheet tab: `/api/tables/*`).
  To add a type: generator + exemplar + builder in `midterm.py` + renderer `MT_RENDER[kind]` in `index.html` + a line in `table_types.py`.
  `python table_types.py` prints the catalogue with the rules. Never commit `midterm*` files to a public repo (real exam tasks).
- **PRO perks:** the AI tutor (`tutor.py`: explain mistakes, chat, study plan; all token spending and limits are in that file, see RUNBOOK section 8), the study desk (skill statistics, mistake notebook with spaced repetition, practice plan: `db.py` tables `skill_stats`, `mistakes`; endpoints `/api/study/overview`, `/api/notebook*`, `/api/tutor/*`), unlimited mid-term papers (free: `FREE_PAPERS_PER_DAY`). The tutor never gets the right answers before a task is checked (`db.has_claim`).
- Never print, log or commit secrets (`.env` is gitignored; values live in Render). Rotate a secret at its provider if it leaks.
- Check Python changes with `python -I -m py_compile <files>`; the UI is verified by running `uvicorn app:app` and driving the page.
- Reply to the owner in Russian. Do not commit or push unless asked.
