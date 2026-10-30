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

MAX_TOKENS = 5000          # a task is ~2000 output tokens; only used tokens are billed
MAX_TOKENS_RETRY = 8000

# The balancing arithmetic is done by exam.py (plug row / plug item) and calculation questions are verified there too,
# so the model does not need hidden reasoning: with it a sheet took 6000+ tokens and 30-90 s instead of ~2000 tokens and 7 s.
# Set ANTHROPIC_THINKING=on to let the model think anyway.
THINKING_ARGS = {} if os.getenv("ANTHROPIC_THINKING", "").lower() in ("1", "true", "on", "yes") else {"thinking": {"type": "disabled"}}
MAX_ATTEMPTS = 3  # retry if the response is cut off, is invalid JSON, fails validation, or is not English

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")

# More real exam material makes the style closer: paste 2-5 real midterm tasks into midterm_examples.txt
# next to this file and they are shown to the model as additional examples (keep that file out of public repositories).
EXAMPLES_PATH = BASE_DIR / "midterm_examples.txt"


def _load_examples(topic: str = "") -> str:
    # midterm_templates.py holds the real midterm tasks with their rules (types, wording, logic, kind of wrong options)
    try:
        import midterm_templates
        templates = midterm_templates.examples_for(topic)
    except Exception as e:  # a broken or missing templates file must never stop task generation
        print(f"[generator] midterm_templates not used: {e}")
        templates = ""
    try:
        text = EXAMPLES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return templates
    if not text:
        return templates
    return templates + ("\n\nREAL EXAM EXAMPLES. Imitate their structure, wording, difficulty and level of detail "
                        "(do NOT copy their companies or numbers):\n" + text[:6000])


# ---------------------------------------------------------------- shared prompt parts
ENGLISH_RULE = (
    "STRICT REQUIREMENT: All generated content MUST be 100% in English. Never use Russian or any "
    "other language for company names, transaction descriptions, financial items, options, or "
    "categories. Use USD ($) for amounts (in balance sheet tables the amounts are in THOUSAND USD, see the format) and international GAAP/IFRS account names (e.g., "
    "'Accounts Payable', 'Raw Materials', 'Work in Process', 'Equipment', 'Debit:', 'Credit:'). "
    "Do NOT use numeric account codes (like 10, 20, 43). Company names must be English-style names "
    "(e.g. 'FitLife Gym & Club', 'Maple Street Bakery'). "
)

MIDTERM_CONTEXT = (
    "You write tasks for the MIDTERM exam of a first-year 'Introduction to Accounting' university course "
    "(business and economics programme; European-style balance sheet with lettered groups and numbered lines, "
    "English terminology). The tasks must look like real exam tasks: precise wording, realistic companies, "
    "round numbers, one unambiguous correct answer, plausible distractors, no trick wording. The balance sheet is "
    "TWO-SIDED: Assets on the left, Equity and liabilities on the right. Typical midterm topics: the accounting equation; "
    "classification of items into the balance sheet lines; the effect of business events on assets, liabilities, equity and "
    "profit; income statement basics (nature-of-expense method: net sales, capitalised own output, other income, material type "
    "expenses, personnel type expenses, depreciation, other expenses, operating profit, financial income and expenses, profit "
    "before tax; the function of expense method with direct and indirect cost of sales); inventories; depreciation; accruals "
    "and deferrals; allowance for doubtful accounts; cash and cash equivalents; bank reconciliation; internal control and the fraud triangle. "
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
    "JSON FORMAT (a TWO-SIDED balance sheet, drawn by the website exactly as you describe it):\n"
    "{\n"
    '  "task_title": "short exam-style title",\n'
    '  "company_name": "English company name",\n'
    '  "unit": "thousand USD",\n'
    '  "columns": ["Amount"],\n'
    '  "description": "the task statement for the student",\n'
    '  "accounts": [{"name": "Patent purchased by the company", "amount": 30, "row": "I. Intangible assets"}],   (compile variant only)\n'
    '  "sides": [\n'
    '    {"title": "Assets", "groups": [\n'
    '        {"title": "A. Non-current assets",\n'
    '         "rows": [{"label": "I. Intangible assets", "values": [30], "blank": true},\n'
    '                  {"label": "II. Tangible assets", "values": [280], "blank": true}],\n'
    '         "subtotal": {"label": "Total non-current assets", "mode": "hidden"}},\n'
    '        {"title": "B. Current assets", "rows": [...], "subtotal": {"label": "Total current assets", "mode": "hidden"}}],\n'
    '     "total": {"label": "TOTAL ASSETS", "mode": "hidden"}},\n'
    '    {"title": "Equity and liabilities", "groups": [{"title": "D. Stockholders\' equity", ...}, {"title": "F. Liabilities", ...}],\n'
    '     "total": {"label": "TOTAL EQUITY AND LIABILITIES", "mode": "hidden"}}\n'
    "  ],\n"
    '  "hint": "one short hint that does not reveal an answer",\n'
    '  "explanation": "3-5 sentences showing how every input cell is calculated, with the numbers"\n'
    "}\n"
    "RULES:\n"
    "- Exactly two sides: 'Assets' and 'Equity and liabilities'. Each has lettered groups, each group has numbered rows.\n"
    "- Use the exam's captions. Assets: A. Non-current assets (I. Intangible assets; II. Tangible assets; III. Financial investments), "
    "B. Current assets (I. Inventories; II. Accounts receivable; III. Securities; IV. Liquid assets), C. Prepaid expenses and accrued income "
    "(a single line, put the whole caption in the row label and leave the group title empty). Equity and liabilities: D. Stockholders' equity "
    "(I. Contributed capital; II. Capital reserve; III. Retained earnings; IV. Profit after tax for the year), E. Provisions (single line), "
    "F. Liabilities (I. Long-term liabilities; II. Current liabilities), G. Accrued expenses and deferred income (single line). "
    "Use only the groups and lines that fit the company and VARY the set between tasks, but always show A and B, D and F.\n"
    "- Every row has ONE value per column. 'blank': true means the student must type that value; false means it is shown. "
    "For two-column sheets 'blank' may be a list with one true/false per column.\n"
    "- Do NOT give values for subtotals and totals: the website computes them. You only choose their labels and their 'mode': "
    "'input' (the student types it), 'given' (it is shown) or 'hidden' (a placeholder 'XXX' that is neither shown nor asked).\n"
    "- BALANCING IS DONE BY THE WEBSITE, NOT BY YOU: do not try to make both sides equal. Mark exactly ONE row of the sheet with \"plug\": true and give it NO 'values' "
    "(use a retained earnings line of the equity group, or for the compile variant the plug item described below); the website calculates it so that "
    "Assets = Equity and liabilities in every column. Choose the other amounts as plausible round numbers, and make the Equity and liabilities side without the plug "
    "clearly SMALLER than the Assets side (the plug is then a positive retained earnings figure).\n"
    "- The table must be DETAILED and exam-sized: 10 to 16 rows in total, and at least 8 cells the student has to type "
    "(rows, subtotals and totals together; at most 24). AMOUNT SCALE: the unit is thousand USD, so write SMALL whole numbers like a real exam: single items between 20 and 900, "
    "usually multiples of 5 or 10, and a total assets figure of roughly 800 to 2,500. Never write amounts like 350,000 or 1,200,000. "
    "In the description and explanation say 'thousand USD' (or 'th USD'), not '$' with extra zeros.\n"
    "- Be compact: no extra fields, 'description' at most 4 sentences, 'explanation' at most 6 sentences."
)

BALANCE_SHEET_VARIANTS = {
    "compile": (
        "VARIANT COMPILE (the real midterm task: compile the balance sheet from a list of items). "
        "Do NOT give any row 'values' and no plug row: the row values are calculated from your items. Every group subtotal and both totals have mode 'hidden'. "
        "Provide 'accounts': 12 to 16 items written descriptively, the way an exam describes them, for example "
        "'Patent purchased by the company', 'Delivery vans used for transportation', 'Share capital paid in by the owners', "
        "'Treasury bills bought as a short-term investment', 'Amounts owed by customers for goods sold', 'Cash held in the till', "
        "'Profits retained from earlier years', 'Tax payable to the authorities within one year', 'Bank loan taken for 8 years', "
        "'Profit after tax of the current year', 'Salaries owed to employees' (invent different ones; never reuse these exact sentences). "
        "Each item is mapped through 'row' to EXACTLY ONE row label of the table (copy the label character for character). "
        "Several items must belong to the same row for some rows (3 or 4 rows should combine 2 or 3 items), some rows may be 0 "
        "(a row with no items has value 0). Exactly ONE item is the plug: {\"name\": \"Profits retained from earlier years\", \"row\": \"<retained earnings row>\", \"plug\": true} "
        "without an 'amount'; the website calculates its amount so that the sheet balances. Every other item has an 'amount'. "
        "Use 8 to 12 rows. The description just tells the student to compile the balance sheet of the company; it must not contain numbers."
    ),
    "totals": (
        "VARIANT TOTALS (the table is fully shown, the student calculates the sums). All rows have blank=false (12 to 16 rows spread over "
        "A, B, D and F), one of them is the plug row. Every group subtotal and both totals have mode 'input' (that gives 6 to 10 inputs). "
        "The description names the company and says that every subtotal and total has to be calculated."
    ),
    "comparative": (
        "VARIANT COMPARATIVE (two balance sheet dates). Use two columns such as [\"Dec 31, Year 1\", \"Dec 31, Year 2\"]. "
        "Every row has two values (one per date) except the plug row, which has none. Show all rows (blank=false) and give every group subtotal and total "
        "mode 'input' so that the student calculates them for both dates (at least 8 inputs). Keep the sheet at 10 to 12 rows. "
        "The description says the student calculates all subtotals and totals for both dates."
    ),
}
BALANCE_SHEET_VARIANT_WEIGHTS = {"compile": 6, "totals": 2, "comparative": 2}

BALANCE_SHEET_SYSTEM = ENGLISH_RULE + MIDTERM_CONTEXT + OUTPUT_RULES + "\n\n" + BALANCE_SHEET_FORMAT

# ---------------------------------------------------------------- tests (terminology)
TEST_FORMAT = (
    "JSON FORMAT (a multiple-choice midterm test):\n"
    "{\n"
    '  "task_title": "Terminology: Midterm Practice Test",\n'
    '  "company_name": "Multiple choice · 8 questions",\n'
    '  "description": "Choose the best answer for each question.",\n'
    '  "questions": [\n'
    '    {"kind": "mcq", "points": 2, "prompt": "We know the following balance sheet items: Goods $3,000; Raw materials $6,000; Cash $3,000; Work in progress $4,000. What is the value of the Inventories?", '
    '"options": ["$13,000", "$16,000", "$9,000", "$7,000"], "correct": 0, "calc": "3000+6000+4000", "explanation": "Inventories = goods + raw materials + work in progress = 13,000; cash is not an inventory item."},\n'
    '    {"kind": "mcq", "points": 1, "prompt": "Which expense category includes ...?", "options": ["...", "...", "...", "..."], "correct": 2, "explanation": "one sentence"}\n'
    "  ]\n"
    "}\n"
    "RULES:\n"
    "- Exactly 8 questions, ALL of kind 'mcq' with exactly 4 options and ONE right answer ('correct' is the index starting at 0). "
    "'points' is 1 (concept questions) or 2 (calculation questions); 3 or 4 of the 8 are calculation questions.\n"
    "- Every calculation question MUST include 'calc': a plain arithmetic expression using only numbers and + - * / ( ) "
    "(for example \"60000-5000-10000-1000+2000\" or \"1000-1000*0.1\") whose result is the number in the correct option. "
    "The wrong options are typical mistakes (a forgotten item, a wrong sign, an item that does not belong). Show the calculation in the explanation.\n"
    "- Calculation ideas: the value of the inventories from a list of balance sheet items; operating profit or profit before tax from income statement items "
    "(nature of expense method); the effect of returned goods or a discount on the value of materials; the balance after a few business events; "
    "equity from assets and liabilities.\n"
    "- Concept questions use exam phrasings: 'Which of the following ...?', 'Select the incorrect statement!', 'How does the event affect ...?', "
    "'Which financial statement must be prepared first?', 'Assets are generally classified as ...'. Options are plausible, of similar length and style; "
    "the right option is not systematically the longest and not in a fixed position. Do not use 'all of the above' or 'none of the above'.\n"
    "- Every question tests a different concept. Keep each prompt under 300 characters."
)

TEST_SYSTEM = ENGLISH_RULE + MIDTERM_CONTEXT + OUTPUT_RULES + "\n\n" + TEST_FORMAT

TEST_TOPICS = {
    "terminology": "Create ONE test of 8 multiple-choice questions on accounting terminology, classification and small calculations.",
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
    examples = _load_examples(topic)
    if topic == "balance_sheet":
        variant = variant or random.choices(list(BALANCE_SHEET_VARIANT_WEIGHTS), list(BALANCE_SHEET_VARIANT_WEIGHTS.values()))[0]
        user = "Create ONE new balance sheet task.\n" + BALANCE_SHEET_VARIANTS[variant]
        if avoid is not None:
            user += f" Set the company in this industry: {random.choice(INDUSTRIES)}."
        return BALANCE_SHEET_SYSTEM + examples, user + _avoid_text(avoid), variant
    return TEST_SYSTEM + examples, TEST_TOPICS[topic] + _avoid_text(avoid), None


def _normalize(topic: str, raw: dict, variant: str | None) -> dict:
    if topic == "balance_sheet":
        return exam.normalize_balance_sheet(raw, variant)
    task = exam.normalize_test(raw, min_questions=6)
    if any(q["kind"] != "mcq" for q in task["questions"]):
        raise exam.FormatError("the test must contain only multiple-choice questions")
    return task


def generate_task_from_claude(topic: str, avoid: list[str] | None = None, usage_cb=None) -> dict:
    """Generate a new exam-style task via the Claude API.

    avoid=None is the free fallback (no history); a list (even empty) is the PRO mode where the model
    is told which tasks the user has already seen. usage_cb(input_tokens, output_tokens) is told what every attempt really cost.
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
            **THINKING_ARGS,
        )
        raw_text = "".join(b.text for b in response.content if b.type == "text")
        if usage_cb:
            u = response.usage
            usage_cb((getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0),
                     getattr(u, "output_tokens", 0) or 0)

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
