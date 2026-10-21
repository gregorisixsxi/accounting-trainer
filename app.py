import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from generator import TOPIC_PROMPTS, generate_task_from_claude

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Accounting Trainer API", version="1.0")

# Разрешаем запросы с фронтенда.
# Примечание: allow_origins=["*"] нельзя сочетать с credentials, поэтому они выключены.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "message": "Accounting Trainer API is running!"}


@app.get("/api/generate/{topic}")
def get_new_task(topic: str):
    """
    Эндпоинт для генерации задачи.
    Темы: income_statement, balance_sheet, transactions
    """
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
