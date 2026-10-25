"""Exam-style task formats, shared by the AI generator and the local pools.

Two task types reach the browser, both fully described by data so the page can draw any of them:

  type "balance_sheet": a two-sided table (Assets | Equity and liabilities) built from
      sides -> groups -> rows (+ group subtotals and a side total), with one or two columns.
      Every cell is either shown (given), typed by the student (input), or a hidden "XXX" placeholder
      that is neither shown nor asked (like the group totals in the real midterm "compile" task).
      Subtotals and totals are always computed here, never trusted from the model, and both sides must
      balance in every column. A "compile" task also carries an items table: descriptive ledger items
      with amounts that the student classifies into the table rows; their sums are verified here.

  type "test": a list of questions of kind mcq (one answer), multi (select all that apply),
      tf (true/false) or numeric, each worth 1-3 points. A multiple-choice question may carry a "calc"
      expression; it is evaluated here and must match the number in the correct option.

Everything the model returns goes through normalize_*(): it either comes out consistent or raises FormatError,
so the student is never graded against a broken answer key.
"""
import ast
import copy
import math
import operator
import random
import re

MAX_COLUMNS = 3
MAX_ROWS = 30
MIN_BLANK_CELLS = 6      # a balance sheet must ask for at least this many values (exam tables are not tiny)
MAX_BLANK_CELLS = 24
COMPILE_MIN_ROWS = 6     # "compile" tasks: at least this many caption rows to fill in
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


def _spec(value, name: str) -> dict:
    """A subtotal/total may be given as a plain label or as {"label", "mode": input|given|hidden, "blank"}."""
    spec = value if isinstance(value, dict) else {"label": value}
    mode = spec.get("mode")
    if mode not in (None, "input", "given", "hidden"):
        raise FormatError(f"{name}: mode must be input, given or hidden")
    return spec


def _spec_flags(spec: dict, ncols: int, label: str) -> tuple[list[bool], bool]:
    """-> (blank flag per column, hidden)."""
    mode = spec.get("mode")
    if mode == "hidden":
        return [False] * ncols, True
    if mode == "input":
        return [True] * ncols, False
    if mode == "given":
        return [False] * ncols, False
    return _flags(spec.get("blank"), ncols, label), False


# ---------------------------------------------------------------- balance sheet
def _resolve_row_plug(raw: dict, ncols: int) -> None:
    """A row with "plug": true has no values: they are calculated so that the sheet balances in every column.

    The model chooses plausible numbers for everything else and does not have to do the balancing arithmetic.
    """
    sides = raw.get("sides")
    if not (isinstance(sides, list) and len(sides) == 2):
        return
    plugs, sums = [], [[0] * ncols, [0] * ncols]
    for si, side in enumerate(sides):
        for group in (side.get("groups") if isinstance(side, dict) else None) or []:
            for row in (group.get("rows") if isinstance(group, dict) else None) or []:
                if not isinstance(row, dict):
                    raise FormatError("row must be an object")
                if row.get("plug"):
                    plugs.append((si, row))
                    continue
                values = row.get("values")
                if not isinstance(values, list) or len(values) != ncols:
                    raise FormatError(f"row '{row.get('label')}': {ncols} value(s) expected")
                for c in range(ncols):
                    sums[si][c] += _number(values[c], f"row '{row.get('label')}'")
    if not plugs:
        return
    if len(plugs) != 1:
        raise FormatError("only one plug row is allowed")
    si, row = plugs[0]
    values = [sums[1 - si][c] - sums[si][c] for c in range(ncols)]
    if any(v < 0 for v in values):
        raise FormatError("the plug row would be negative; the other side is too small")
    row["values"] = values


def _derive_compile(raw: dict) -> None:
    """'compile': the row values are the sums of the items mapped to them. One item may be "plug": true (no amount);
    its amount is calculated so that assets equal equity plus liabilities."""
    accounts, sides = raw.get("accounts"), raw.get("sides")
    if not isinstance(accounts, list) or not accounts:
        raise FormatError("compile variant needs an 'accounts' list")
    if not (isinstance(sides, list) and len(sides) == 2):
        raise FormatError("exactly two sides expected (assets, and equity & liabilities)")
    rows = {}
    for si, side in enumerate(sides):
        for group in (side.get("groups") if isinstance(side, dict) else None) or []:
            for row in (group.get("rows") if isinstance(group, dict) else None) or []:
                if not isinstance(row, dict):
                    raise FormatError("row must be an object")
                rows[str(row.get("label", "")).strip().lower()] = (si, row)
    sums, side_sums, plug = {}, [0, 0], None
    for a in accounts:
        if not isinstance(a, dict):
            raise FormatError("account must be an object")
        target = str(a.get("row", "")).strip().lower()
        if target not in rows:
            raise FormatError(f"account '{a.get('name')}' is mapped to an unknown row")
        if a.get("plug"):
            if plug is not None:
                raise FormatError("only one plug item is allowed")
            plug = a
            continue
        amount = _number(a.get("amount"), f"account '{a.get('name')}'")
        sums[target] = sums.get(target, 0) + amount
        side_sums[rows[target][0]] += amount
    if plug is not None:
        target = str(plug.get("row", "")).strip().lower()
        si = rows[target][0]
        amount = side_sums[1 - si] - side_sums[si]
        if amount < 0:
            raise FormatError("the plug item would be negative; the other side is too small")
        plug["amount"] = amount
        sums[target] = sums.get(target, 0) + amount
    for label, (_, row) in rows.items():
        row["values"] = [sums.get(label, 0)]
        row["blank"] = True


def normalize_balance_sheet(raw: dict, variant: str | None = None, min_blank: int = MIN_BLANK_CELLS) -> dict:
    """Validates a raw balance sheet (see generator.py for the shape) and returns the render-ready task."""
    if not isinstance(raw, dict):
        raise FormatError("balance sheet must be an object")

    columns = raw.get("columns") or ["Amount"]
    if not isinstance(columns, list) or not (1 <= len(columns) <= MAX_COLUMNS):
        raise FormatError(f"columns: 1-{MAX_COLUMNS} headings expected")
    columns = [_text(c, "column heading", 28) for c in columns]
    ncols = len(columns)
    raw = copy.deepcopy(raw)
    if variant == "compile":
        if ncols != 1:
            raise FormatError("compile sheets have one column")
        _derive_compile(raw)
    else:
        _resolve_row_plug(raw, ncols)

    sides_raw = raw.get("sides")
    if not isinstance(sides_raw, list) or len(sides_raw) != 2:
        raise FormatError("exactly two sides expected (assets, and equity & liabilities)")

    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"c{counter}"

    def make_cells(values: list, blank: list[bool], hidden: bool = False) -> list[dict]:
        return [{"id": next_id(), "value": values[i], "blank": blank[i], "hidden": hidden} for i in range(ncols)]

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
                sub = _spec(g["subtotal"], "subtotal")
                label = _text(sub.get("label"), "subtotal label", 80)
                blank, hidden = _spec_flags(sub, ncols, label)
                subtotal = {"label": label, "cells": make_cells(group_sum, blank, hidden)}
            groups.append({"title": _text(g.get("title"), "group title", 80, required=False), "rows": rows, "subtotal": subtotal})
            side_total = [a + b for a, b in zip(side_total, group_sum)]
        total_spec = _spec(s.get("total"), "total")
        total_label = _text(total_spec.get("label"), "total label", 80)
        blank, hidden = _spec_flags(total_spec, ncols, total_label)
        sides.append({
            "title": _text(s.get("title"), "side title", 60),
            "groups": groups,
            "total": {"label": total_label, "cells": make_cells(side_total, blank, hidden)},
        })
        side_sums.append(side_total)

    if row_count > MAX_ROWS:
        raise FormatError("too many rows")
    for c in range(ncols):
        if abs(side_sums[0][c] - side_sums[1][c]) > TOLERANCE:
            raise FormatError(f"the sheet does not balance in column {c + 1}: {side_sums[0][c]} vs {side_sums[1][c]}")

    blanks = sum(1 for cell in _all_cells(sides) if cell["blank"])
    if blanks < min_blank:
        raise FormatError(f"only {blanks} input cells; at least {min_blank} are required")
    if blanks > MAX_BLANK_CELLS:
        raise FormatError("too many blank cells")

    items = _items(raw, sides, ncols, variant)
    if variant == "compile":
        line_rows = [r for s in sides for g in s["groups"] for r in g["rows"]]
        if len(line_rows) < COMPILE_MIN_ROWS or any(not r["cells"][0]["blank"] for r in line_rows):
            raise FormatError(f"compile: every one of at least {COMPILE_MIN_ROWS} caption rows must be an input")

    return {
        "type": "balance_sheet",
        "variant": variant or "",
        "task_title": _text(raw.get("task_title"), "task_title", 120),
        "company_name": _text(raw.get("company_name"), "company_name", 120),
        "unit": _text(raw.get("unit") or "USD", "unit", 30),
        "description": _text(raw.get("description"), "description", 1500),
        "hint": _text(raw.get("hint"), "hint", 400, required=False),
        "explanation": _text(raw.get("explanation"), "explanation", 900, required=False),
        "layout": "two_sided" if ncols <= 2 else "report",     # assets on the left, equity and liabilities on the right
        "columns": columns,
        "sides": sides,
        "items": items,
    }


def _all_cells(sides: list[dict]):
    for side in sides:
        for group in side["groups"]:
            for row in group["rows"]:
                yield from row["cells"]
            if group["subtotal"]:
                yield from group["subtotal"]["cells"]
        yield from side["total"]["cells"]


def _items(raw: dict, sides: list[dict], ncols: int, variant: str | None) -> list[dict]:
    """The items table of a 'compile' task: descriptive ledger items the student sorts into the rows.

    The mapping 'row' (which caption an item belongs to) stays on the server; the sums per row are verified here.
    """
    accounts = raw.get("accounts")
    if not accounts:
        if variant == "compile":
            raise FormatError("compile variant needs an 'accounts' list")
        return []
    if ncols != 1:
        raise FormatError("accounts are only supported for single-column sheets")
    if not isinstance(accounts, list) or not (6 <= len(accounts) <= 30):
        raise FormatError("accounts: 6-30 entries expected")

    rows = {}
    for side in sides:
        for group in side["groups"]:
            for row in group["rows"]:
                rows[row["label"].lower()] = row
    sums, items = {}, []
    for a in accounts:
        if not isinstance(a, dict):
            raise FormatError("account must be an object")
        name = _text(a.get("name"), "account name", 110)
        amount = _number(a.get("amount"), f"account '{name}'")
        target = _text(a.get("row"), "account row", 80).lower()
        if target not in rows:
            raise FormatError(f"account '{name}' is mapped to an unknown row")
        sums[target] = sums.get(target, 0) + amount
        items.append({"name": name, "amount": amount})
    for label, row in rows.items():
        cell = row["cells"][0]
        if cell["blank"] and abs(sums.get(label, 0) - cell["value"]) > TOLERANCE:
            raise FormatError(f"accounts mapped to '{row['label']}' add up to {sums.get(label, 0)}, not {cell['value']}")
    random.shuffle(items)
    return items


# ---------------------------------------------------------------- tests
QUESTION_KINDS = ("mcq", "multi", "tf", "numeric")
_POSITIONAL = re.compile(r"\b(all|none|both|neither)\b[^.]{0,20}\b(above|these|of the)\b|\b[a-d]\s+and\s+[a-d]\b", re.I)

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def safe_eval(expression: str) -> float:
    """Evaluates +, -, *, / and parentheses on plain numbers; nothing else is allowed."""
    if not isinstance(expression, str) or not (1 <= len(expression) <= 120):
        raise FormatError("calc: expression expected")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = walk(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        raise FormatError("calc: only numbers and + - * / ( ) are allowed")

    try:
        return walk(ast.parse(expression.replace(",", "").strip(), mode="eval"))
    except (SyntaxError, ZeroDivisionError, RecursionError):
        raise FormatError("calc: cannot evaluate the expression")


_NUMBER_IN_TEXT = re.compile(r"-?\d[\d.,\s]*\d|-?\d")


def numbers_in(text: str) -> list[float]:
    """Amounts written in an option: 13000, 13,000, 1 100, 60.000 (thousands dot), 1,010.5, $7,000 ..."""
    found = []
    for m in _NUMBER_IN_TEXT.findall(text):
        t = m.strip().replace(" ", "")
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
            t = t.replace(".", "")
        t = t.replace(",", "")
        try:
            found.append(float(t))
        except ValueError:
            pass
    return found


def _verify_calc(q: dict, options: list[str], correct: int) -> None:
    value = safe_eval(q["calc"])
    if not any(abs(n - value) <= 0.01 for n in numbers_in(options[correct])):
        raise FormatError(f"calc {q['calc']!r} = {value:g}, but the correct option is {options[correct]!r}")


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
    points = q.get("points", 1)
    if isinstance(points, bool) or not isinstance(points, int) or not (1 <= points <= 3):
        raise FormatError(f"question {number}: points must be 1, 2 or 3")
    out = {
        "id": f"q{number}",
        "kind": kind,
        "points": points,
        "prompt": _text(q.get("prompt"), "prompt", 500),
        "explanation": _text(q.get("explanation"), "explanation", 500, required=False),
    }
    if kind == "mcq":
        options = _options(q, 3, 5)
        correct = _indices(q.get("correct"), len(options), "mcq")
        if len(correct) != 1:
            raise FormatError("mcq needs exactly one correct option")
        if q.get("calc"):
            _verify_calc(q, options, correct[0])      # calculation questions are checked by arithmetic, before shuffling
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


def mcq_from_options(prompt: str, options: list[str], correct_text: str, explanation: str = "", points: int = 1) -> dict:
    """Builds a raw mcq question from the pools' 'options + correct_answer' items."""
    if correct_text not in options:
        raise FormatError("correct answer is not among the options")
    return {"kind": "mcq", "prompt": prompt, "options": list(options), "correct": options.index(correct_text),
            "explanation": explanation, "points": points}
