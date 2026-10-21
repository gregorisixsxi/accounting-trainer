"""Бесплатный режим: задачи из локального tasks_pool.json, без обращений к Claude API.

Файл читается один раз при старте. Каждая задача проверяется; некорректные пропускаются
(с сообщением в лог), чтобы одна ошибка в JSON не ломала сайт.
"""
import json
import random
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TASKS_PATH = BASE_DIR / "tasks_pool.json"            # balance_sheet, transactions
TERMINOLOGY_PATH = BASE_DIR / "terminology_pool.json"  # terminology
CURRENCY = "USD"

# Статьи, которые относятся к внеоборотным активам (Fixed Assets). Всё остальное в разделе assets — оборотные.
FIXED_ASSETS = {
    "Buildings", "Facility Building", "Cleaning Equipment", "Computers & Servers", "Construction Tools",
    "Delivery Trucks", "Delivery Vans", "Display Shelves", "Domain & Website", "Exercise Equipment",
    "Furniture", "Kitchen Equipment", "Machinery", "Office Hardware", "Ovens & Mixers", "Server Hardware",
    "Sewing Machines", "Software Licenses", "Store Equipment", "Store Fixtures", "Cash Register",
}

BS_HINT = (
    "Fixed assets are long-lived items you use (equipment, buildings, vehicles, software). "
    "Current assets are cash and things turned into cash within a year (receivables, inventory, supplies). "
    "Remember: Assets = Liabilities + Equity."
)
TR_HINT = (
    "Debits increase assets and expenses; credits increase liabilities, equity and revenue. "
    "Ask yourself which two accounts change, and in which direction."
)


def _valid_balance_sheet(t: dict) -> bool:
    try:
        a, l, e = (sum(t[k].values()) for k in ("assets", "liabilities", "equity"))
        return bool(t["assets"]) and a == l + e
    except Exception:
        return False


def _valid_options(t: dict) -> bool:
    o = t["options"]
    return t["correct_answer"] in o and len(set(o)) == len(o) >= 2


def _valid_transaction(t: dict) -> bool:
    try:
        return _valid_options(t) and bool(t["description"])
    except Exception:
        return False


def _valid_terminology(t: dict) -> bool:
    try:
        return _valid_options(t) and bool(t["question"])
    except Exception:
        return False


def _load() -> dict[str, list[dict]]:
    sources = (
        (TASKS_PATH, "balance_sheet", _valid_balance_sheet),
        (TASKS_PATH, "transactions", _valid_transaction),
        (TERMINOLOGY_PATH, "terminology", _valid_terminology),
    )
    files: dict[Path, dict] = {}
    pool: dict[str, list[dict]] = {}
    for path, topic, check in sources:
        if path not in files:
            try:
                files[path] = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[pool] cannot read {path.name}: {e}")
                files[path] = {}
        good, seen = [], set()
        for t in files[path].get(topic, []):
            if isinstance(t, dict) and t.get("id") not in seen and "id" in t and check(t):
                good.append(t)
                seen.add(t["id"])
            else:
                print(f"[pool] skipped invalid {topic} task: {t.get('id') if isinstance(t, dict) else t!r}")
        pool[topic] = good
    return pool


POOL = _load()
POOL_TOPICS = {t for t, items in POOL.items() if items}


def _pick(topic: str, exclude: set[str]) -> dict:
    items = POOL[topic]
    fresh = [t for t in items if t["id"] not in exclude]
    return random.choice(fresh or items)


def _balance_sheet_task(t: dict) -> dict:
    assets, liabilities, equity = t["assets"], t["liabilities"], t["equity"]
    fixed = sum(v for k, v in assets.items() if k in FIXED_ASSETS)
    total_assets = sum(assets.values())
    total_liab, total_eq = sum(liabilities.values()), sum(equity.values())

    items = list({**assets, **liabilities, **equity}.items())
    random.shuffle(items)  # статьи без разделов: студент сам классифицирует
    lines = "\n".join(f"{name}: {value:,}" for name, value in items)
    return {
        "id": t["id"],
        "task_title": "Prepare a Balance Sheet",
        "company_name": t["title"],
        "unit": CURRENCY,
        "description": (
            f"{t['title']} reports the following items at year-end (in {CURRENCY}). "
            f"Classify them and fill in the balance sheet totals.\n\n{lines}"
        ),
        "hint": BS_HINT,
        "balance_sheet_key": {
            "fixed_assets": fixed,
            "current_assets": total_assets - fixed,
            "total_assets": total_assets,
            "equity": total_eq,
            "liabilities": total_liab,
            "total_equity_and_liabilities": total_liab + total_eq,
        },
        "explanation": (
            f"Fixed assets: {', '.join(k for k in assets if k in FIXED_ASSETS) or 'none'}. "
            f"Current assets: {', '.join(k for k in assets if k not in FIXED_ASSETS)}. "
            f"Assets {total_assets:,} = Liabilities {total_liab:,} + Equity {total_eq:,}."
        ),
    }


def _transaction_task(t: dict) -> dict:
    options = list(t["options"])
    random.shuffle(options)  # в исходном файле верный ответ почти всегда первый
    return {
        "id": t["id"],
        "task_title": t["title"],
        "company_name": "Journal entry",
        "description": t["description"],
        "hint": TR_HINT,
        "options": options,
        "correct_answer": t["correct_answer"],
        "choose_label": "Choose the correct journal entry:",
    }


def _terminology_task(t: dict) -> dict:
    options = list(t["options"])
    random.shuffle(options)
    return {
        "id": t["id"],
        "task_title": "Terminology & Concepts",
        "company_name": "Quick question",
        "description": t["question"],
        "options": options,
        "correct_answer": t["correct_answer"],
        "choose_label": "Choose the correct answer:",
    }


def get_task(topic: str, exclude: set[str] | None = None) -> dict | None:
    """Возвращает задачу в формате фронтенда или None, если для темы нет пула."""
    if topic not in POOL_TOPICS:
        return None
    t = _pick(topic, exclude or set())
    builders = {"balance_sheet": _balance_sheet_task, "transactions": _transaction_task, "terminology": _terminology_task}
    return builders[topic](t)
