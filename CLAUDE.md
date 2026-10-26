# Accounting Trainer

FastAPI + SQLite + one-page `index.html` (Tailwind CDN). Live: https://accountingtrainer.app (Render). Repo: gregorisixsxi/accounting-trainer.

- **Incident / "the site is on fire":** follow `RUNBOOK.md` first. Health check: `GET /api/health`. Backup: `python scripts/backup_db.py`.
- Files: `app.py` (routes), `db.py` (SQLite), `rating.py` (Score/levels; Score is called `elo` in code and DB), `grading.py` (signed tasks, server-side grading),
  `generator.py` (Claude API), `email_service.py` (Resend), `twitter_auth.py` (X OAuth2), `exam.py`, `pool.py`, `index.html`.
- Never print, log or commit secrets (`.env` is gitignored; values live in Render). Rotate a secret at its provider if it leaks.
- Check Python changes with `python -I -m py_compile <files>`; the UI is verified by running `uvicorn app:app` and driving the page.
- Reply to the owner in Russian. Do not commit or push unless asked.
