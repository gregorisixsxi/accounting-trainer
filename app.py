import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, model_validator
from typing import Literal

# Load .env before db/generator read their settings (real environment variables still win).
load_dotenv(Path(__file__).resolve().parent / ".env")

import db
import email_service
import pool
from generator import TOPIC_PROMPTS, generate_task_from_claude

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Accounting Trainer")
db.init()

# Per-IP rate limiting. Buckets: AI task generation and login attempts (brute-force protection).
RATE_LIMITS = {
    "generate": (10, 60),  # 10 requests per minute
    "auth": (10, 60),
    "archive": (60, 60),
}
REQUEST_COUNTS = defaultdict(list)


def client_ip(request: Request) -> str:
    """Real client IP. Cloudflare sets CF-Connecting-IP; then X-Forwarded-For; then the direct peer.

    For X-Forwarded-For the LAST entry is used: it is appended by the nearest trusted proxy,
    while the first entries can be forged by the client to dodge the rate limits.
    """
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


AUTH_RATE_LIMITED_PATHS = {
    "/api/login", "/api/register", "/api/create-checkout-session",
    "/api/auth/register", "/api/auth/forgot-password", "/api/auth/reset-password", "/api/auth/resend-verification",
}


def bucket_for(path: str) -> str | None:
    if path.startswith("/api/generate"):
        topic = path.rsplit("/", 1)[-1]
        # pool tasks cost nothing, so the limit only applies to Claude API calls
        return None if topic in pool.POOL_TOPICS and path != "/api/generate/pro" else "generate"
    if path in AUTH_RATE_LIMITED_PATHS:
        return "auth"
    if path == "/api/archive":
        return "archive"
    return None


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    bucket = bucket_for(request.url.path)
    if bucket:
        limit, window = RATE_LIMITS[bucket]
        key = (bucket, client_ip(request))
        now = time.time()
        recent = [t for t in REQUEST_COUNTS[key] if now - t < window]
        if len(recent) >= limit:
            REQUEST_COUNTS[key] = recent
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please wait a minute and try again."},
            )
        recent.append(now)
        REQUEST_COUNTS[key] = recent
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"  # API answers must never be cached by the browser or a proxy
    return response


# ---------------- auth ----------------
USERNAME_RE = re.compile(r"^\S.{1,62}\S$")  # 3-64 characters, any string (username or e-mail)


class Credentials(BaseModel):
    username: str
    password: str


def validate_credentials(c: Credentials) -> tuple[str, str]:
    username = c.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=422, detail="Username must be 3-64 characters.")
    if not (6 <= len(c.password) <= 128):
        raise HTTPException(status_code=422, detail="Password must be 6-128 characters.")
    return username, c.password


def auth_response(user: dict) -> dict:
    return {
        "token": db.create_session(user["id"]),
        "username": user["username"],
        "is_pro": user["is_pro"],
        "pro_expires_at": user["pro_expires_at"],
    }


def current_user(authorization: str | None = Header(default=None)) -> dict:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    user = db.user_by_token(token) if token else None
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return user


RESEND_COOLDOWN_SECONDS = 60   # one e-mail of each kind per minute per account


class EmailRequest(BaseModel):
    email: str


class ResetRequest(BaseModel):
    token: str
    password: str


def mail_ready() -> None:
    if not (email_service.is_configured() or email_service.dev_log_enabled()):
        raise HTTPException(status_code=503, detail="Email service is not available right now. Please try again later.")


def issue_verification(user_id: int, email: str, request: Request, background: BackgroundTasks) -> None:
    age = db.token_age_seconds(user_id, "verify")
    if age is not None and age < RESEND_COOLDOWN_SECONDS:
        return   # a fresh mail was just sent
    token = secrets.token_urlsafe(32)
    db.set_verify_token(user_id, token)
    background.add_task(email_service.send_verification_email, email, f"{public_base_url(request)}/verify?token={token}")


@app.post("/api/auth/register")
@app.post("/api/register")   # old path kept as an alias
def register(creds: Credentials, request: Request, background: BackgroundTasks):
    username, password = validate_credentials(creds)
    if not EMAIL_RE.match(username):
        raise HTTPException(status_code=422, detail="Please use a valid email address to create an account.")
    mail_ready()
    email = username.lower()
    existing = db.get_user_by_email(email)
    if existing and existing["is_verified"]:
        raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in or reset your password.")
    if existing:   # registered earlier but never confirmed: send a new link, keep the original password
        issue_verification(existing["id"], email, request, background)
    else:
        user = db.create_user(email, password)
        if user is None:
            raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in or reset your password.")
        issue_verification(user["id"], email, request, background)
    return {"status": "verification_sent", "email": email}


@app.get("/api/auth/verify")
@app.get("/verify")   # the address used in the e-mail
def verify_email_link(token: str = ""):
    ok = bool(token) and db.verify_email(token) is not None
    return RedirectResponse("/?verified=1" if ok else "/?verify_error=1", status_code=303)


@app.post("/api/auth/resend-verification")
def resend_verification(body: EmailRequest, request: Request, background: BackgroundTasks):
    mail_ready()
    email = body.email.strip().lower()
    user = db.get_user_by_email(email) if EMAIL_RE.match(email) else None
    if user and not user["is_verified"]:
        issue_verification(user["id"], email, request, background)
    return {"status": "ok"}   # same answer whether or not the account exists


@app.post("/api/auth/forgot-password")
def forgot_password(body: EmailRequest, request: Request, background: BackgroundTasks):
    mail_ready()
    email = body.email.strip().lower()
    user = db.get_user_by_email(email) if EMAIL_RE.match(email) else None
    if user:
        age = db.token_age_seconds(user["id"], "reset")
        if age is None or age >= RESEND_COOLDOWN_SECONDS:
            token = secrets.token_urlsafe(32)
            db.set_reset_token(user["id"], token)
            background.add_task(
                email_service.send_password_reset_email, email, f"{public_base_url(request)}/reset-password?token={token}"
            )
    return {"status": "ok", "message": "If an account exists for that email, we sent a password reset link."}


@app.post("/api/auth/reset-password")
def reset_password(body: ResetRequest):
    if not (6 <= len(body.password) <= 128):
        raise HTTPException(status_code=422, detail="Password must be 6-128 characters.")
    if db.reset_password(body.token, body.password) is None:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Please request a new one.")
    return {"status": "ok"}


@app.post("/api/login")
def login(creds: Credentials):
    username, password = validate_credentials(creds)
    key = username.lower()
    wait = db.login_lock_seconds(key)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {(wait + 59) // 60} min.",
            headers={"Retry-After": str(wait)},
        )
    status, user = db.check_credentials(username, password)
    if status == "no_user":
        raise HTTPException(status_code=404, detail="No such user.")
    if status == "bad_password":
        left = db.record_login_failure(key)
        detail = "Wrong password." if left else "Wrong password. This account is locked for 15 minutes."
        if left and left <= 2:
            detail += f" {left} attempt{'s' if left != 1 else ''} left."
        raise HTTPException(status_code=401, detail=detail)
    db.clear_login_failures(key)
    if not user["is_verified"]:
        raise HTTPException(status_code=403, detail="Please check your inbox to verify your email first.")
    return auth_response(user)


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "is_pro": user["is_pro"], "pro_expires_at": user["pro_expires_at"]}


@app.get("/api/history")
def history_info(user: dict = Depends(current_user)):
    return {"count": len(db.get_history(user["id"]))}


@app.post("/api/history/reset")
def history_reset(user: dict = Depends(current_user)):
    db.clear_history(user["id"])
    return {"count": 0}


# ---------------- archive of solved tasks ----------------
class ReviewItem(BaseModel):
    label: str = Field(max_length=300)
    yours: str = Field(default="", max_length=500)
    expected: str = Field(default="", max_length=500)
    correct: bool
    note: str = Field(default="", max_length=600)


class ArchiveEntry(BaseModel):
    attempt_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    topic: Literal["balance_sheet", "terminology"]
    source: Literal["free", "pro"] = "free"
    title: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    condition: str = Field(default="", max_length=6000)
    explanation: str = Field(default="", max_length=2000)
    review: list[ReviewItem] = Field(max_length=30)
    score: int = Field(ge=0, le=30)
    total: int = Field(ge=1, le=30)

    @model_validator(mode="after")
    def score_within_total(self):
        if self.score > self.total:
            raise ValueError("score cannot be higher than total")
        return self


@app.post("/api/archive")
def archive_save(entry: ArchiveEntry, user: dict = Depends(current_user)):
    """Called after the student presses Check Answer. Free (pool) and PRO (AI) tasks are both archived."""
    item_id = db.save_archive(user["id"], entry.model_dump())
    return {"id": item_id, "saved": True}


@app.get("/api/archive")
def archive_list(topic: str = "", limit: int = 20, offset: int = 0, user: dict = Depends(current_user)):
    if topic and topic not in FREE_TOPICS:
        raise HTTPException(status_code=422, detail="Unknown topic.")
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    items, total = db.list_archive(user["id"], topic or None, limit, offset)
    return {"items": items, "total": total, "has_more": offset + len(items) < total}


@app.get("/api/archive/{item_id}")
def archive_item(item_id: int, user: dict = Depends(current_user)):
    item = db.get_archive_item(user["id"], item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Archive entry not found.")
    return item


@app.delete("/api/archive/{item_id}")
def archive_delete(item_id: int, user: dict = Depends(current_user)):
    if not db.delete_archive_item(user["id"], item_id):
        raise HTTPException(status_code=404, detail="Archive entry not found.")
    return {"deleted": True}


@app.post("/api/logout")
def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.lower().startswith("bearer "):
        db.delete_session(authorization[7:].strip())
    return {"status": "ok"}


# ---------------- billing (Lemon Squeezy) ----------------
# Env: LEMON_SQUEEZY_API_KEY, LEMON_SQUEEZY_STORE_ID, LEMON_SQUEEZY_VARIANT_ID, LEMON_SQUEEZY_WEBHOOK_SECRET,
# optional: PUBLIC_URL (https://your-site), PRO_PRICE_LABEL ("$5/mo"), LEMON_SQUEEZY_TEST_MODE=1
LEMON_API = "https://api.lemonsqueezy.com/v1"
PRO_PERIOD = timedelta(days=30)   # used when the webhook has no renews_at
PRO_GRACE = timedelta(days=2)     # slack so a late renewal webhook does not cut access for a moment
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def lemon_config() -> dict:
    return {
        "api_key": os.getenv("LEMON_SQUEEZY_API_KEY", ""),
        "store_id": os.getenv("LEMON_SQUEEZY_STORE_ID", ""),
        "variant_id": os.getenv("LEMON_SQUEEZY_VARIANT_ID", ""),
        "secret": os.getenv("LEMON_SQUEEZY_WEBHOOK_SECRET", ""),
    }


def public_base_url(request: Request) -> str:
    configured = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


@app.get("/api/plans")
def plans():
    cfg = lemon_config()
    return {
        "price_label": os.getenv("PRO_PRICE_LABEL", "").strip(),
        "checkout_available": bool(cfg["api_key"] and cfg["store_id"] and cfg["variant_id"]),
    }


@app.post("/api/create-checkout-session")
def create_checkout_session(request: Request, user: dict = Depends(current_user)):
    cfg = lemon_config()
    if not (cfg["api_key"] and cfg["store_id"] and cfg["variant_id"]):
        raise HTTPException(status_code=503, detail="Payments are not configured yet.")
    if user["is_pro"]:
        raise HTTPException(status_code=409, detail="You already have PRO access.")

    checkout_data = {"custom": {"user_id": str(user["id"])}}
    if EMAIL_RE.match(user["username"]):
        checkout_data["email"] = user["username"]
    attributes = {
        "checkout_data": checkout_data,
        "product_options": {"redirect_url": public_base_url(request) + "/?upgraded=1"},
    }
    if os.getenv("LEMON_SQUEEZY_TEST_MODE", "").lower() in ("1", "true", "yes"):
        attributes["test_mode"] = True
    body = {
        "data": {
            "type": "checkouts",
            "attributes": attributes,
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(cfg["store_id"])}},
                "variant": {"data": {"type": "variants", "id": str(cfg["variant_id"])}},
            },
        }
    }
    req = urllib.request.Request(
        LEMON_API + "/checkouts",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
        return {"checkout_url": payload["data"]["attributes"]["url"]}
    except urllib.error.HTTPError as e:
        print(f"[billing] Lemon Squeezy checkout failed: {e.code} {e.read()[:300]!r}")
    except Exception as e:
        print(f"[billing] Lemon Squeezy checkout error: {e!r}")
    raise HTTPException(status_code=502, detail="Could not start the checkout. Please try again later.")


def process_lemon_event(payload: dict) -> str:
    """Applies one verified webhook event to the user record. Returns a short status string."""
    meta = payload.get("meta") or {}
    event = meta.get("event_name", "")
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    custom = meta.get("custom_data") or {}

    # invoices carry the subscription id in attributes; subscription objects carry it as their own id
    sub_id = attrs.get("subscription_id") if data.get("type") == "subscription-invoices" else data.get("id")
    user_id = db.find_user_id(custom.get("user_id"), sub_id, attrs.get("user_email"))
    if user_id is None:
        print(f"[billing] {event}: user not found (subscription {sub_id})")
        return "user_not_found"

    customer_id = attrs.get("customer_id")
    now = db.now_utc()

    def grant():
        renews = db.parse_dt(attrs.get("renews_at")) or db.parse_dt(attrs.get("trial_ends_at"))
        expires = (renews if renews and renews > now else now + PRO_PERIOD) + PRO_GRACE
        db.set_subscription(user_id, True, expires, customer_id, sub_id)
        return "granted"

    def cancel():
        # a cancelled subscription stays paid until the end of the billing period
        ends = db.parse_dt(attrs.get("ends_at"))
        if ends and ends > now:
            db.set_subscription(user_id, True, ends, customer_id, sub_id)
            return "cancelled_until_period_end"
        db.set_subscription(user_id, False, None, customer_id, sub_id)
        return "revoked"

    def revoke():
        db.set_subscription(user_id, False, None, customer_id, sub_id)
        return "revoked"

    if event == "subscription_expired":
        return revoke()
    if event == "subscription_cancelled":
        return cancel()
    if event in ("subscription_created", "subscription_updated", "subscription_payment_success",
                 "subscription_resumed", "subscription_unpaused"):
        status = attrs.get("status") if data.get("type") == "subscriptions" else None
        if status == "cancelled":
            return cancel()
        if status in ("expired", "unpaid", "paused"):
            return revoke()
        if status == "past_due":
            return "ignored_past_due"
        return grant()
    return "ignored_event"


@app.post("/api/webhook/lemon")
async def lemon_webhook(request: Request):
    secret = lemon_config()["secret"]
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured.")
    raw = await request.body()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-signature", "")):
        raise HTTPException(status_code=401, detail="Invalid signature.")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON.")
    result = await run_in_threadpool(process_lemon_event, payload)
    return {"status": result}


# ---------------- pages / health ----------------
STATIC_DIR = BASE_DIR / "static"


@app.get("/")
@app.get("/reset-password")
def read_root(request: Request):
    # __SITE_URL__ in the meta tags becomes the real absolute address (social crawlers need absolute URLs).
    # no-cache: after a deploy users get the fresh page instead of a cached one.
    html = (BASE_DIR / "index.html").read_text(encoding="utf-8").replace("__SITE_URL__", public_base_url(request))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def static_file(name: str, media_type: str) -> FileResponse:
    return FileResponse(STATIC_DIR / name, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/og.png")
def og_image():
    return static_file("og.png", "image/png")


@app.get("/favicon.svg")
def favicon_svg():
    return static_file("favicon.svg", "image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    return static_file("favicon.png", "image/png")   # browsers accept PNG content for /favicon.ico


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return static_file("apple-touch-icon.png", "image/png")


@app.get("/robots.txt")
def robots(request: Request):
    body = f"User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: {public_base_url(request)}/sitemap.xml\n"
    return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sitemap.xml")
def sitemap(request: Request):
    lastmod = time.strftime("%Y-%m-%d", time.gmtime((BASE_DIR / "index.html").stat().st_mtime))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{public_base_url(request)}/</loc><lastmod>{lastmod}</lastmod></url>\n"
        "</urlset>\n"
    )
    return Response(xml, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/health")
def health_check():
    """Ping endpoint, keeps the server from sleeping."""
    return {"status": "ok"}


# ---------------- task generation ----------------
FREE_TOPICS = ("balance_sheet", "terminology")   # what the UI offers; other topics are not public


@app.get("/api/generate/{topic}")
def generate_task(topic: str, exclude: str = ""):
    """Free mode: tasks come from the local pools (no API cost); other topics fall back to Claude."""
    recent = {x for x in exclude.split(",") if x}
    task = pool.get_task(topic, recent)
    if task is not None:
        return task
    if topic not in FREE_TOPICS:
        raise HTTPException(status_code=404, detail=f"Unknown topic '{topic}'. Available: {', '.join(FREE_TOPICS)}")
    # only reached if a pool file is missing: keep the site working, still limited by the per-IP rate limit
    try:
        return generate_task_from_claude(topic)
    except json.JSONDecodeError as e:
        print(f"[generate] invalid JSON from model: {e}")
        raise HTTPException(status_code=502, detail="The task generator returned an invalid answer. Please try again.")
    except Exception as e:
        print(f"[generate] failed: {e!r}")
        raise HTTPException(status_code=500, detail="Could not generate a task. Please try again.")


PRO_TOPICS = ("balance_sheet", "terminology")
AVOID_LIMIT = 40  # how many recent tasks are shown to the model
PRO_ATTEMPTS = 2


class ProRequest(BaseModel):
    topic: str = "balance_sheet"


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def task_summary(topic: str, task: dict) -> tuple[str, str]:
    """(description for the "do not repeat" prompt, key for exact duplicate comparison)."""
    if task.get("type") == "balance_sheet":
        company = str(task.get("company_name", "")).strip()
        total = task["sides"][0]["total"]["cells"][0]["value"]
        variant = task.get("variant", "")
        return f"{company} ({variant or 'balance sheet'}, total assets {total:,})", f"{_norm(company)}|{total}"
    prompts = [q.get("prompt", "") for q in task.get("questions", [])]
    text = "; ".join(p[:90] for p in prompts)[:400]
    return text or str(task.get("task_title", "")), _norm(prompts[0] if prompts else str(task.get("task_title", "")))


@app.post("/api/generate/pro")
def generate_pro(body: ProRequest, user: dict = Depends(current_user)):
    if not user["is_pro"]:
        raise HTTPException(status_code=403, detail="PRO access is required for AI-generated tasks.")
    if body.topic not in PRO_TOPICS:
        raise HTTPException(status_code=404, detail=f"Unknown PRO topic. Available: {', '.join(PRO_TOPICS)}")

    allowed, used = db.consume_pro_quota(user["id"])
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Daily PRO limit reached ({db.PRO_DAILY_LIMIT} tasks per day). It resets at 00:00 UTC.",
        )

    history = db.get_history(user["id"], body.topic)
    seen_keys = {h.get("key") for h in history}
    avoid = [h["summary"] for h in history[-AVOID_LIMIT:] if h.get("summary")]

    try:
        for _ in range(PRO_ATTEMPTS):
            task = generate_task_from_claude(body.topic, avoid=avoid)
            summary, key = task_summary(body.topic, task)
            if key not in seen_keys:
                break
            avoid.append(summary)  # duplicate: ask the model to avoid this one too
        else:
            raise HTTPException(status_code=502, detail="Could not create a new unique task, please try again.")
    except HTTPException:
        db.refund_pro_quota(user["id"])
        raise
    except json.JSONDecodeError as e:
        db.refund_pro_quota(user["id"])
        print(f"[pro] invalid JSON from model: {e}")
        raise HTTPException(status_code=502, detail="The task generator returned an invalid answer. Please try again.")
    except Exception as e:
        db.refund_pro_quota(user["id"])
        print(f"[pro] generation failed: {e!r}")
        raise HTTPException(status_code=500, detail="Could not generate a task. Please try again.")

    db.add_to_history(user["id"], {"topic": body.topic, "summary": summary, "key": key, "ts": int(time.time())})
    return task


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
