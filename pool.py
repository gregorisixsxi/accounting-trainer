"""Free mode: tasks from the local English pools (tasks_pool.json, terminology_pool.json), no Claude API calls.

Files are read once at startup. Every task is validated; invalid ones are skipped
(with a log message) so one bad entry cannot break the site. Pools are English/USD only.
"""
import json
import random
from pathlib import Path

import exam

BASE_DIR = Path(__file__).resolve().parent
TASKS_PATH = BASE_DIR / "tasks_pool.json"              # balance_sheet
TERMINOLOGY_PATH = BASE_DIR / "terminology_pool.json"  # terminology
CURRENCY = "thousand USD"          # amounts are shown in thousands, like the real exam (350, 120, 520 ...), not as 350,000
AMOUNT_DIVISOR = 1000              # the pool file stores whole dollars; they are scaled once when the pool is loaded

# Items that count as fixed (non-current) assets in the "totals" variant. Everything else under assets is current.
FIXED_ASSETS = {
    "Buildings", "Facility Building", "Cleaning Equipment", "Computers & Servers", "Construction Tools",
    "Delivery Trucks", "Delivery Vans", "Display Shelves", "Domain & Website", "Exercise Equipment",
    "Furniture", "Kitchen Equipment", "Machinery", "Office Hardware", "Ovens & Mixers", "Server Hardware",
    "Sewing Machines", "Software Licenses", "Store Equipment", "Store Fixtures", "Cash Register",
}

# ---------------------------------------------------------------- balance sheet: caption mapping (midterm layout)
# Every pool item belongs to exactly one caption of the balance sheet; the "compile" variant asks the student to do that sorting.
GROUPS = [  # (side, group title, [(caption key, row label)], subtotal label)
    ("assets", "A. Non-current assets", [("intangible", "I. Intangible assets"), ("tangible", "II. Tangible assets")], "Total non-current assets"),
    ("assets", "B. Current assets", [("inventories", "I. Inventories"), ("receivables", "II. Accounts receivable"), ("liquid", "IV. Liquid assets")], "Total current assets"),
    ("el", "D. Stockholders' equity", [("contributed", "I. Contributed capital"), ("retained", "IV. Retained earnings")], "Total stockholders' equity"),
    ("el", "F. Liabilities", [("longterm", "II. Long-term liabilities"), ("current_liab", "III. Current liabilities")], "Total liabilities"),
    ("el", "", [("deferred", "G. Accrued expenses and deferred income")], None),
]

ITEM_CAPTION: dict[str, str] = {}
for _key, _names in {
    "intangible": "Domain & Website|Software Licenses",
    "tangible": ("Buildings|Facility Building|Cleaning Equipment|Computers & Servers|Construction Tools|Delivery Trucks|Delivery Vans|"
                 "Display Shelves|Exercise Equipment|Furniture|Kitchen Equipment|Machinery|Office Hardware|Ovens & Mixers|"
                 "Server Hardware|Sewing Machines|Store Equipment|Store Fixtures|Cash Register"),
    "inventories": ("Auto Spare Parts|Building Materials|Fabric & Accessories|Finished Garments|Finished Goods|Finished Pastries|"
                    "Flour & Ingredients|Food Inventory|Fuel Inventory|Inventory|Medicine Stock|Raw Materials|Supplements Inventory|"
                    "Supplies|Warehouse Stock|Work in Progress"),
    "receivables": "Accounts Receivable|Client Invoices Due|Client Receivables",
    "liquid": "Bank|Bank Account|Cash|Cash and Equivalents|Checking|Checking Account|Merchant Account|Operating Account|Operating Cash|Petty Cash",
    "contributed": "Common Stock|Capital Stock|Initial Capital|Owner Capital|Owner's Capital|Owner's Equity|Preferred Stock",
    "retained": "Retained Earnings",
    "longterm": "Bank Loan|Equipment Loan|Lease Obligations|Long-term Bank Loan|Long-term Mortgage|Notes Payable",
    "current_liab": ("Accounts Payable|Customer Advances|Salaries Payable|Short-term Loan|Short-term Notes Payable|Supplier Credit|"
                     "Supplier Debt|Supplier Payables|Tax Payable|Taxes Payable|Trade Creditors|Wages Payable"),
    "deferred": "Accrued Expenses|Deferred Subscription Revenue|Unearned Memberships|Unearned Retainer|Unearned Revenue",
}.items():
    ITEM_CAPTION.update({n.strip(): _key for n in _names.split("|")})

# Names that would be ambiguous on a sheet are shown with a clarification, so the classification has one right answer.
DISPLAY_NAME = {
    "Bank Loan": "Bank loan (repayable in 5 years)",
    "Equipment Loan": "Equipment loan (repayable in 4 years)",
    "Lease Obligations": "Lease obligations (repayable in 3 years)",
    "Notes Payable": "Notes payable (due in 3 years)",
    "Customer Advances": "Advances received from customers",
    "Cash Register": "Cash register (equipment)",
    "Supplies": "Supplies in stock",
    "Operating Cash": "Cash in the till",
    "Checking": "Checking account balance",
}


def _valid_balance_sheet(t: dict) -> bool:
    try:
        a, l, e = (sum(t[k].values()) for k in ("assets", "liabilities", "equity"))
        return bool(t["assets"]) and a == l + e
    except Exception:
        return False


def _valid_terminology(t: dict) -> bool:
    try:
        o = t["options"]
        return t["correct_answer"] in o and len(set(o)) == len(o) >= 2 and bool(t["question"])
    except Exception:
        return False


def _scale_amounts(t: dict) -> dict:
    """Dollars -> thousands of dollars for every line of a balance sheet task. The sheet still balances because every
    amount in the pool is a multiple of 1,000; the check afterwards skips the task if that ever stops being true."""
    out = dict(t)
    for key in ("assets", "liabilities", "equity"):
        if isinstance(t.get(key), dict):
            out[key] = {name: round(v / AMOUNT_DIVISOR) for name, v in t[key].items()}
    return out


def _load() -> dict[str, list[dict]]:
    sources = (
        (TASKS_PATH, "balance_sheet", _valid_balance_sheet),
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
            if topic == "balance_sheet" and isinstance(t, dict):
                t = _scale_amounts(t)
            if isinstance(t, dict) and t.get("id") not in seen and "id" in t and check(t):
                good.append(t)
                seen.add(t["id"])
            else:
                print(f"[pool] skipped invalid {topic} task: {t.get('id') if isinstance(t, dict) else t!r}")
        pool[topic] = good
    return pool


POOL = _load()
POOL_TOPICS = {t for t, items in POOL.items() if items}

QUESTIONS_PER_TEST = {"terminology": 8}


def _pick_many(topic: str, exclude: set[str], n: int) -> list[dict]:
    """n distinct items, preferring ones the student has not just seen."""
    items = POOL[topic]
    fresh = [t for t in items if t["id"] not in exclude]
    stale = [t for t in items if t["id"] in exclude]
    random.shuffle(fresh)
    random.shuffle(stale)
    return (fresh + stale)[: min(n, len(items))]


# ---------------------------------------------------------------- balance sheets
BS_VARIANTS = (("compile", 6), ("totals", 4))   # (variant, weight)


def _compile_raw(t: dict) -> tuple[dict, str, str, str] | None:
    """Midterm 'compile' task: sort the items into the captions. Returns None if an item cannot be classified."""
    by_key: dict[str, list[tuple[str, int]]] = {}
    for section in ("assets", "liabilities", "equity"):
        for name, amount in t[section].items():
            key = ITEM_CAPTION.get(name)
            if key is None:
                print(f"[pool] no caption for {name!r} ({t['id']})")
                return None
            by_key.setdefault(key, []).append((DISPLAY_NAME.get(name, name), amount))

    sides = {"assets": [], "el": []}
    accounts, parts = [], []
    for side, title, captions, subtotal in GROUPS:
        rows = []
        for key, label in captions:
            if key in by_key:
                total = sum(a for _, a in by_key[key])
                rows.append({"label": label, "values": [total], "blank": True})
                accounts += [{"name": d, "amount": a, "row": label} for d, a in by_key[key]]
                parts.append(f"{label}: " + " + ".join(f"{d} {a:,}" for d, a in by_key[key]) + f" = {total:,}.")
        if rows:
            group = {"title": title, "rows": rows}
            if subtotal:
                group["subtotal"] = {"label": subtotal, "mode": "hidden"}
            sides[side].append(group)
    raw = {
        "sides": [
            {"title": "Assets", "groups": sides["assets"], "total": {"label": "TOTAL ASSETS", "mode": "hidden"}},
            {"title": "Equity and liabilities", "groups": sides["el"], "total": {"label": "TOTAL EQUITY AND LIABILITIES", "mode": "hidden"}},
        ],
        "accounts": accounts,
    }
    description = (f"Based on the data provided, compile the balance sheet of {t['title']} (amounts in {CURRENCY}). "
                   "You do not have to summarize the groups, but each line of the balance sheet can include several items.")
    hint = ("Decide for every item whether it is a non-current asset, a current asset, equity or a liability (long-term or current), "
            "then add up the items that belong to the same line. Assets = Equity + Liabilities.")
    return raw, description, hint, " ".join(parts)


def _totals_raw(t: dict) -> tuple[dict, str, str, str]:
    """All lines are shown; the student calculates every subtotal and both totals."""
    fixed = [(k, v) for k, v in t["assets"].items() if k in FIXED_ASSETS]
    current = [(k, v) for k, v in t["assets"].items() if k not in FIXED_ASSETS]

    def group(title, items, sub_label):
        return {"title": title, "rows": [{"label": DISPLAY_NAME.get(k, k), "values": [v], "blank": False} for k, v in items],
                "subtotal": {"label": sub_label, "mode": "input"}}

    asset_groups = []
    if fixed:
        asset_groups.append(group("A. Fixed assets", fixed, "Total fixed assets"))
    if current:
        asset_groups.append(group("B. Current assets", current, "Total current assets"))
    raw = {"sides": [
        {"title": "Assets", "groups": asset_groups, "total": {"label": "Total assets", "mode": "input"}},
        {"title": "Equity and liabilities",
         "groups": [group("C. Equity", list(t["equity"].items()), "Total equity"),
                    group("D. Liabilities", list(t["liabilities"].items()), "Total liabilities")],
         "total": {"label": "Total equity and liabilities", "mode": "input"}}]}
    total_assets = sum(t["assets"].values())
    total_el = sum(t["liabilities"].values()) + sum(t["equity"].values())
    description = f"{t['title']} reports the line items below (amounts in {CURRENCY}). Calculate every subtotal and both totals."
    hint = "Add the lines of each group, then add the groups. Assets must equal equity plus liabilities."
    explanation = (f"Total assets {total_assets:,} = Total equity and liabilities {total_el:,}. "
                   "Each subtotal is the sum of the lines above it.")
    return raw, description, hint, explanation


def _finish(t: dict, variant: str, built: tuple[dict, str, str, str]) -> dict:
    raw, description, hint, explanation = built
    raw.update(
        task_title="Compile the Balance Sheet" if variant == "compile" else "Complete the Balance Sheet",
        company_name=t["title"], unit=CURRENCY, columns=["Amount"],
        description=description, hint=hint, explanation=explanation,
    )
    return exam.normalize_balance_sheet(raw, variant)


def _balance_sheet_task(t: dict) -> dict:
    variant = random.choices([v for v, _ in BS_VARIANTS], [w for _, w in BS_VARIANTS])[0]
    task = None
    if variant == "compile":
        built = _compile_raw(t)
        if built is not None:
            try:
                task = _finish(t, "compile", built)
            except exam.FormatError as e:      # e.g. too few captions for a "compile" task: use the totals variant
                print(f"[pool] {t['id']} compile rejected ({e}); using totals")
    if task is None:
        task = _finish(t, "totals", _totals_raw(t))
    task["id"] = t["id"]
    return task


# ---------------------------------------------------------------- multiple-choice tests
def _test_task(picks: list[dict]) -> dict:
    questions = [exam.mcq_from_options(t["question"], t["options"], t["correct_answer"], t.get("explanation", ""), t.get("points", 1))
                 for t in picks]
    task = exam.normalize_test({
        "task_title": "Terminology: Midterm-Style Test",
        "company_name": f"Multiple choice · {len(picks)} questions",
        "description": "Choose the best answer for each question.",
        "questions": questions,
    })
    task["ids"] = [t["id"] for t in picks]
    return task


def get_task(topic: str, exclude: set[str] | None = None) -> dict | None:
    """Returns a task in the frontend format, or None if the topic has no pool."""
    if topic not in POOL_TOPICS:
        return None
    exclude = exclude or set()
    if topic == "balance_sheet":
        return _balance_sheet_task(_pick_many(topic, exclude, 1)[0])
    return _test_task(_pick_many(topic, exclude, QUESTIONS_PER_TEST[topic]))
