import json
import os
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from generator import TOPIC_PROMPTS, generate_task_from_claude

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Accounting Trainer")

# Rate limiting по IP: максимум 10 генераций в минуту на пользователя
REQUEST_COUNTS = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # секунды
RATE_LIMIT_MAX = 10


def client_ip(request: Request) -> str:
    # За прокси Render request.client.host — это адрес прокси (один на всех),
    # реальный IP пользователя приходит в X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/generate"):
        ip = client_ip(request)
        now = time.time()
        recent = [t for t in REQUEST_COUNTS[ip] if now - t < RATE_LIMIT_WINDOW]
        if len(recent) >= RATE_LIMIT_MAX:
            REQUEST_COUNTS[ip] = recent
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please wait a minute before generating a new task."},
            )
        recent.append(now)
        REQUEST_COUNTS[ip] = recent
    return await call_next(request)


@app.get("/")
def read_root():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
def health_check():
    """Пинг, чтобы сервер не засыпал от бездействия."""
    return {"status": "ok"}


@app.get("/api/generate/{topic}")
def generate_task(topic: str):
    if topic not in TOPIC_PROMPTS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown topic '{topic}'. Available: {', '.join(TOPIC_PROMPTS)}",
        )
    try:
        return generate_task_from_claude(topic)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Model returned invalid JSON: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
