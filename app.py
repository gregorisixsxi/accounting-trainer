import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Load .env before db/generator read their settings (real environment variables still win).
load_dotenv(Path(__file__).resolve().parent / ".env")

import db
import pool
from generator import TOPIC_PROMPTS, generate_task_from_claude

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Accounting Trainer")
db.init()

# Per-IP rate limiting. Buckets: AI task generation and login attempts (brute-force protection).
RATE_LIMITS = {
    "generate": (10, 60),  # 10 requests per minute
    "auth": (10, 60),
}
REQUEST_COUNTS = defaultdict(list)


def client_ip(request: Request) -> str:
    # Behind Render's proxy request.client.host is the proxy address (shared by everyone);
    # the real user IP arrives in X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def bucket_for(path: str) -> str | None:
    if path.startswith("/api/generate"):
        topic = path.rsplit("/", 1)[-1]
        # pool tasks cost nothing, so the limit only applies to Claude API calls
        return None if topic in pool.POOL_TOPICS and path != "/api/generate/pro" else "generate"
    if path in ("/api/login", "/api/register", "/api/create-checkout-session"):
        return "auth"
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


@app.post("/api/register")
def register(creds: Credentials):
    username, password = validate_credentials(creds)
    user = db.create_user(username, password)
    if user is None:
        raise HTTPException(status_code=409, detail="This username is already taken.")
    return auth_response(user)


@app.post("/api/login")
def login(creds: Credentials):
    username, password = validate_credentials(creds)
    status, user = db.check_credentials(username, password)
    if status == "no_user":
        raise HTTPException(status_code=404, detail="No such user.")
    if status == "bad_password":
        raise HTTPException(status_code=401, detail="Wrong password.")
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
@app.get("/")
def read_root():
    # no-cache: after a deploy users get the fresh page instead of a cached one
    return FileResponse(BASE_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health_check():
    """Ping endpoint, keeps the server from sleeping."""
    return {"status": "ok"}


# ---------------- task generation ----------------
@app.get("/api/generate/{topic}")
def generate_task(topic: str, exclude: str = ""):
    """Free mode: tasks come from the local pools (no API cost); other topics fall back to Claude."""
    recent = {x for x in exclude.split(",") if x}
    task = pool.get_task(topic, recent)
    if task is not None:
        return task
    if topic not in TOPIC_PROMPTS:
        available = sorted(set(TOPIC_PROMPTS) | pool.POOL_TOPICS)
        raise HTTPException(status_code=404, detail=f"Unknown topic '{topic}'. Available: {', '.join(available)}")
    try:
        return generate_task_from_claude(topic)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Model returned invalid JSON: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


PRO_TOPICS = ("balance_sheet", "terminology")
AVOID_LIMIT = 40  # how many recent tasks are shown to the model
PRO_ATTEMPTS = 2


class ProRequest(BaseModel):
    topic: str = "balance_sheet"


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def task_summary(topic: str, task: dict) -> tuple[str, str]:
    """(description for the "do not repeat" prompt, key for exact duplicate comparison)."""
    if topic == "balance_sheet":
        company = str(task.get("company_name", "")).strip()
        total = (task.get("balance_sheet_key") or {}).get("total_assets", "")
        return f"{company} (total assets {total})", _norm(company)
    questions = [r.get("question", "") if isinstance(r, dict) else str(r) for r in task.get("requirements", [])]
    text = "; ".join(q for q in questions if q)[:300]
    return text or str(task.get("task_title", "")), _norm(questions[0] if questions else str(task.get("task_title", "")))


@app.post("/api/generate/pro")
def generate_pro(body: ProRequest, user: dict = Depends(current_user)):
    if not user["is_pro"]:
        raise HTTPException(status_code=403, detail="PRO access is required for AI-generated tasks.")
    if body.topic not in PRO_TOPICS:
        raise HTTPException(status_code=404, detail=f"Unknown PRO topic. Available: {', '.join(PRO_TOPICS)}")

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
        raise
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Model returned invalid JSON: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    db.add_to_history(user["id"], {"topic": body.topic, "summary": summary, "key": key, "ts": int(time.time())})
    return task


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
