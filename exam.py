"""Exam-style task formats, shared by the AI generator and the local pools.

Two task types reach the browser, both fully described by data so the page can draw any of them:

  type "balance_sheet": a table built from sides -> groups -> rows (+ subtotals/totals), with one or
      more columns. Any cell can be given (shown) or blank (the student types it). Subtotals and totals
      are always computed here, never trusted from the model, and both sides must balance in every column.

  type "test": a list of questions of kind mcq (one answer), multi (select all that apply),
      tf (true/false) or numeric.

Everything the model returns goes through normalize_*(): it either comes out consistent or raises FormatError,
so the student is never graded against a broken answer key.
"""
import math
import random
import re

MAX_COLUMNS = 3
MAX_ROWS = 30
MAX_BLANK_CELLS = 24
TOLERANCE = 0.5          # amounts must balance within half a unit


class FormatError(ValueError):
    pass


# ---------------------------------------------------------------- helpers
def _text(value, name: str, max_len: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FormatError(f"{name} must be text")
    value = " ".join(value.split()) if max_len <= 400 else value.strip()
    if required and not value:
        raise FormatError(f"{name} is empty")
    return value[:max_len]


def _number(value, name: str):
    if isinstance(value, bool):
        raise FormatError(f"{name} must be a number")
    if isinstance(value, str):
        value = value.replace(",", "").replace(" ", "").replace("$", "")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise FormatError(f"{name} must be a number")
    if not math.isfinite(v) or abs(v) > 1e12:
        raise FormatError(f"{name} is out of range")
    return int(round(v)) if abs(v - round(v)) < 1e-6 else round(v, 2)


def _flags(value, ncols: int, name: str) -> list[bool]:
    """blank: true/false for every column, or a list with one flag per column."""
    if value in (None, False):
        return [False] * ncols
    if value is True:
        return [True] * ncols
    if isinstance(value, list) and len(value) == ncols and all(isinstance(x, bool) for x in value):
        return list(value)
    raise FormatError(f"{name}: 'blank' must be true/false or one flag per column")


# ---------------------------------------------------------------- balance sheet
def normalize_balance_sheet(raw: dict, variant: str | None = None) -> dict:
    """Validates a raw balance sheet (see generator.py for the shape) and returns the render-ready task."""
    if not isinstance(raw, dict):
        raise FormatError("balance sheet must be an object")

    columns = raw.get("columns") or ["Amount"]
    if not isinstance(columns, list) or not (1 <= len(columns) <= MAX_COLUMNS):
        raise FormatError(f"columns: 1-{MAX_COLUMNS} headings expected")
    columns = [_text(c, "column heading", 28) for c in columns]
    ncols = len(columns)

    sides_raw = raw.get("sides")
    if not isinstance(sides_raw, list) or len(sides_raw) != 2:
        raise FormatError("exactly two sides expected (assets, and equity & liabilities)")

    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"c{counter}"

    def make_cells(values: list, blank: list[bool]) -> list[dict]:
        return [{"id": next_id(), "value": values[i], "blank": blank[i]} for i in range(ncols)]

    sides, side_sums, row_count = [], [], 0
    for s in sides_raw:
        if not isinstance(s, dict) or not isinstance(s.get("groups"), list) or not s["groups"]:
            raise FormatError("each side needs at least one group")
        side_total = [0] * ncols
        groups = []
        for g in s["groups"]:
            if not isinstance(g, dict) or not isinstance(g.get("rows"), list) or not g["rows"]:
                raise FormatError("each group needs at least one row")
            group_sum, rows = [0] * ncols, []
            for r in g["rows"]:
                if not isinstance(r, dict):
                    raise FormatError("row must be an object")
                label = _text(r.get("label"), "row label", 80)
                values = r.get("values")
                if not isinstance(values, list) or len(values) != ncols:
                    raise FormatError(f"row '{label}': {ncols} value(s) expected")
                values = [_number(v, f"row '{label}'") for v in values]
                rows.append({"label": label, "cells": make_cells(values, _flags(r.get("blank"), ncols, label))})
                group_sum = [a + b for a, b in zip(group_sum, values)]
                row_count += 1
            subtotal = None
            if g.get("subtotal"):
                sub = g["subtotal"] if isinstance(g["subtotal"], dict) else {"label": g["subtotal"]}
                label = _text(sub.get("label"), "subtotal label", 80)
                subtotal = {"label": label, "cells": make_cells(group_sum, _flags(sub.get("blank"), ncols, label))}
            groups.append({"title": _text(g.get("title"), "group title", 80, required=False), "rows": rows, "subtotal": subtotal})
            side_total = [a + b for a, b in zip(side_total, group_sum)]
        total_raw = s.get("total") if isinstance(s.get("total"), dict) else {"label": s.get("total")}
        total_label = _text(total_raw.get("label"), "total label", 80)
        sides.append({
            "title": _text(s.get("title"), "side title", 60),
            "groups": groups,
            "total": {"label": total_label, "cells": make_cells(side_total, _flags(total_raw.get("blank"), ncols, total_label))},
        })
        side_sums.append(side_total)

    if row_count > MAX_ROWS:
        raise FormatError("too many rows")
    for c in range(ncols):
        if abs(side_sums[0][c] - side_sums[1][c]) > TOLERANCE:
            raise FormatError(f"the sheet does not balance in column {c + 1}: {side_sums[0][c]} vs {side_sums[1][c]}")

    blanks = sum(1 for cell in _all_cells(sides) if cell["blank"])
    if blanks < 1:
        raise FormatError("at least one blank cell is required")
    if blanks > MAX_BLANK_CELLS:
        raise FormatError("too many blank cells")

    description = _text(raw.get("description"), "description", 1500)
    accounts_block = _accounts_block(raw, sides, ncols, variant)
    layout = raw.get("layout") if raw.get("layout") in ("two_sided", "report") else "two_sided"
    if ncols > 1:
        layout = "report"   # several columns need the full width

    return {
        "type": "balance_sheet",
        "variant": variant or "",
        "task_title": _text(raw.get("task_title"), "task_title", 120),
        "company_name": _text(raw.get("company_name"), "company_name", 120),
        "unit": _text(raw.get("unit") or "USD", "unit", 30),
        "description": description + accounts_block,
        "hint": _text(raw.get("hint"), "hint", 400, required=False),
        "explanation": _text(raw.get("explanation"), "explanation", 900, required=False),
        "layout": layout,
        "columns": columns,
        "sides": sides,
    }


def _all_cells(sides: list[dict]):
    for side in sides:
        for group in side["groups"]:
            for row in group["rows"]:
                yield from row["cells"]
            if group["subtotal"]:
                yield from group["subtotal"]["cells"]
        yield from side["total"]["cells"]


def _accounts_block(raw: dict, sides: list[dict], ncols: int, variant: str | None) -> str:
    """'compile' tasks list ledger accounts; the student maps them to table rows. The sums are verified here."""
    accounts = raw.get("accounts")
    if variant != "compile" and not accounts:
        return ""
    if variant == "compile" and not accounts:
        raise FormatError("compile variant needs an 'accounts' list")
    if ncols != 1:
        raise FormatError("accounts are only supported for single-column sheets")
    if not isinstance(accounts, list) or not (6 <= len(accounts) <= 30):
        raise FormatError("accounts: 6-30 entries expected")

    rows = {}
    for side in sides:
        for group in side["groups"]:
            for row in group["rows"]:
                rows[row["label"].lower()] = row
    sums = {}
    lines = []
    for a in accounts:
        if not isinstance(a, dict):
            raise FormatError("account must be an object")
        name = _text(a.get("name"), "account name", 80)
        amount = _number(a.get("amount"), f"account '{name}'")
        target = _text(a.get("row"), "account row", 80).lower()
        if target not in rows:
            raise FormatError(f"account '{name}' is mapped to an unknown row")
        sums[target] = sums.get(target, 0) + amount
        lines.append((name, amount))
    for label, row in rows.items():
        cell = row["cells"][0]
        if cell["blank"] and abs(sums.get(label, 0) - cell["value"]) > TOLERANCE:
            raise FormatError(f"accounts mapped to '{row['label']}' add up to {sums.get(label, 0)}, not {cell['value']}")
    random.shuffle(lines)
    listing = "\n".join(f"{name}: {amount:,}" for name, amount in lines)
    return f"\n\nLedger balances:\n{listing}"


# ---------------------------------------------------------------- tests
QUESTION_KINDS = ("mcq", "multi", "tf", "numeric")
_POSITIONAL = re.compile(r"\b(all|none|both|neither)\b[^.]{0,20}\b(above|these|of the)\b|\b[a-d]\s+and\s+[a-d]\b", re.I)


def _options(q: dict, lo: int, hi: int) -> list[str]:
    options = q.get("options")
    if not isinstance(options, list) or not (lo <= len(options) <= hi):
        raise FormatError(f"{q.get('kind')}: {lo}-{hi} options expected")
    options = [_text(o, "option", 220) for o in options]
    if len({o.lower() for o in options}) != len(options):
        raise FormatError("duplicate options")
    return options


def _indices(value, n: int, name: str) -> list[int]:
    items = value if isinstance(value, list) else [value]
    out = []
    for i in items:
        if isinstance(i, bool) or not isinstance(i, int) or not (0 <= i < n):
            raise FormatError(f"{name}: invalid option index")
        out.append(i)
    if len(set(out)) != len(out):
        raise FormatError(f"{name}: repeated option index")
    return out


def _shuffle(options: list[str], correct: list[int]) -> tuple[list[str], list[int]]:
    """Randomises the option order (so the right answer is not always first) unless options refer to each other."""
    if any(_POSITIONAL.search(o) for o in options):
        return options, correct
    order = list(range(len(options)))
    random.shuffle(order)
    return [options[i] for i in order], sorted(order.index(i) for i in correct)


def normalize_question(q: dict, number: int) -> dict:
    if not isinstance(q, dict) or q.get("kind") not in QUESTION_KINDS:
        raise FormatError(f"question {number}: unknown kind")
    kind = q["kind"]
    out = {
        "id": f"q{number}",
        "kind": kind,
        "prompt": _text(q.get("prompt"), "prompt", 500),
        "explanation": _text(q.get("explanation"), "explanation", 500, required=False),
    }
    if kind == "mcq":
        options = _options(q, 3, 5)
        correct = _indices(q.get("correct"), len(options), "mcq")
        if len(correct) != 1:
            raise FormatError("mcq needs exactly one correct option")
        options, correct = _shuffle(options, correct)
        out.update(options=options, correct=correct[0])
    elif kind == "multi":
        options = _options(q, 4, 6)
        correct = _indices(q.get("correct"), len(options), "multi")
        if not (2 <= len(correct) < len(options)):
            raise FormatError("multi needs 2+ correct options, but not all of them")
        options, correct = _shuffle(options, correct)
        out.update(options=options, correct=correct)
    elif kind == "tf":
        c = q.get("correct")
        if isinstance(c, str) and c.lower() in ("true", "false"):
            c = c.lower() == "true"
        if not isinstance(c, bool):
            raise FormatError("tf needs true or false")
        out.update(correct=c)
    else:
        tol = q.get("tolerance", 0)
        out.update(
            correct=_number(q.get("correct"), "numeric answer"),
            tolerance=max(0, _number(tol, "tolerance")) if tol is not None else 0,
            unit=_text(q.get("unit"), "unit", 20, required=False),
        )
    return out


def normalize_test(raw: dict, min_questions: int = 3) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("questions"), list):
        raise FormatError("a test needs a 'questions' list")
    if not (min_questions <= len(raw["questions"]) <= 12):
        raise FormatError(f"{min_questions}-12 questions expected")
    questions = [normalize_question(q, i + 1) for i, q in enumerate(raw["questions"])]
    return {
        "type": "test",
        "task_title": _text(raw.get("task_title"), "task_title", 120),
        "company_name": _text(raw.get("company_name"), "label", 80, required=False),
        "description": _text(raw.get("description"), "description", 600, required=False),
        "hint": "",
        "questions": questions,
    }


def mcq_from_options(prompt: str, options: list[str], correct_text: str, explanation: str = "") -> dict:
    """Builds a raw mcq question from the pools' 'options + correct_answer' items."""
    if correct_text not in options:
        raise FormatError("correct answer is not among the options")
    return {"kind": "mcq", "prompt": prompt, "options": list(options), "correct": options.index(correct_text), "explanation": explanation}
