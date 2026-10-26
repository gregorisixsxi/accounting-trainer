# RUNBOOK: "everything is on fire"

Live site: https://accountingtrainer.app (Render service `accounting-trainer`, repo `gregorisixsxi/accounting-trainer`).

## 0. The first 2 minutes (do this before anything else)

1. **Is it really down?** Open https://accountingtrainer.app/api/health
   - `200` and `"ok": true` -> the server and the database are alive; the problem is somewhere specific (go to section 2).
   - `503` or no answer -> the server or the database is down (section 1).
   - Takes ~1 minute to answer the first time -> Render free plan woke up; that is not an outage.
2. **Look at the Render logs** (Render dashboard -> the service -> Logs). Search for `Traceback`, `[email]`, `[x-login]`, `[health]`, `[grading]`.
3. **Did it start right after a deploy?** Render -> Events. If yes: roll back (section 3), investigate afterwards.
4. **Take a backup before touching anything** (section 4).

Then come to Claude and paste the "Prompt for Claude" from the end of this file, filled in.

## 1. Site down / 500s everywhere

| Symptom | Likely cause | Fix |
|---|---|---|
| `/api/health` -> `"database": false` | DB file missing/locked/corrupted | Logs show `[health] database check failed`. Check `DB_PATH` points to the persistent disk. Restore the latest backup (section 4). |
| Build or start fails on Render | Bad import/syntax in the last commit | Roll back the deploy (section 3). Locally: `python -m py_compile app.py db.py rating.py email_service.py` |
| Everybody's data vanished after a deploy | `DB_PATH` not set, so the DB lived in the ephemeral container | Set `DB_PATH=/var/data/app.db`, add a Render Disk mounted at `/var/data`. `/api/health` shows `persistent_db_path: false` when this is wrong. |
| Open tasks say "expired/invalid" after restarts | `TASK_SECRET` not set | Set `TASK_SECRET` to a long random string (health: `configured.task_secret`). |
| Very slow first request | Free plan sleeping | Not a bug. Paid plan or an uptime pinger on `/api/health` every 10 min. |
| `429 Too many requests` for real users | Rate limits in `app.py` (`RATE_LIMITS`) too tight, or one IP (school/campus NAT) shared by many | Raise the limit for that bucket, redeploy. |

## 2. One feature broken

| Feature | Check | Fix / kill switch |
|---|---|---|
| **Task generation (PRO) fails** | Logs: Anthropic errors (`401` key, `429` rate/credit, `overloaded`). Anthropic console -> usage/billing. | Check `ANTHROPIC_API_KEY` and credit balance. Free pool tasks keep working without the API. Limit spend: lower `PRO_DAILY_LIMIT`. |
| **Emails not arriving** | Logs `[email] Resend rejected ... HTTP 403` = sender/domain problem; `RESEND_API_KEY is not set` = key missing. Resend dashboard -> Logs. | `MAIL_FROM` must be `Accounting Trainer <auth@accountingtrainer.app>` (verified domain). While broken, `EMAIL_FAIL_OPEN=1` (default) lets people in without confirmation. |
| **Users can't log in (verification)** | Message "Please check your inbox..." | Temporary relief: `REQUIRE_EMAIL_VERIFICATION=0` (or leave `EMAIL_FAIL_OPEN=1`). |
| **Sign in with X fails** | The error is on `x.com`: callback URL mismatch. Logs `[x-login] ... HTTP`. | developer.x.com -> app -> Callback URI must equal `https://accountingtrainer.app/auth/twitter/callback`; OAuth 2.0 Web App; `TWITTER_CLIENT_ID/SECRET` are the OAuth 2.0 pair; `PUBLIC_URL=https://accountingtrainer.app` (plain URL, no Markdown). |
| **Payments / PRO not activating** | Lemon Squeezy dashboard -> Webhooks -> deliveries (look for non-200). Logs for webhook signature errors. | `LEMON_SQUEEZY_WEBHOOK_SECRET` must equal the signing secret of the webhook. Webhook URL: `https://accountingtrainer.app/api/webhook/lemon`. Give a paying user PRO by hand: set `is_pro=1` and `pro_expires_at` for their row (section 5) or add the username to `PRO_USERS`. |
| **Wrong links in emails** | `PUBLIC_URL` | Must be exactly `https://accountingtrainer.app`. |
| **Leaderboard empty / weird** | It lists only PRO players who finished calibration | Not a bug. See `db._ranked_filter`. |
| **Someone cheats the rating** | Check `users` by `tasks_total`, `elo`, `last_scored_at` | Reset with `POST /api/admin/set-rating` (own account only) or SQL (section 5). Raise `MIN_SECONDS_PER_POINT`. |

## 3. Roll back a bad deploy

Fastest: Render dashboard -> Events -> pick the last good deploy -> **Rollback**.
Or in git (never rewrite history, add a revert commit):

```bash
git log --oneline -10
git revert <bad-commit-sha>
git push
```

## 4. Backups (do this regularly, and before any risky change)

```bash
python scripts/backup_db.py
```

Writes `backups/data-<time>.db` using SQLite's online backup (safe while the site runs); keeps the newest 14. On Render open the service **Shell**, run it there and download the file. Restore = stop the service, replace the file at `DB_PATH`, start it. **Test a restore once while nothing is on fire.**

## 5. Quick database look (read before you write)

```bash
sqlite3 "$DB_PATH" "select count(*) from users;"
sqlite3 "$DB_PATH" "select id, username, nickname, is_pro, pro_expires_at, elo, calibration_left, tasks_total from users order by id desc limit 20;"
sqlite3 "$DB_PATH" "update users set is_pro=1, pro_expires_at='2026-12-31T00:00:00+00:00' where username='someone@example.com';"
```

Always take a backup first and run the `select` for the same `where` before an `update`/`delete`.

## 6. Environment variables at a glance (values live only in Render and your local `.env`, never in git)

`ANTHROPIC_API_KEY`, `TASK_SECRET`, `DB_PATH`, `PUBLIC_URL`, `RESEND_API_KEY`, `MAIL_FROM`, `EMAIL_FAIL_OPEN`, `REQUIRE_EMAIL_VERIFICATION`,
`LEMON_SQUEEZY_API_KEY / STORE_ID / VARIANT_ID / WEBHOOK_SECRET / TEST_MODE`, `PRO_PRICE_LABEL`, `PRO_USERS`, `PRO_DAILY_LIMIT`,
`TWITTER_CLIENT_ID / CLIENT_SECRET / REDIRECT_URI`, `ADMIN_USERS`, `MIN_SECONDS_PER_POINT`. See `.env.example`.
If a secret leaked (pasted in chat, committed, screenshot): **rotate it at the provider first**, then update Render.

## 7. Dashboards (bookmarks)

Render (service, logs, events, disk) · Resend (domain, logs) · Cloudflare DNS (`accountingtrainer.app`) · Lemon Squeezy (orders, webhooks) · developer.x.com (app, callback) · Anthropic console (usage, credit) · Formspree (feedback).

## Prompt for Claude (copy, fill in, paste)

```
ВСЁ ГОРИТ. Проект accounting-trainer (C:\someshi\website), прочитай RUNBOOK.md.
Что вижу: <симптом, ссылка/скриншот, когда началось>
/api/health отвечает: <200 / 503 / не отвечает>
Последний деплой: <когда и что менял>
Логи Render (последние ошибки): <вставь Traceback / строки [email] [x-login]>
Сделай: 1) найди причину, 2) предложи самое быстрое безопасное лечение, 3) ничего не удаляй и не пушь без моего ок.
```
