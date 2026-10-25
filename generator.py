"""AI task generation (PRO mode, and the fallback when a local pool is missing).

The model is asked for a raw JSON description; exam.py validates it and turns it into the render-ready task.
Anything that does not pass validation is retried, so students only ever see consistent tasks.
"""
import json
import os
import random
import re
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

import exam

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# override=True: an empty ANTHROPIC_API_KEY in the system environment would otherwise block the .env value.
# The path is explicit, so it does not depend on the folder uvicorn is started from.
load_dotenv(ENV_PATH, override=True)

# For a quick local test you can paste the key here (NEVER commit a file containing a key!).
HARDCODED_API_KEY = ""

MAX_TOKENS = 8000          # the two-column "comparative" sheets need ~6000; only used tokens are billed
MAX_TOKENS_RETRY = 10000
MAX_ATTEMPTS = 3  # retry if the response is cut off, is invalid JSON, fails validation, or is not English

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")

# Real exam material makes the style much closer: paste 2-5 real midterm tasks (balance sheet tables,
# test questions) into midterm_examples.txt next to this file and they are shown to the model as examples.
EXAMPLES_PATH = BASE_DIR / "midterm_examples.txt"


def _load_examples() -> str:
    try:
        text = EXAMPLES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return ("\n\nREAL EXAM EXAMPLES. Imitate their structure, wording, difficulty and level of detail "
            "(do NOT copy their companies or numbers):\n" + text[:6000])


# ---------------------------------------------------------------- shared prompt parts
ENGLISH_RULE = (
    "STRICT REQUIREMENT: All generated content MUST be 100% in English. Never use Russian or any "
    "other language for company names, transaction descriptions, financial items, options, or "
    "categories. Use USD ($) for amounts and international GAAP/IFRS account names (e.g., "
    "'Accounts Payable', 'Raw Materials', 'Work in Process', 'Equipment', 'Debit:', 'Credit:'). "
    "Do NOT use numeric account codes (like 10, 20, 43). Company names must be English-style names "
    "(e.g. 'FitLife Gym & Club', 'Maple Street Bakery'). "
)

MIDTERM_CONTEXT = (
    "You write tasks for the MIDTERM exam of a first-year 'Introduction to Accounting' university course "
    "(business and economics programme, IFRS / US-GAAP style English terminology). The tasks must look like real "
    "exam tasks: precise wording, realistic companies, round numbers, one unambiguous correct answer, plausible "
    "distractors, no trick wording. Typical midterm topics: the accounting equation; structure and classification of "
    "the balance sheet (fixed assets: intangible assets, tangible assets, long-term financial investments; current "
    "assets: inventories, trade receivables, securities, cash and bank; prepaid expenses and accrued income; equity: "
    "share capital, capital reserve, retained earnings, profit for the year; provisions; long-term and short-term "
    "liabilities; accrued expenses and deferred income); the effect of business transactions on the balance sheet; "
    "income statement basics (nature-of-expense method, cost of goods sold, revenue and expense recognition, the "
    "matching principle); depreciation; accruals and deferrals; allowance for doubtful accounts; cash and cash "
    "equivalents; bank reconciliation; internal control and the fraud triangle; debit and credit rules and journal entries. "
)

OUTPUT_RULES = (
    "OUTPUT RULES: your entire reply is ONE valid JSON object. The first character must be '{' and the last '}'. "
    "No greetings, no explanations, no markdown, no code fences, no comments, no trailing commas. Double quotes only; "
    "escape line breaks inside strings as \\n. JSON number fields are plain numbers without separators or currency signs, "
    "but inside text strings write amounts the normal way, e.g. $4,000. "
    "Invent a NEW company name for every task and never reuse the example names that appear in these instructions "
    "('FitLife Gym & Club', 'Maple Street Bakery'). "
)

INDUSTRIES = [
    "a bakery", "a software studio", "a car dealership", "a hotel", "a logistics company",
    "a pharmacy", "a farm", "a fitness club", "a furniture manufacturer", "an online shop",
    "a construction firm", "a law firm", "a cinema", "a craft brewery", "a travel agency",
    "a dental clinic", "a bookstore", "a recycling plant", "a wine cellar", "a taxi company",
]

# ---------------------------------------------------------------- balance sheet tasks
BALANCE_SHEET_FORMAT = (
    "JSON FORMAT (a balance sheet table, drawn by the website exactly as you describe it):\n"
    "{\n"
    '  "task_title": "short exam-style title",\n'
    '  "company_name": "English company name",\n'
    '  "unit": "USD",\n'
    '  "layout": "two_sided" or "report",\n'
    '  "columns": ["Amount"],\n'
    '  "description": "the task statement for the student",\n'
    '  "accounts": [{"name": "Patents", "amount": 30000, "row": "Intangible assets"}],\n'
    '  "sides": [\n'
    '    {"title": "Assets", "groups": [\n'
    '        {"title": "A. Fixed assets",\n'
    '         "rows": [{"label": "Intangible assets", "values": [30000], "blank": false},\n'
    '                  {"label": "Tangible assets", "values": [280000], "blank": false}],\n'
    '         "subtotal": {"label": "Total fixed assets", "blank": true}},\n'
    '        {"title": "B. Current assets", "rows": [...], "subtotal": {"label": "Total current assets", "blank": true}}],\n'
    '     "total": {"label": "Total assets", "blank": true}},\n'
    '    {"title": "Equity and liabilities", "groups": [{"title": "C. Equity", ...}, {"title": "D. Liabilities", ...}],\n'
    '     "total": {"label": "Total equity and liabilities", "blank": true}}\n'
    "  ],\n"
    '  "hint": "one short hint that does not reveal an answer",\n'
    '  "explanation": "2-4 sentences showing how every blank cell is calculated, with the numbers"\n'
    "}\n"
    "RULES:\n"
    "- Exactly two sides: Assets, and Equity and liabilities. Each has groups (with a lettered title), each group has rows.\n"
    "- Every row has ONE value per column. 'blank': true means the student must type that value; false means it is shown. "
    "For multi-column sheets 'blank' may be a list with one true/false per column, e.g. [false, true].\n"
    "- Do NOT give values for subtotals and totals: the website computes them. You only choose their labels and whether they are blank.\n"
    "- The sheet MUST balance in every column: the sum of all Assets rows = the sum of all Equity and liabilities rows. Check your arithmetic.\n"
    "- Use realistic captions and VARY the set of rows between tasks (use only the groups that fit the company; do not always use every caption). "
    "Assets captions: Intangible assets; Tangible assets (property, plant and equipment); Long-term financial investments; Inventories; "
    "Trade receivables; Other receivables; Securities; Cash and bank; Prepaid expenses and accrued income. "
    "Equity and liabilities captions: Share capital; Capital reserve; Retained earnings; Profit for the year; Provisions; "
    "Long-term liabilities (bank loans, bonds); Short-term liabilities (trade payables, short-term loans, tax payable); Accrued expenses and deferred income.\n"
    "- 8 to 16 rows in total, 1 to 24 blank cells. Amounts are whole numbers, usually multiples of 1,000 (or 100).\n"
    "- Be compact: no extra fields, 'description' at most 4 sentences, 'explanation' at most 5 sentences.\n"
    "- 'layout' is 'two_sided' (assets left, equity and liabilities right) for one column, 'report' for several columns.\n"
    "- Include the 'accounts' list ONLY for the COMPILE variant."
)

BALANCE_SHEET_VARIANTS = {
    "compile": (
        "VARIANT COMPILE (prepare the balance sheet from ledger balances). Every LINE ROW has blank=true and every subtotal and total has blank=true. "
        "Provide 'accounts': 10-16 ledger accounts (like 'Patents', 'Machinery', 'Merchandise', 'Accounts receivable from customers', 'Bank account', "
        "'Petty cash', 'Subscribed capital', 'Bank loan (5 years)', 'Accounts payable') each mapped through 'row' to EXACTLY ONE row label of the table "
        "(copy the label character for character). Several accounts may belong to one row. The accounts mapped to a row must add up exactly to that row's value. "
        "The student must classify each account, add them up per row and compute subtotals and totals. In the description just say what to prepare (no numbers)."
    ),
    "missing_item": (
        "VARIANT MISSING ITEM. All line rows are shown (blank=false) except 1 or 2 rows with blank=true; all subtotals and totals are shown (blank=false). "
        "The student finds the missing line(s) from the shown subtotals/totals and the accounting equation. The description names the company and explains what to find."
    ),
    "equation": (
        "VARIANT ACCOUNTING EQUATION. Show only part of the sheet: mark 3 to 6 cells blank (for example total assets, one equity or liability row and a subtotal) "
        "so that each blank can be derived from the shown cells using Assets = Equity + Liabilities and the subtotal sums. "
        "The description briefly tells the story of the company (e.g. owners' investment, borrowing)."
    ),
    "comparative": (
        "VARIANT COMPARATIVE (two balance sheet dates). Use two columns such as [\"Dec 31, Year 1\", \"Dec 31, Year 2\"] and layout 'report'. "
        "Both columns must balance. Mark 3 to 6 cells blank, in DIFFERENT rows and columns, each derivable from the shown cells and the equation. "
        "The description may add a fact consistent with the table (for example that retained earnings grew by the year's profit)."
    ),
    "after_transactions": (
        "VARIANT EFFECT OF TRANSACTIONS. Use two columns [\"Before\", \"After\"] and layout 'report'. The description lists 2 or 3 simple transactions with amounts "
        "(for example: bought equipment for 20,000 paid from the bank; took a bank loan of 50,000; paid 10,000 to a supplier). The Before column is fully shown. "
        "The After column reflects the transactions; set blank to [false, true] on the rows and subtotals/totals whose After value changes or must be recomputed, "
        "and [false, false] where nothing changes. Both columns must balance."
    ),
}

BALANCE_SHEET_SYSTEM = (
    ENGLISH_RULE + MIDTERM_CONTEXT + OUTPUT_RULES + "\n\n" + BALANCE_SHEET_FORMAT
)

# ---------------------------------------------------------------- tests (terminology, journal entries)
TEST_FORMAT = (
    "JSON FORMAT (a multiple-choice style test):\n"
    "{\n"
    '  "task_title": "Terminology: Midterm Practice Test",\n'
    '  "company_name": "Multiple choice · 8 questions",\n'
    '  "description": "one sentence of instructions",\n'
    '  "questions": [\n'
    '    {"kind": "mcq", "prompt": "...", "options": ["...", "...", "...", "..."], "correct": 2, "explanation": "one sentence"},\n'
    '    {"kind": "multi", "prompt": "Select ALL that apply: ...", "options": ["...", "...", "...", "...", "..."], "correct": [0, 3], "explanation": "..."},\n'
    '    {"kind": "tf", "prompt": "True or false: ...", "correct": true, "explanation": "..."},\n'
    '    {"kind": "numeric", "prompt": "... Calculate ...", "unit": "USD", "correct": 12500, "tolerance": 0, "explanation": "show the calculation"}\n'
    "  ]\n"
    "}\n"
    "RULES:\n"
    "- 'correct' for mcq is the index (starting at 0) of the single right option; for multi a list of 2 or more indexes (never all options); for tf true or false; for numeric the number.\n"
    "- mcq has 4 options, multi has 5. Options must be plausible, similar in length and style; the right option must NOT be systematically the longest or in a fixed position. "
    "Do not use 'all of the above' or 'none of the above'.\n"
    "- Mix exam-like phrasings: 'Which of the following...', 'What is the effect of ... on ...?', 'Which statement is NOT correct?', scenario questions with small numbers.\n"
    "- Numeric questions must have ONE exact answer from a short calculation (e.g. equity from the accounting equation, depreciation, cost of goods sold, net income, bank reconciliation balance).\n"
    "- Every question tests a different concept. Keep each prompt under 280 characters."
)

TEST_SYSTEM = ENGLISH_RULE + MIDTERM_CONTEXT + OUTPUT_RULES + "\n\n" + TEST_FORMAT

TEST_TOPICS = {
    "terminology": (
        "Create ONE test of 8 questions on accounting terminology and concepts: 5 mcq, 1 multi, 1 tf and 1 numeric. "
        "Title it 'Terminology: Midterm Practice Test'."
    ),
    "transactions": (
        "Create ONE test of 8 questions on journal entries and on the effects of business transactions on assets, liabilities and equity: "
        "5 mcq (the options are journal entries written as 'Debit: Account | Credit: Account'), 1 multi, 1 tf and 1 numeric. "
        "Title it 'Journal Entries: Midterm Practice Test'."
    ),
}

TOPIC_PROMPTS = {"balance_sheet": "balance sheet", **TEST_TOPICS}   # topics this generator can produce

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


def _avoid_text(avoid: list[str] | None) -> str:
    if not avoid:
        return ""
    listed = "\n".join(f"- {a}" for a in avoid)
    return (
        "\n\nThe student has ALREADY solved the tasks below. Create a clearly different one: different company, "
        "industry or scenario, rows, numbers and (for tests) different questions and concepts. "
        "Do not reuse anything from this list:\n" + listed
    )


def build_prompts(topic: str, avoid: list[str] | None = None, variant: str | None = None) -> tuple[str, str, str | None]:
    """Returns (system prompt, user prompt, balance-sheet variant)."""
    examples = _load_examples()
    if topic == "balance_sheet":
        variant = variant or random.choice(list(BALANCE_SHEET_VARIANTS))
        user = "Create ONE new balance sheet task.\n" + BALANCE_SHEET_VARIANTS[variant]
        if avoid is not None:
            user += f" Set the company in this industry: {random.choice(INDUSTRIES)}."
        return BALANCE_SHEET_SYSTEM + examples, user + _avoid_text(avoid), variant
    return TEST_SYSTEM + examples, TEST_TOPICS[topic] + _avoid_text(avoid), None


def _normalize(topic: str, raw: dict, variant: str | None) -> dict:
    if topic == "balance_sheet":
        return exam.normalize_balance_sheet(raw, variant)
    return exam.normalize_test(raw, min_questions=5)


def generate_task_from_claude(topic: str, avoid: list[str] | None = None) -> dict:
    """Generate a new exam-style task via the Claude API.

    avoid=None is the free fallback (no history); a list (even empty) is the PRO mode where the model
    is told which tasks the user has already seen.
    """
    if topic not in TOPIC_PROMPTS:
        raise KeyError(topic)
    system, user, variant = build_prompts(topic, avoid)

    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        response = _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS if attempt == 0 else MAX_TOKENS_RETRY,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        raw_text = "".join(b.text for b in response.content if b.type == "text")

        if response.stop_reason == "max_tokens":
            last_error = ValueError("Model response was cut off (max_tokens)")
            print(f"[generator] attempt {attempt + 1} ({variant or topic}): cut off after {len(raw_text)} characters; tail: {raw_text[-120:]!r}")
            continue
        try:
            _ensure_english(raw_text)
            return _normalize(topic, _extract_json(raw_text), variant)
        except ValueError as e:  # json errors, non-English text and exam.FormatError are all ValueErrors
            last_error = e
            print(f"[generator] attempt {attempt + 1} ({variant or topic}): rejected ({e}); raw start: {raw_text[:160]!r}")

    raise last_error
