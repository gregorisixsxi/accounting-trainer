import json
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# override=True: пустая переменная ANTHROPIC_API_KEY в системе иначе блокирует значение из .env.
# Путь явный, поэтому не зависит от папки, из которой запущен uvicorn.
load_dotenv(ENV_PATH, override=True)

# Для быстрого теста можно вставить ключ сюда (НЕ коммитьте файл с ключом в git!).
HARDCODED_API_KEY = ""

MAX_TOKENS = 4000
MAX_ATTEMPTS = 3 # одна повторная попытка, если ответ обрезан или JSON невалиден

MODEL =os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")

TOPIC_PROMPTS = {
    "income_statement": (
        "Generate an Income Statement practice exercise (Nature of expense or Function of "
        "expense method) similar to introductory financial accounting tasks. Use data in million HUF."
    ),
    "balance_sheet": (
        "Generate a Balance Sheet practice exercise with mixed assets, liabilities, and equity "
        "items in thousand HUF, requiring the student to compile a formal balance sheet."
    ),
    "terminology": (
        "Generate a short accounting terminology and concepts quiz in the style of an "
        "introductory accounting course (e.g. definitions of asset, liability, equity, accrual, "
        "depreciation, matching principle, revenue recognition). Provide 4 questions; description "
        "is a one-sentence intro, each answer is a concise definition or term."
    ),
    "transactions": (
        "Generate a business transaction analysis exercise showing effects on the accounting "
        "equation (Assets = Liabilities + Equity) or Journal entries."
    ),
}

BALANCE_SHEET_SYSTEM_PROMPT = (
    "You are an expert financial accounting professor. "
    "Generate a brand new, randomized balance sheet exercise matching introductory university "
    "standards. The description lists 8-12 items (cash, receivables, inventory, equipment, "
    "buildings, share capital, retained earnings, loans, payables, etc.) with amounts, in "
    "thousand HUF, and asks the student to compute the balance sheet totals. The items MUST "
    "balance: total assets = total equity and liabilities. Use whole numbers only.\n"
    "STRICT OUTPUT RULES: your entire reply must be one valid JSON object. The first character "
    "must be '{' and the last character must be '}'. No greetings, no explanations, no markdown, "
    "no code fences, no comments, no trailing commas. Numbers are plain digits, no separators.\n"
    "Use exactly this structure:\n"
    "{\n"
    '  "task_title": "string",\n'
    '  "company_name": "string",\n'
    '  "unit": "thousand HUF",\n'
    '  "description": "string (list every item with its amount, one per line, separated by \\n)",\n'
    '  "hint": "short hint about classifying items, without giving the answers",\n'
    '  "balance_sheet_key": {\n'
    '    "fixed_assets": number, "current_assets": number, "total_assets": number,\n'
    '    "equity": number, "liabilities": number, "total_equity_and_liabilities": number\n'
    "  },\n"
    '  "explanation": "2-3 sentences explaining which items belong where"\n'
    "}\n"
    "Fixed assets = non-current assets; liabilities = all long- and short-term liabilities."
)

SYSTEM_PROMPT = (
    "You are an expert financial accounting professor. "
    "Generate a brand new, randomized practice exercise matching university standards. "
    "STRICT OUTPUT RULES: your entire reply must be one valid JSON object. The first character "
    "must be '{' and the last character must be '}'. No greetings, no explanations, no markdown, "
    "no code fences, no comments, no trailing commas. Use double quotes, escape newlines inside "
    "strings as \\n, and write numbers as plain digits without thousand separators in JSON values "
    "where possible. Keep the exercise compact: at most 12 data items, at most 5 requirements, "
    "and a short solution_key (final figures only, no long step-by-step explanations). "
    "Use exactly this structure:\n"
    "{\n"
    '  "task_title": "string",\n'
    '  "company_name": "string",\n'
    '  "description": "string (the task conditions and numbers)",\n'
    '  "requirements": [{"question": "string", "hint": "short hint without the answer"}],\n'
    '  "solution_key": [{"answer": "string", "explanation": "1-2 sentences"}]\n'
    "}\n"
    "solution_key must have exactly one item per requirement, in the same order. "
    "For numeric answers, put just the number in answer (e.g. 1250, no units or separators)."
)

_client = None


def _resolve_api_key() -> str:
    key = (os.environ.get("ANTHROPIC_API_KEY") or HARDCODED_API_KEY).strip().strip("\"'")
    if not key:
        print(
            "[generator] ANTHROPIC_API_KEY не найден.\n"
            f"  Искал .env здесь: {ENV_PATH} (существует: {ENV_PATH.exists()})\n"
            "  Формат строки в .env: ANTHROPIC_API_KEY=sk-ant-...  (без кавычек и пробелов)\n"
            "  Либо задайте HARDCODED_API_KEY в generator.py для теста."
        )
        raise RuntimeError(f"ANTHROPIC_API_KEY is not set (looked in {ENV_PATH})")
    return key


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=_resolve_api_key())
    return _client


def _extract_json(text: str) -> dict:
    """Parse JSON even if the model wrapped it in ``` fences or added stray text."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model response contains no JSON object")
    return json.loads(text[start : end + 1])


BALANCE_SHEET_FIELDS = (
    "fixed_assets", "current_assets", "total_assets",
    "equity", "liabilities", "total_equity_and_liabilities",
)


def _validate_balance_sheet(data: dict) -> None:
    """Reject exercises whose answer key does not add up, so the student is never graded against a wrong key."""
    key = data.get("balance_sheet_key")
    if not isinstance(key, dict):
        raise ValueError("balance_sheet_key is missing")
    try:
        v = {f: float(key[f]) for f in BALANCE_SHEET_FIELDS}
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"balance_sheet_key is incomplete: {e!r}")
    data["balance_sheet_key"] = v
    if abs(v["fixed_assets"] + v["current_assets"] - v["total_assets"]) > 0.5:
        raise ValueError("Total assets do not equal fixed + current assets")
    if abs(v["equity"] + v["liabilities"] - v["total_equity_and_liabilities"]) > 0.5:
        raise ValueError("Total equity and liabilities do not add up")
    if abs(v["total_assets"] - v["total_equity_and_liabilities"]) > 0.5:
        raise ValueError("Balance sheet does not balance")


def generate_task_from_claude(topic: str) -> dict:
    """Generate a new accounting task via the Claude API."""
    if topic not in TOPIC_PROMPTS:
        raise KeyError(topic)

    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        response = _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=BALANCE_SHEET_SYSTEM_PROMPT if topic == "balance_sheet" else SYSTEM_PROMPT,
            messages=[{"role": "user", "content": TOPIC_PROMPTS[topic]}],
        )
        raw_text = "".join(b.text for b in response.content if b.type == "text")

        if response.stop_reason == "max_tokens":
            last_error = ValueError(f"Model response was cut off (max_tokens={MAX_TOKENS})")
            print(f"[generator] attempt {attempt + 1}: {last_error}")
            continue
        try:
            data = _extract_json(raw_text)
            if topic == "balance_sheet":
                _validate_balance_sheet(data)
            return data
        except ValueError as e:  # includes json.JSONDecodeError
            last_error = e
            print(f"[generator] attempt {attempt + 1}: bad JSON ({e}); raw start: {raw_text[:200]!r}")

    raise last_error
