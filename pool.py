"""Free mode: tasks from the local English pools (tasks_pool.json, terminology_pool.json), no Claude API calls.

Files are read once at startup. Every task is validated; invalid ones are skipped
(with a log message) so one bad entry cannot break the site. Pools are English/USD only.
"""
import json
import random
from pathlib import Path

import exam

BASE_DIR = Path(__file__).resolve().parent
TASKS_PATH = BASE_DIR / "tasks_pool.json"            # balance_sheet, transactions
TERMINOLOGY_PATH = BASE_DIR / "terminology_pool.json"  # terminology
CURRENCY = "USD"

# Items that count as fixed (non-current) assets. Everything else under assets is current.
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


QUESTIONS_PER_TEST = {"terminology": 6, "transactions": 4}


def _pick_many(topic: str, exclude: set[str], n: int) -> list[dict]:
    """n distinct items, preferring ones the student has not just seen."""
    items = POOL[topic]
    fresh = [t for t in items if t["id"] not in exclude]
    stale = [t for t in items if t["id"] in exclude]
    random.shuffle(fresh)
    random.shuffle(stale)
    return (fresh + stale)[: min(n, len(items))]


# ---------------------------------------------------------------- balance sheets
BS_VARIANTS = (("totals", 4), ("missing_item", 4), ("missing_two", 2))   # (variant, weight)


def _bs_raw(t: dict, variant: str) -> tuple[dict, str, str, str]:
    """Builds the raw sheet for a pool item. Returns (raw, description, hint, explanation)."""
    assets = list(t["assets"].items())
    fixed = [(k, v) for k, v in assets if k in FIXED_ASSETS]
    current = [(k, v) for k, v in assets if k not in FIXED_ASSETS]
    totals_blank = variant == "totals"

    def group(title, items, sub_label):
        return {"title": title, "rows": [{"label": k, "values": [v], "blank": False} for k, v in items],
                "subtotal": {"label": sub_label, "blank": totals_blank}}

    asset_groups = []
    if fixed:
        asset_groups.append(group("A. Fixed assets", fixed, "Total fixed assets"))
    if current:
        asset_groups.append(group("B. Current assets", current, "Total current assets"))
    eq_group = group("C. Equity", list(t["equity"].items()), "Total equity")
    liab_group = group("D. Liabilities", list(t["liabilities"].items()), "Total liabilities")
    sides = [
        {"title": "Assets", "groups": asset_groups, "total": {"label": "Total assets", "blank": totals_blank}},
        {"title": "Equity and liabilities", "groups": [eq_group, liab_group], "total": {"label": "Total equity and liabilities", "blank": totals_blank}},
    ]
    total_assets, total_el = sum(t["assets"].values()), sum(t["liabilities"].values()) + sum(t["equity"].values())
    name = t["title"]

    if variant == "totals":
        description = f"{name} reports the line items below (amounts in {CURRENCY}). Calculate every subtotal and both totals."
        hint = "Add the lines of each group, then add the groups. Assets must equal equity plus liabilities."
        explanation = (f"Total assets {total_assets:,} = Total equity and liabilities {total_el:,}. "
                       "Each subtotal is the sum of the lines above it.")
        return {"sides": sides}, description, hint, explanation

    # missing_item / missing_two: blank line items, subtotals and totals are shown
    asset_rows = [(g, r) for g in asset_groups for r in g["rows"]]
    el_rows = [(g, r) for g in (eq_group, liab_group) for r in g["rows"]]
    if variant == "missing_two":
        picks = [random.choice(asset_rows), random.choice(el_rows)]
    else:
        equity_rows = [(eq_group, r) for r in eq_group["rows"]]
        pool_rows = equity_rows if equity_rows and random.random() < 0.4 else asset_rows + el_rows
        picks = [random.choice(pool_rows)]
    parts = []
    for g, r in picks:
        r["blank"] = True
        value = r["values"][0]
        subtotal = sum(x["values"][0] for x in g["rows"])
        others = subtotal - value
        parts.append(f"{r['label']}: {g['subtotal']['label']} {subtotal:,} minus the other lines {others:,} = {value:,}.")
    n = len(picks)
    description = (f"The balance sheet of {name} is shown below (amounts in {CURRENCY}) with {n} line{'s' if n > 1 else ''} left blank. "
                   f"Use the accounting equation and the subtotals to find the missing amount{'s' if n > 1 else ''}.")
    hint = "Assets = Equity + Liabilities. A subtotal minus the other lines of its group gives the missing line."
    return {"sides": sides}, description, hint, " ".join(parts)


def _balance_sheet_task(t: dict) -> dict:
    variant = random.choices([v for v, _ in BS_VARIANTS], [w for _, w in BS_VARIANTS])[0]
    raw, description, hint, explanation = _bs_raw(t, variant)
    raw.update(
        task_title="Complete the Balance Sheet" if variant == "totals" else "Find the Missing Balance Sheet Item",
        company_name=t["title"], unit=CURRENCY, layout="two_sided", columns=["Amount"],
        description=description, hint=hint, explanation=explanation,
    )
    task = exam.normalize_balance_sheet(raw, variant)
    task["id"] = t["id"]
    return task


# ---------------------------------------------------------------- multiple-choice tests
def _test_task(topic: str, picks: list[dict]) -> dict:
    if topic == "terminology":
        questions = [exam.mcq_from_options(t["question"], t["options"], t["correct_answer"]) for t in picks]
        title, label = "Terminology: Midterm-Style Test", f"Multiple choice · {len(picks)} questions"
        description, hint = "Choose the best answer for each question.", ""
    else:
        questions = [exam.mcq_from_options(f"{t['title']}: {t['description']}", t["options"], t["correct_answer"]) for t in picks]
        title, label = "Journal Entries", f"Multiple choice · {len(picks)} transactions"
        description, hint = "Choose the correct journal entry for each transaction.", TR_HINT
    task = exam.normalize_test({"task_title": title, "company_name": label, "description": description, "questions": questions})
    task["hint"] = hint
    task["ids"] = [t["id"] for t in picks]
    return task


def get_task(topic: str, exclude: set[str] | None = None) -> dict | None:
    """Returns a task in the frontend format, or None if the topic has no pool."""
    if topic not in POOL_TOPICS:
        return None
    exclude = exclude or set()
    if topic == "balance_sheet":
        return _balance_sheet_task(_pick_many(topic, exclude, 1)[0])
    return _test_task(topic, _pick_many(topic, exclude, QUESTIONS_PER_TEST[topic]))
