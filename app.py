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
import twitter_auth
import grading
import rating
import pool
import midterm
import table_types
import tutor
from generator import TOPIC_PROMPTS, generate_task_from_claude

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Accounting Trainer")
db.init()

# Per-IP rate limiting. Buckets: AI task generation and login attempts (brute-force protection).
RATE_LIMITS = {
    "generate": (10, 60),  # 10 requests per minute
    "auth": (10, 60),
    "check": (60, 60),
    "tutor": (20, 60),     # AI replies per minute and IP (on top of the per-user daily budget in tutor.py)
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
    "/auth/twitter/login", "/auth/twitter/callback",
}


def bucket_for(path: str) -> str | None:
    if path.startswith("/api/generate"):
        topic = path.rsplit("/", 1)[-1]
        # pool tasks cost nothing, so the limit only applies to Claude API calls
        return None if topic in pool.POOL_TOPICS and path != "/api/generate/pro" else "generate"
    if path in AUTH_RATE_LIMITED_PATHS:
        return "auth"
    if path in ("/api/check", "/api/midterm/grade", "/api/tables/check"):
        return "check"
    if path.startswith("/api/tutor/"):
        return "tutor"
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
    return {"token": db.create_session(user["id"]), **profile_payload(user)}


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


def verification_required() -> bool:
    """E-mail confirmation is always required. Only an explicit REQUIRE_EMAIL_VERIFICATION=0 switches it off
    (emergency/test use; do not leave it set in production)."""
    return os.getenv("REQUIRE_EMAIL_VERIFICATION", "").strip().lower() not in ("0", "false", "no", "off")


def mail_ready() -> None:
    if not (email_service.is_configured() or email_service.dev_log_enabled()):
        raise HTTPException(status_code=503, detail="Email service is not available right now. Please try again later.")


def fail_open() -> bool:
    """While mail cannot be delivered (no Resend key, or no verified domain: Resend answers 403 for every address except the
    owner's), an account is confirmed anyway instead of leaving the user stuck. Strict again with EMAIL_FAIL_OPEN=0.
    When mail does work, the confirmation e-mail is still required: nothing needs to be switched back on."""
    return os.getenv("EMAIL_FAIL_OPEN", "1").strip().lower() not in ("0", "false", "no", "off")


def send_verification(user_id: int, email: str, request: Request) -> bool:
    """Sends the confirmation e-mail now. True = it went out (or one went out a minute ago)."""
    age = db.token_age_seconds(user_id, "verify")
    if age is not None and age < RESEND_COOLDOWN_SECONDS:
        return True
    token = secrets.token_urlsafe(32)
    db.set_verify_token(user_id, token)
    sent = email_service.send_verification_email(email, f"{public_base_url(request)}/verify?token={token}")
    if not sent:
        db.clear_verify_token(user_id)     # nothing went out, so the one-minute cooldown must not apply
    return sent


AVATARS = [f"g{i}" for i in range(1, 13)]   # preset avatars (gradients), drawn by the page
NICKNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,19}$")


def optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    return db.user_by_token(token) if token else None


def profile_with_rank(user: dict) -> dict:
    """The rating profile plus the position in the all-time board (None until the player is ranked)."""
    fresh = db.get_user(user["id"]) or user
    return {**rating.profile_view(fresh), "global_rank": db.user_rank(user["id"], "all")}


def profile_payload(user: dict) -> dict:
    return {
        "username": user["username"],
        "nickname": user.get("nickname") or user["username"].split("@")[0],
        "avatar": user.get("avatar"),
        "avatar_url": user.get("avatar_url"),
        "x_username": user.get("twitter_username"),
        "is_pro": user["is_pro"],
        "pro_expires_at": user["pro_expires_at"],
        "profile": profile_with_rank(user),
    }


@app.post("/api/auth/register")
@app.post("/api/register")   # old path kept as an alias
def register(creds: Credentials, request: Request, background: BackgroundTasks):
    username, password = validate_credentials(creds)
    if not EMAIL_RE.match(username) or username.lower().endswith("@twitter.invalid"):   # that domain is reserved for X accounts
        raise HTTPException(status_code=422, detail="Please use a valid email address to create an account.")
    email = username.lower()
    if not verification_required():
        # temporary open mode: the account is confirmed right away and the user is signed in
        if db.get_user_by_email(email):
            raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in or reset your password.")
        user = db.create_user(email, password, verified=True)
        if user is None:
            raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in or reset your password.")
        return auth_response(user)
    if not fail_open():
        mail_ready()
    existing = db.get_user_by_email(email)
    if existing and existing["is_verified"]:
        raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in or reset your password.")
    if existing:   # registered earlier but never confirmed: keep the original password
        user_id = existing["id"]
    else:
        user = db.create_user(email, password)
        if user is None:
            raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in or reset your password.")
        user_id = user["id"]
    sent = send_verification(user_id, email, request)
    if not sent and fail_open():
        # the mail could not be delivered: confirm the account so the user is not locked out, and sign in
        # (an old unconfirmed account is only opened for someone who knows its password)
        if not existing or db.check_credentials(email, password)[0] == "ok":
            print(f"[auth] confirmation mail to {email} failed, the account was confirmed without it (EMAIL_FAIL_OPEN)")
            db.mark_verified(user_id)
            return auth_response(db.get_user(user_id))
    return {"status": "verification_sent", "email": email}


@app.get("/api/auth/verify")
@app.get("/verify")   # the address used in the e-mail
def verify_email_link(token: str = ""):
    ok = bool(token) and db.verify_email(token) is not None
    return RedirectResponse("/?verified=1" if ok else "/?verify_error=1", status_code=303)


@app.post("/api/auth/resend-verification")
def resend_verification(body: EmailRequest, request: Request):
    if not fail_open():
        mail_ready()
    email = body.email.strip().lower()
    user = db.get_user_by_email(email) if EMAIL_RE.match(email) else None
    verified = False
    if user and not user["is_verified"]:
        if not send_verification(user["id"], email, request) and fail_open():
            db.mark_verified(user["id"])          # mail cannot be delivered right now: do not keep the account locked
            verified = True
    return {"status": "ok", "verified": verified}   # same answer whether or not the account exists


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
    if not user["is_verified"] and verification_required():
        raise HTTPException(status_code=403, detail="Please check your inbox to verify your email first.")
    return auth_response(user)


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return profile_payload(user)


class ProfileUpdate(BaseModel):
    nickname: str | None = None
    avatar: str | None = None


@app.get("/api/leaderboard")
def get_leaderboard(period: str = "all", user: dict | None = Depends(optional_user)):
    """Top 50 PRO players by Score. Only nicknames, avatars, levels, Score and task counts are public."""
    if period not in db.PERIODS:
        raise HTTPException(status_code=422, detail="period must be all, year or month.")
    rows = db.leaderboard(period)
    entries = [{**{k: v for k, v in r.items() if k != "user_id"}, "is_me": bool(user and r["user_id"] == user["id"])} for r in rows]
    me = None
    if user:
        fresh = db.get_user(user["id"]) or user
        rank = db.user_rank(user["id"], period)
        me = {
            "rank": rank, "in_top": bool(rank and rank <= db.LEADERBOARD_SIZE),
            "elo": fresh["elo"], "level": rating.profile_view(fresh)["level"], "is_pro": fresh["is_pro"],
            "calibrating": fresh["calibration_left"] > 0, "calibration_left": fresh["calibration_left"],
            "calibration_total": rating.CALIBRATION_TASKS, "tasks": fresh["tasks_total"],
        }
    return {"period": period, "entries": entries, "me": me, "size": db.LEADERBOARD_SIZE}


# ---------------- sign in with X (Twitter) ----------------
X_COOKIE = "tw_oauth"          # signed, HttpOnly: carries the state and the PKCE verifier between /login and /callback
X_COOKIE_PATH = "/auth/twitter"
X_COOKIE_SECONDS = 600


def twitter_redirect_uri(request: Request) -> str:
    return os.getenv("TWITTER_REDIRECT_URI", "").strip() or public_base_url(request) + "/auth/twitter/callback"


@app.get("/api/auth/providers")
def auth_providers():
    return {"x": twitter_auth.is_configured()}


X_LINK_SECONDS = 300


@app.post("/api/auth/twitter/link")
def twitter_link_start(user: dict = Depends(current_user)):
    """Signed-in user wants to attach an X account: returns the address that starts the X flow in link mode.
    The one-time ticket is signed and short-lived, because a plain browser redirect cannot carry the session header."""
    if not twitter_auth.is_configured():
        raise HTTPException(status_code=503, detail="Sign in with X is not set up yet.")
    if user.get("twitter_username"):
        raise HTTPException(status_code=409, detail="An X account is already linked.")
    ticket = grading.pack({"k": "xlink", "uid": user["id"], "iat": int(time.time())})
    return {"url": "/auth/twitter/login?link=" + ticket}


@app.get("/auth/twitter/login")
def twitter_login(request: Request, link: str = ""):
    if not twitter_auth.is_configured():
        return RedirectResponse("/?x_error=not_configured", status_code=303)
    link_user = None
    if link:
        try:
            ticket = grading.open_token(link)
        except grading.TokenError:
            return RedirectResponse("/?x_error=state", status_code=303)
        if ticket.get("k") != "xlink" or time.time() - ticket.get("iat", 0) > X_LINK_SECONDS:
            return RedirectResponse("/?x_error=state", status_code=303)
        link_user = ticket["uid"]
    state = secrets.token_urlsafe(24)
    verifier, challenge = twitter_auth.new_pkce()
    redirect_uri = twitter_redirect_uri(request)
    response = RedirectResponse(twitter_auth.authorize_url(redirect_uri, state, challenge), status_code=303)
    response.set_cookie(
        X_COOKIE, grading.pack({"k": "x", "s": state, "v": verifier, "l": link_user, "iat": int(time.time())}),
        max_age=X_COOKIE_SECONDS, httponly=True, samesite="lax", secure=redirect_uri.startswith("https://"), path=X_COOKIE_PATH,
    )
    return response


@app.get("/auth/twitter/callback")
def twitter_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    def finish(location: str) -> RedirectResponse:
        response = RedirectResponse(location, status_code=303)
        response.delete_cookie(X_COOKIE, path=X_COOKIE_PATH)
        response.headers["Cache-Control"] = "no-store"
        return response

    if error:
        return finish("/?x_error=" + ("access_denied" if error == "access_denied" else "failed"))
    # CSRF protection: the state in the address must be the one this very browser received in /login
    try:
        saved = grading.open_token(request.cookies.get(X_COOKIE, ""))
    except grading.TokenError:
        return finish("/?x_error=state")
    if saved.get("k") != "x" or time.time() - saved.get("iat", 0) > X_COOKIE_SECONDS or not state or not hmac.compare_digest(saved["s"], state):
        return finish("/?x_error=state")
    if not code:
        return finish("/?x_error=failed")
    try:
        me = twitter_auth.fetch_me(twitter_auth.exchange_code(code, saved["v"], twitter_redirect_uri(request)))
    except twitter_auth.TwitterError:
        return finish("/?x_error=failed")

    if saved.get("l"):                       # link mode: attach this X account to the signed-in user, no new session
        outcome = db.link_twitter(saved["l"], me["id"], me.get("username", ""), me.get("profile_image_url"))
        return finish("/?x_linked=1" if outcome == "ok" else "/?x_error=" + ("taken" if outcome == "taken" else "other"))

    user, created = db.find_or_create_twitter_user(me["id"], me.get("username", ""), me.get("profile_image_url"))
    token = db.create_session(user["id"])
    # the session token travels in the URL fragment: it is never sent to a server and never appears in logs or Referer headers
    return finish(f"/#xauth={token}" + ("&new=1" if created else ""))


ADMIN_USERS = {u.strip().lower() for u in os.environ.get("ADMIN_USERS", "").split(",") if u.strip()}


class RatingOverride(BaseModel):
    level: int | None = Field(default=None, ge=1, le=10)
    calibrating: bool = False


@app.post("/api/admin/set-rating")
def admin_set_rating(body: RatingOverride, user: dict = Depends(current_user)):
    """For testing the look of the levels: only usernames listed in ADMIN_USERS may call it, and only for their own account."""
    if user["username"].lower() not in ADMIN_USERS:
        raise HTTPException(status_code=403, detail="Not allowed.")
    if body.calibrating:
        db.set_rating(user["id"], rating.START_ELO, rating.CALIBRATION_TASKS)
    elif body.level:
        lo = rating.LEVEL_FLOORS[body.level - 1]
        hi = rating.LEVEL_FLOORS[body.level] - 1 if body.level < 10 else lo + 300
        db.set_rating(user["id"], (lo + hi) // 2, 0)
    return profile_payload(db.get_user(user["id"]))


@app.patch("/api/profile")
def update_profile(body: ProfileUpdate, user: dict = Depends(current_user)):
    nickname = body.nickname.strip() if body.nickname is not None else None
    if nickname is not None and not NICKNAME_RE.match(nickname):
        raise HTTPException(status_code=422, detail="Nickname: 3-20 characters, letters, digits, dot, dash or underscore.")
    if body.avatar is not None and body.avatar not in AVATARS:
        raise HTTPException(status_code=422, detail="Unknown avatar.")
    if db.set_profile(user["id"], nickname, body.avatar) == "taken":
        raise HTTPException(status_code=409, detail="This nickname is already taken.")
    return profile_payload(db.get_user(user["id"]))


@app.get("/api/history")
def history_info(user: dict = Depends(current_user)):
    return {"count": len(db.get_history(user["id"]))}


@app.post("/api/history/reset")
def history_reset(user: dict = Depends(current_user)):
    db.clear_history(user["id"])
    return {"count": 0}


# ---------------- what a student did: skill statistics and the mistake notebook ----------------
CLASSIC_TITLES = {"classic_balance_sheet": "Balance sheet (classic)", "classic_terminology": "Terminology"}


def claim_key(payload: dict) -> str:
    """The key under which a checked question is remembered (db.claim_task); the tutor may reveal answers only after it exists."""
    if payload.get("ex") == "practice":
        return "tbl:" + payload["jti"]
    return ("paper:" + payload["ex"]) if payload.get("ex") else payload["jti"]


def record_study(user: dict, payload: dict, answers: dict) -> None:
    """Adds a checked question to the student's skill statistics and stores the wrong answers in the notebook. Never breaks the request."""
    try:
        items = midterm.review_items(payload, answers)
        if not items:
            return
        skill = payload.get("tid") or "classic_" + str(payload.get("topic", "other"))
        title = payload.get("ttl") or CLASSIC_TITLES.get(skill, skill)
        right = sum(1 for i in items if i["correct"])
        db.record_skill(user["id"], skill, title, right / len(items), 1)
        db.add_mistakes(user["id"], skill, title, items)
    except Exception as e:
        print(f"[study] could not record: {e!r}")


# ---------------- checking answers (server-side grading, rating, archive) ----------------
class CheckRequest(BaseModel):
    token: str = Field(max_length=20000)
    answers: dict


MIN_SECONDS_PER_POINT = grading.MIN_SECONDS_PER_POINT


@app.post("/api/check")
def check_answers(body: CheckRequest, user: dict | None = Depends(optional_user)):
    """Marks the answers against the sealed task. For a signed-in student it also saves the archive entry and
    moves the rating (once per served task, and not for answers that came in suspiciously fast)."""
    try:
        payload = grading.open_token(body.token)
        result = grading.grade(payload, body.answers)
    except grading.TokenError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result["answered"]:
        raise HTTPException(status_code=422, detail="Enter at least one answer before checking.")   # nothing is revealed

    first = db.claim_task(payload["jti"], user["id"] if user else None)
    rating_info, archived = None, False
    if user:
        archived = bool(db.save_archive(user["id"], {
            "attempt_id": payload["jti"], "topic": payload["topic"], "source": payload["source"],
            "title": payload["title"][:200], "company": payload["company"][:200], "condition": payload["condition"],
            "explanation": result["explanation"][:2000], "review": result["review"][:30],
            "score": result["score"], "total": result["total"],
        }))
        if first:
            db.mark_solved(user["id"], payload.get("fp", []))          # feeds the "Solved previously" tag
            record_study(user, payload, body.answers)                  # skill statistics + mistake notebook
        if not first:
            rating_info = {"already_counted": True}
        elif (fresh := db.get_user(user["id"]) or user)["calibration_left"] > 0 and payload["topic"] not in rating.CALIBRATION_TOPICS:
            rating_info = {"not_calibrating": True}      # while calibrating, only Balance Sheet tasks count
        elif fresh["calibration_left"] == 0 and not fresh["is_pro"]:
            rating_info = {"no_pro": True}               # after calibration Score is earned with PRO only
        elif time.time() - payload["iat"] < MIN_SECONDS_PER_POINT * result["total"]:
            rating_info = {"too_fast": True}
        else:
            rating_info = db.record_result(user["id"], result["score"], result["total"], payload["penalty"])
        rating_info = {**rating_info, "profile": profile_with_rank(user)}
    result.pop("review", None)
    result.pop("answered", None)
    return {**result, "archived": archived, "rating": rating_info, "signed_in": bool(user)}


# ---------------- mid-term test: a whole practice paper, marked when it is handed in ----------------
class PaperItem(BaseModel):
    token: str = Field(max_length=60000)
    answers: dict = Field(default_factory=dict)


class PaperSubmit(BaseModel):
    items: list[PaperItem] = Field(max_length=30)


@app.get("/api/tables")
def table_types_list():
    """The table types of the Balance Sheet tab (see table_types.py)."""
    return {"types": table_types.catalog()}


@app.get("/api/tables/{type_id}")
def table_practice(type_id: str):
    """One fresh question of a table type that has its own screen (everything except the classic compile/totals sheets)."""
    question = midterm.practice(type_id)
    if question is None:
        raise HTTPException(status_code=404, detail="There is no such table type.")
    return question


class TableCheck(BaseModel):
    token: str = Field(max_length=60000)
    answers: dict = Field(default_factory=dict)


@app.post("/api/tables/check")
def table_check(body: TableCheck, user: dict | None = Depends(optional_user)):
    """Marks one table question and reveals the answers. Practice only: the Score is not affected."""
    try:
        result = midterm.grade_single(body.token, body.answers)
        payload = grading.open_token(body.token)
    except grading.TokenError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result["answered"] and db.claim_task(claim_key(payload), user["id"] if user else None):     # the first check counts for the statistics
        if user:
            record_study(user, payload, body.answers)
    return result


@app.get("/api/midterm")
def midterm_papers():
    return {"papers": midterm.overview(), "time_limit": midterm.TIME_LIMIT_S}


FREE_PAPERS_PER_DAY = int((os.environ.get("FREE_PAPERS_PER_DAY") or "1").strip() or 1)      # PRO: unlimited


@app.get("/api/midterm/{n}")
def midterm_start(n: int, request: Request, user: dict | None = Depends(optional_user)):
    if n not in midterm.PAPERS:
        raise HTTPException(status_code=404, detail="There is no such midterm.")
    if not (user and user["is_pro"]):                # free accounts (and guests) may sit a limited number of papers per day
        owner = f"u:{user['id']}" if user else f"ip:{client_ip(request)}"
        allowed, _used = db.consume_paper(owner, FREE_PAPERS_PER_DAY)
        if not allowed:
            return JSONResponse(status_code=403, content={
                "code": "paper_limit",
                "detail": f"The free plan includes {FREE_PAPERS_PER_DAY} mid-term paper per day. PRO has no limit. A new free paper opens at 00:00 UTC."})
    paper = midterm.build(n)
    if paper is None:
        raise HTTPException(status_code=404, detail="There is no such midterm.")
    return paper


@app.post("/api/midterm/grade")
def midterm_grade(body: PaperSubmit, user: dict | None = Depends(optional_user)):
    """Marks the whole paper at once; the answers are revealed only now. A paper can be handed in once (like /api/check: first one wins).
    The Score (rating) is not affected by the mid-term test."""
    try:
        result = midterm.grade_paper([i.model_dump() for i in body.items])
    except grading.TokenError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not db.claim_task("paper:" + result["exam_id"], user["id"] if user else None):
        raise HTTPException(status_code=409, detail="This paper has already been handed in. Start a new one.")
    if user:                                         # every question of the paper feeds the statistics and the notebook
        for item in body.items:
            try:
                record_study(user, grading.open_token(item.token), item.answers)
            except grading.TokenError:
                pass
    return result


# ---------------- PRO: the study desk (plan, mistake notebook, spaced repetition) and the AI tutor ----------------
def require_pro(user: dict) -> dict:
    if not user["is_pro"]:
        raise HTTPException(status_code=403, detail="This is a PRO feature.")
    return user


def _action_for(skill: str) -> dict:
    """What button starts practice for a skill: a table type, the classic sheet or the terminology test."""
    by_gen = {t["generator"]: t for t in table_types.TABLE_TYPES if t.get("generator")}
    if skill in by_gen:
        t = by_gen[skill]
        return {"action": "table", "id": t["id"], "label": f"Practise {t['short']}"}
    if skill == "classic_balance_sheet":
        return {"action": "table", "id": "classic", "label": "Practise the classic sheet"}
    return {"action": "topic", "topic": "terminology", "label": "Practise terminology"}


def build_plan(user_id: int) -> list[dict]:
    """Today's practice, from the statistics alone (no tokens): the weakest skills first, then a table type not tried yet."""
    skills = db.get_skills(user_id)
    weak = sorted((s for s in skills if s["accuracy"] is not None and s["accuracy"] < 85), key=lambda s: (s["accuracy"], s["updated_at"]))[:3]
    plan = [{"skill": s["skill"], "title": s["title"], "accuracy": s["accuracy"], "attempts": s["attempts"], "reason": "weak", **_action_for(s["skill"])} for s in weak]
    tried = {s["skill"] for s in skills}
    for t in table_types.TABLE_TYPES:
        if len(plan) >= 4:
            break
        if t.get("generator") and t["generator"] not in tried:
            plan.append({"skill": t["generator"], "title": t["title"], "accuracy": None, "attempts": 0, "reason": "new", "action": "table", "id": t["id"], "label": f"Try {t['short']}"})
    return plan


@app.get("/api/study/overview")
def study_overview(user: dict = Depends(current_user)):
    mistakes_all, mistakes_due = db.count_mistakes(user["id"])
    base = {"pro": bool(user["is_pro"]), "mistakes": mistakes_all, "due": mistakes_due}
    if not user["is_pro"]:
        return base                                  # a free account only sees that its mistakes are being saved
    skills = sorted(db.get_skills(user["id"]), key=lambda s: (s["accuracy"] if s["accuracy"] is not None else 101))
    return {**base, "skills": skills, "plan": build_plan(user["id"]), "ai_left": tutor.left(user["id"]), "ai_limits": tutor.LIMITS, "tutor_on": not tutor.DISABLED}


@app.get("/api/notebook")
def notebook_list(skill: str = "", limit: int = 30, offset: int = 0, user: dict = Depends(current_user)):
    require_pro(user)
    items, total = db.list_mistakes(user["id"], skill or None, max(1, min(limit, 100)), max(0, offset))
    return {"items": items, "total": total}


@app.get("/api/notebook/quiz")
def notebook_quiz(user: dict = Depends(current_user)):
    require_pro(user)
    items, total_due = db.due_mistakes(user["id"], 10)
    return {"items": items, "due": total_due}


class NotebookReview(BaseModel):
    right: bool


@app.post("/api/notebook/{mistake_id}/review")
def notebook_review(mistake_id: int, body: NotebookReview, user: dict = Depends(current_user)):
    require_pro(user)
    result = db.review_mistake(user["id"], mistake_id, body.right)
    if result is None:
        raise HTTPException(status_code=404, detail="No such mistake.")
    return result


@app.delete("/api/notebook/{mistake_id}")
def notebook_delete(mistake_id: int, user: dict = Depends(current_user)):
    require_pro(user)
    if not db.delete_mistake(user["id"], mistake_id):
        raise HTTPException(status_code=404, detail="No such mistake.")
    return {"ok": True}


class TutorItem(BaseModel):
    token: str = Field(max_length=60000)
    answers: dict = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class TutorChat(TutorItem):
    messages: list[ChatMessage] = Field(max_length=40)


class TutorPlan(BaseModel):
    items: list[TutorItem] | None = Field(default=None, max_length=30)      # the questions of a paper that was just handed in


def _tutor_payload(token: str) -> dict:
    try:
        return grading.open_token(token)
    except grading.TokenError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _tutor_call(fn, *args):
    try:
        return fn(*args)
    except tutor.TutorError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@app.post("/api/tutor/explain")
def tutor_explain(body: TutorItem, user: dict = Depends(current_user)):
    """Explains the student's mistakes in a checked task. Only after the check: before it nothing about the right answers is said."""
    require_pro(user)
    payload = _tutor_payload(body.token)
    if not db.has_claim(claim_key(payload)):
        raise HTTPException(status_code=409, detail="Check the answers first, then ask for an explanation.")
    items = midterm.review_items(payload, body.answers)
    text = _tutor_call(tutor.explain, user["id"], payload, items)
    return {"text": text, "ai_left": tutor.left(user["id"])}


@app.post("/api/tutor/chat")
def tutor_chat(body: TutorChat, user: dict = Depends(current_user)):
    """Chat about the task in hand. Before the check the tutor only coaches (it is not given the answers); after it, it can explain everything."""
    require_pro(user)
    payload = _tutor_payload(body.token)
    checked = db.has_claim(claim_key(payload))
    items = midterm.review_items(payload, body.answers) if checked else None
    text = _tutor_call(tutor.chat, user["id"], payload, items, checked, [m.model_dump() for m in body.messages])
    return {"text": text, "checked": checked, "ai_left": tutor.left(user["id"])}


@app.post("/api/tutor/plan")
def tutor_plan(body: TutorPlan, user: dict = Depends(current_user)):
    """A short written study plan from the statistics (and, right after a paper, from that paper's result)."""
    require_pro(user)
    lines = ["Skills (accuracy over attempts):"]
    skills = sorted(db.get_skills(user["id"]), key=lambda x: (x["accuracy"] if x["accuracy"] is not None else 101))
    lines += [f"- {x['title']}: {x['accuracy']}% over {x['attempts']} attempts" for x in skills[:10]] or ["- no practice yet"]
    mistakes, total = db.list_mistakes(user["id"], None, 6, 0)
    lines.append(f"Notebook: {total} saved mistakes. Recent: " + "; ".join(f"{m['title']}: {m['label'][:70]}" for m in mistakes) if mistakes else "Notebook: empty.")
    if body.items:
        payloads = [(_tutor_payload(i.token), i.answers) for i in body.items]
        if not all(db.has_claim(claim_key(p)) for p, _a in payloads):
            raise HTTPException(status_code=409, detail="Hand in the paper first.")
        lines.append("Last mid-term paper:")
        for p, a in sorted(payloads, key=lambda x: x[0].get("no", 0)):
            r = midterm.grade_one(p, a)
            wrong = [i["label"][:60] for i in midterm.review_items(p, a) if not i["correct"]][:3]
            lines.append(f"- Q{p.get('no')} {p.get('ttl', '')}: {r['points']}/{r['max']}" + (f"; wrong: {', '.join(wrong)}" if wrong else ""))
    text = _tutor_call(tutor.study_plan, user["id"], "\n".join(lines))
    return {"text": text, "ai_left": tutor.left(user["id"])}


@app.get("/api/admin/ai-usage")
def admin_ai_usage(user: dict = Depends(current_user)):
    """Today's AI spending by feature and the biggest users (only usernames listed in ADMIN_USERS)."""
    if user["username"].lower() not in ADMIN_USERS:
        raise HTTPException(status_code=403, detail="Not allowed.")
    report = db.ai_usage_report()
    report["limits"] = {"calls_per_user_per_day": tutor.LIMITS, "tokens_per_user_per_day": tutor.USER_TOKENS, "tokens_everybody_per_day": tutor.GLOBAL_TOKENS, "tutor_disabled": tutor.DISABLED}
    return report


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
    # Tolerates a value pasted as Markdown, e.g. "https://a.com](https://a.com)": keep only the first URL.
    raw = os.getenv("PUBLIC_URL", "https://accountingtrainer.app")
    match = re.match(r"https?://[^\s\]\[()<>\"']+", raw.strip())
    return match.group(0).rstrip("/") if match else "https://accountingtrainer.app"


STARTED_AT = time.time()


@app.get("/api/health")
def health():
    """For uptime monitors and for the 'everything is on fire' runbook. Reports only yes/no flags, never values."""
    try:
        with db.connect() as c:
            c.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        database = True
    except Exception as e:
        print(f"[health] database check failed: {e!r}")
        database = False
    lemon = lemon_config()
    body = {
        "ok": database,
        "database": database,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "persistent_db_path": bool(os.environ.get("DB_PATH")),      # False on Render = the data is wiped on every deploy
        "configured": {
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "resend": email_service.is_configured(),
            "lemon_squeezy": bool(lemon["api_key"] and lemon["store_id"] and lemon["variant_id"] and lemon["secret"]),
            "x_login": twitter_auth.is_configured(),
            "task_secret": bool(os.environ.get("TASK_SECRET")),     # False = open tasks break after every restart
        },
    }
    return JSONResponse(body, status_code=200 if database else 503)


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


def with_solved_flag(sealed: dict, user: dict | None) -> dict:
    """Adds {"solved": {"seen": n, "total": m}} when a signed-in student has already checked (part of) this task."""
    if user:
        fp = grading.open_token(sealed["token"]).get("fp", [])
        seen = db.count_solved(user["id"], fp)
        if seen:
            sealed["solved"] = {"seen": seen, "total": len(fp)}
    return sealed


@app.get("/api/generate/{topic}")
def generate_task(topic: str, exclude: str = "", variant: str = "", user: dict | None = Depends(optional_user)):
    """Free mode: tasks come from the local pools (no API cost); other topics fall back to Claude.
    variant: "compile" or "totals" picks the table type of a balance sheet task (default: a mix)."""
    recent = {x for x in exclude.split(",") if x}
    task = pool.get_task(topic, recent, variant if variant in ("compile", "totals") else None)
    if task is not None:
        return with_solved_flag(grading.seal(task, topic, "free"), user)
    if topic not in FREE_TOPICS:
        raise HTTPException(status_code=404, detail=f"Unknown topic '{topic}'. Available: {', '.join(FREE_TOPICS)}")
    # only reached if a pool file is missing: keep the site working, still limited by the per-IP rate limit
    try:
        return grading.seal(generate_task_from_claude(topic), topic, "free")
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

    over = db.ai_over_budget(user["id"], tutor.USER_TOKENS * 4, tutor.GLOBAL_TOKENS)       # generation is bigger than a tutor reply: its own, wider per-user cap
    if over == "global":
        raise HTTPException(status_code=503, detail="AI tasks are resting for today. Try again after 00:00 UTC.")
    if over == "user_tokens":
        raise HTTPException(status_code=429, detail="You have reached today's AI limit. It resets at 00:00 UTC.")
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
            task = generate_task_from_claude(body.topic, avoid=avoid, usage_cb=lambda i, o: db.ai_record(user["id"], "generate", i, o, count_call=True))
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
    return grading.seal(task, body.topic, "pro", fp=[f"pro:{key}"])


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
