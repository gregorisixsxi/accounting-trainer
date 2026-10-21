import json
import os
import random
import re
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# override=True: an empty ANTHROPIC_API_KEY in the system environment would otherwise block the .env value.
# The path is explicit, so it does not depend on the folder uvicorn is started from.
load_dotenv(ENV_PATH, override=True)

# For a quick local test you can paste the key here (NEVER commit a file containing a key!).
HARDCODED_API_KEY = ""

MAX_TOKENS = 4000
MAX_ATTEMPTS = 3  # retry if the response is cut off, is invalid JSON, or is not English

MODEL =os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")

TOPIC_PROMPTS = {
    "income_statement": (
        "Generate an Income Statement practice exercise (Nature of expense or Function of "
        "expense method) similar to introductory financial accounting tasks. Use USD ($) amounts, in whole numbers."
    ),
    "balance_sheet": (
        "Generate a Balance Sheet practice exercise with mixed assets, liabilities, and equity "
        "items in USD ($), requiring the student to compile a formal balance sheet. Use a realistic English company name such as 'FitLife Gym & Club' and standard English item names such as 'Unearned Revenue', 'Retained Earnings', 'Equipment', 'Accounts Receivable'."
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

ENGLISH_RULE = (
    "STRICT REQUIREMENT: All generated content MUST be 100% in English. Never use Russian or any "
    "other language for company names, transaction descriptions, financial items, options, or "
    "categories. Use USD ($) for amounts and international GAAP/IFRS account names (e.g., "
    "'Accounts Payable', 'Raw Materials', 'Work in Process', 'Equipment', 'Debit:', 'Credit:'). "
    "Do NOT use numeric account codes (like 10, 20, 43). Company names must be English-style names "
    "(e.g. 'FitLife Gym & Club', 'Maple Street Bakery'). "
)

BALANCE_SHEET_SYSTEM_PROMPT = (
    ENGLISH_RULE +
    "You are an expert financial accounting professor. "
    "Generate a brand new, randomized balance sheet exercise matching introductory university "
    "standards. The description lists 8-12 items (cash, receivables, inventory, equipment, "
    "buildings, share capital, retained earnings, loans, payables, etc.) with amounts, in "
    "USD, and asks the student to compute the balance sheet totals. The items MUST "
    "balance: total assets = total equity and liabilities. Use whole numbers only.\n"
    "STRICT OUTPUT RULES: your entire reply must be one valid JSON object. The first character "
    "must be '{' and the last character must be '}'. No greetings, no explanations, no markdown, "
    "no code fences, no comments, no trailing commas. Numbers are plain digits, no separators.\n"
    "Use exactly this structure:\n"
    "{\n"
    '  "task_title": "string",\n'
    '  "company_name": "string",\n'
    '  "unit": "USD",\n'
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
    ENGLISH_RULE +
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
            "[generator] ANTHROPIC_API_KEY not found.\n"
            f"  Looked for .env here: {ENV_PATH} (exists: {ENV_PATH.exists()})\n"
            "  Line format in .env: ANTHROPIC_API_KEY=sk-ant-...  (no quotes, no spaces)\n"
            "  Or set HARDCODED_API_KEY in generator.py for a quick test."
        )
        raise RuntimeError(f"ANTHROPIC_API_KEY is not set (looked in {ENV_PATH})")
    return key


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=_resolve_api_key())
    return _client


_NON_ENGLISH = re.compile(r"[Ѐ-ӿ֐-ۿ一-鿿぀-ヿ]")  # Cyrillic, Hebrew/Arabic, CJK


def _ensure_english(text: str) -> None:
    if _NON_ENGLISH.search(text):
        raise ValueError("Model returned non-English content")


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


INDUSTRIES = [
    "a bakery", "a software studio", "a car dealership", "a hotel", "a logistics company",
    "a pharmacy", "a farm", "a fitness club", "a furniture manufacturer", "an online shop",
    "a construction firm", "a law firm", "a cinema", "a craft brewery", "a travel agency",
    "a dental clinic", "a bookstore", "a recycling plant", "a wine cellar", "a taxi company",
]


def _build_user_prompt(topic: str, avoid: list[str] | None) -> str:
    prompt = TOPIC_PROMPTS[topic]
    if avoid is None:
        return prompt
    if topic == "balance_sheet":
        prompt += f" Set the company in this industry: {random.choice(INDUSTRIES)}."
    if avoid:
        listed = "\n".join(f"- {a}" for a in avoid)
        prompt += (
            "\n\nThe student has ALREADY solved the tasks below. Create a clearly different one: "
            "different company, industry/scenario, items and amounts, and for quizzes different "
            "questions and terms. Do not reuse anything from this list:\n" + listed
        )
    return prompt


def generate_task_from_claude(topic: str, avoid: list[str] | None = None) -> dict:
    """Generate a new accounting task via the Claude API.

    avoid=None is the free mode (unchanged behaviour); a list (even empty) is the PRO mode
    where the model is told which tasks the user has already seen.
    """
    if topic not in TOPIC_PROMPTS:
        raise KeyError(topic)

    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        response = _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=BALANCE_SHEET_SYSTEM_PROMPT if topic == "balance_sheet" else SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(topic, avoid)}],
        )
        raw_text = "".join(b.text for b in response.content if b.type == "text")

        if response.stop_reason == "max_tokens":
            last_error = ValueError(f"Model response was cut off (max_tokens={MAX_TOKENS})")
            print(f"[generator] attempt {attempt + 1}: {last_error}")
            continue
        try:
            _ensure_english(raw_text)
            data = _extract_json(raw_text)
            if topic == "balance_sheet":
                _validate_balance_sheet(data)
            return data
        except ValueError as e:  # includes json.JSONDecodeError
            last_error = e
            print(f"[generator] attempt {attempt + 1}: bad JSON ({e}); raw start: {raw_text[:200]!r}")

    raise last_error
