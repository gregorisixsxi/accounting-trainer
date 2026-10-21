import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import db
import pool
from generator import TOPIC_PROMPTS, generate_task_from_claude

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Accounting Trainer")
db.init()

# Rate limiting по IP. Бакеты: генерация задач и попытки входа (защита от перебора паролей).
RATE_LIMITS = {
    "generate": (10, 60),  # 10 запросов в минуту
    "auth": (10, 60),
}
REQUEST_COUNTS = defaultdict(list)


def client_ip(request: Request) -> str:
    # За прокси Render request.client.host — это адрес прокси (один на всех),
    # реальный IP пользователя приходит в X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def bucket_for(path: str) -> str | None:
    if path.startswith("/api/generate"):
        topic = path.rsplit("/", 1)[-1]
        # задачи из локального пула бесплатны для нас, лимит нужен только для вызовов Claude API
        return None if topic in pool.POOL_TOPICS and path != "/api/generate/pro" else "generate"
    if path in ("/api/login", "/api/register"):
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
    return await call_next(request)


# ---------------- auth ----------------
USERNAME_RE = re.compile(r"^\S.{1,62}\S$")  # 3-64 символа, любая строка (логин или e-mail)


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
    return {"username": user["username"], "is_pro": user["is_pro"]}


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


# ---------------- pages / health ----------------
@app.get("/")
def read_root():
    # no-cache: после деплоя пользователи сразу получают свежую страницу, а не закешированную
    return FileResponse(BASE_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health_check():
    """Пинг, чтобы сервер не засыпал от бездействия."""
    return {"status": "ok"}


# ---------------- task generation ----------------
@app.get("/api/generate/{topic}")
def generate_task(topic: str, exclude: str = ""):
    """Бесплатный режим: пул из tasks_pool.json (без затрат API), остальные темы — как раньше через Claude."""
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
AVOID_LIMIT = 40  # сколько последних задач показываем модели
PRO_ATTEMPTS = 2


class ProRequest(BaseModel):
    topic: str = "balance_sheet"


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def task_summary(topic: str, task: dict) -> tuple[str, str]:
    """(описание для промпта «не повторяй», ключ для точного сравнения)."""
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
            avoid.append(summary)  # дубль: просим модель обойти и его
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
