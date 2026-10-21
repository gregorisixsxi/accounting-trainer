import os
import time
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from generator import generate_accounting_task, validate_balance_sheet

load_dotenv()

app = FastAPI(title="Accounting Trainer")

# Простой и надежный Rate Limiting по IP (максимум 10 запросов в минуту на генерацию)
REQUEST_COUNTS = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # секунды
RATE_LIMIT_MAX = 10     # количество запросов

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/generate"):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        
        # Очищаем старые запросы вне окна
        REQUEST_COUNTS[client_ip] = [t for t in REQUEST_COUNTS[client_ip] if now - t < RATE_LIMIT_WINDOW]
        
        if len(REQUEST_COUNTS[client_ip]) >= RATE_LIMIT_MAX:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please wait a minute before generating a new task."}
            )
        
        REQUEST_COUNTS[client_ip].append(now)
        
    response = await call_next(request)
    return response

class ValidationRequest(BaseModel):
    user_answers: dict
    correct_key: dict

@app.get("/", response_class=HTMLResponse)
async def read_root():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "index.html not found", 404

@app.get("/health")
async def health_check():
    """Эндпоинт для пинга, чтобы сервер не засыпал от бездействия"""
    return {"status": "ok"}

@app.get("/api/generate/{task_type}")
async def generate_task(task_type: str):
    try:
        task = generate_accounting_task(task_type)
        return task
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/validate")
async def validate_task(data: ValidationRequest):
    try:
        result = validate_balance_sheet(data.user_answers, data.correct_key)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)