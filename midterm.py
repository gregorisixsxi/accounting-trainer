"""Mid-term test: a whole practice paper made from the structure of the real midterms.

midterm1_tasks.json / midterm2_tasks.json (made by scripts/build_midterm_tasks.py from the exemplars in midterm_templates.py) hold, for every
question of the real paper (its "type"), several practice variants. build(n) picks one variant per type, in the real order, and seals every
question: the browser gets the question without answers plus a signed token that carries the answer key (the same idea as grading.seal).
The whole paper is marked in one go by grade_paper(), after the student hands it in; nothing is revealed before that.

Question kinds: mcq (a one-question test), balance_sheet (compile), income_statement, event_impact, journal, trial_balance, ledger_posting.
"""
import copy
import importlib.util
import json
import random
import secrets
import time
from pathlib import Path

import exam
import grading
import table_types

BASE_DIR = Path(__file__).resolve().parent
FILES = {1: BASE_DIR / "midterm1_tasks.json", 2: BASE_DIR / "midterm2_tasks.json"}
GENERATORS = BASE_DIR / "scripts" / "build_midterm_tasks.py"      # its functions make fresh tasks of every type (rng -> task body)
PAPER_TOKEN_TTL = 8 * 3600       # a paper may take a long time; grading.open_token itself allows 6 h, the exam is packed with its own limit
TIME_LIMIT_S = 30 * 60           # a paper is 30 minutes; the browser hands it in when the time is over
LATE_GRACE_S = 90                # a hand-in later than this after the limit is marked "late" (it is still marked)
TOL = 0.5


def _load() -> dict[int, list[dict]]:
    papers = {}
    for n, path in FILES.items():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[midterm] cannot read {path.name}: {e}")
            continue
        types = [g for g in data.get("types", []) if isinstance(g, dict) and g.get("tasks")]
        if types:
            papers[n] = types
    return papers


PAPERS = _load()


def overview() -> list[dict]:
    return [{"n": n, "title": f"Midterm {n}", "questions": len(types), "points": sum(g["tasks"][0].get("points", 1) for g in types)}
            for n, types in sorted(PAPERS.items())]


# ---------------------------------------------------------------- building a paper
def _num(f: dict, fid: str, value) -> None:
    f[fid] = {"t": "n", "v": value}


def _income_statement(t: dict):
    f, rows = {}, []
    for i, r in enumerate(t["rows"]):
        row = {"no": r["no"], "label": r["label"], "bold": bool(r.get("bold"))}
        if r.get("answer") is not None:              # rows without data have no answer cell
            row["id"] = f"r{i}"
            _num(f, row["id"], r["answer"])
        rows.append(row)
    public = {"company": t.get("company", ""), "unit": t.get("unit", ""), "description": t["description"], "information": t["information"],
              "instruction": t["instruction"], "answer_format": t.get("answer_format", []), "rows": rows}
    return public, {"f": f, "x": t.get("explanation", "")}


def _event_impact(t: dict):
    f, events, notes = {}, [], []
    for ev in t["events"]:
        n, bs, inc = ev["no"], ev["balance_sheet"], ev["income_statement"]

        def lines(side: str, tag: str) -> list[dict]:
            out = []
            for i, ln in enumerate(bs[side]):
                row = {"label": ln["label"], "given": bool(ln.get("given"))}
                if row["given"]:
                    row.update(change=ln["change"], closing=ln["closing"])
                else:
                    row["cid"], row["zid"] = f"e{n}.{tag}{i}.c", f"e{n}.{tag}{i}.z"
                    _num(f, row["cid"], ln["change"])
                    _num(f, row["zid"], ln["closing"])
                out.append(row)
            return out

        totals = {}
        for tag, src in (("ta", "total_assets"), ("tl", "total_equity_and_liabilities")):
            totals[tag] = {"cid": f"e{n}.{tag}.c", "zid": f"e{n}.{tag}.z"}
            _num(f, totals[tag]["cid"], bs[src]["change"])
            _num(f, totals[tag]["zid"], bs[src]["closing"])
        statement = {}
        for tag, src in (("i", "income"), ("x", "expense_cost")):
            part = inc[src]
            statement[tag] = {"sid": f"e{n}.{tag}.s", "cid": f"e{n}.{tag}.c", "zid": f"e{n}.{tag}.z"}
            f[statement[tag]["sid"]] = {"t": "s", "v": part["item"]}
            _num(f, statement[tag]["cid"], part["change"])
            _num(f, statement[tag]["zid"], part["closing"])
        statement["p"] = {"cid": f"e{n}.p.c", "zid": f"e{n}.p.z"}
        _num(f, statement["p"]["cid"], inc["profit_or_loss"]["change"])
        _num(f, statement["p"]["zid"], inc["profit_or_loss"]["closing"])
        events.append({"no": n, "text": ev["text"], "assets": lines("assets", "a"), "equity_and_liabilities": lines("equity_and_liabilities", "l"),
                       "totals": totals, "statement": statement})
        notes.append(f"Event {n}: {ev.get('explanation', '')}")
    public = {"company": t.get("company", ""), "unit": t.get("unit", ""), "description": t.get("description", ""), "instructions": t.get("instructions", []),
              "opening": t["opening_balance_sheet"], "options": t["income_statement_options"], "events": events}
    return public, {"f": f, "x": "\n".join(notes)}


def _journal(t: dict):
    f, entries = {}, []
    for e in t["entries"]:
        entries.append({"no": e["no"], "text": e["text"], "amount": e["amount"], "did": f"j{e['no']}.d", "cid": f"j{e['no']}.c"})
        f[f"j{e['no']}.d"] = {"t": "s", "v": e["debit"]}
        f[f"j{e['no']}.c"] = {"t": "s", "v": e["credit"]}
    public = {"description": t["description"], "options": t["account_options"], "entries": entries}
    return public, {"f": f, "x": t.get("explanation", "")}


def _trial_balance(t: dict):
    f, rows = {}, []
    for i, r in enumerate(t["trial_balance"]):
        row = {"account": r["account"], "asked": bool(r.get("asked"))}
        if row["asked"]:
            row["did"], row["cid"] = f"t{i}.d", f"t{i}.c"
            _num(f, row["did"], r["debit"])
            _num(f, row["cid"], r["credit"])
        else:
            row.update(debit=r["debit"], credit=r["credit"])
        rows.append(row)
    totals = {}
    for tag, src in (("bs", "balance_sheet_accounts_total"), ("pl", "profit_and_loss_accounts_total")):
        totals[tag] = {"did": f"tot.{tag}.d", "cid": f"tot.{tag}.c"}
        _num(f, totals[tag]["did"], t["trial_balance_totals"][src]["debit"])
        _num(f, totals[tag]["cid"], t["trial_balance_totals"][src]["credit"])
    closing = {}
    for side in ("assets", "equity_and_liabilities"):
        closing[side] = []
        for i, r in enumerate(t["closing_balance_sheet"][side]):
            fid = f"cb.{side[0]}{i}"
            closing[side].append({"label": r["label"], "id": fid})
            _num(f, fid, r["answer"])
    public = {"unit": t.get("unit", ""), "description": t["description"], "ledger": t["ledger"], "rows": rows, "totals": totals, "closing": closing}
    return public, {"f": f, "x": t.get("explanation", "")}


def _ledger_posting(t: dict):
    f, m, accounts = {}, {}, []
    for i, a in enumerate(t["accounts"]):
        sides = {"debit": [], "credit": []}
        for e in a["entries"]:
            sides[e["side"]].append(e["amount"])
        acc = {"name": a["name"], "opening": a.get("opening"), "dn": max(len(sides["debit"]), 1), "cn": max(len(sides["credit"]), 1)}
        acc["did"] = [f"l{i}.d{k}" for k in range(acc["dn"])]
        acc["cid"] = [f"l{i}.c{k}" for k in range(acc["cn"])]
        m[f"l{i}.d"] = {"ids": acc["did"], "v": sorted(sides["debit"])}
        m[f"l{i}.c"] = {"ids": acc["cid"], "v": sorted(sides["credit"])}
        accounts.append(acc)
    questions = []
    for i, q in enumerate(t["questions"]):
        questions.append({"id": f"q{i}", "prompt": q["prompt"], "format": q.get("format", "")})
        _num(f, f"q{i}", q["answer"])
    public = {"unit": t.get("unit", ""), "events": t["economic_events"], "task": t["task"], "accounts": accounts, "questions": questions}
    return public, {"f": f, "m": m, "x": t.get("explanation", "")}


BUILDERS = {"income_statement": _income_statement, "event_impact": _event_impact, "journal": _journal,
            "trial_balance": _trial_balance, "ledger_posting": _ledger_posting}


def _repack(token: str, **extra) -> str:
    payload = grading.open_token(token)
    payload.update(extra)
    return grading.pack(payload)


def _seal_question(no: int, group: dict, t: dict, exam_id: str) -> dict:
    kind, points = t["kind"], t.get("points", 1)
    q = {"no": no, "points": points, "title": group.get("title", ""), "kind": kind}
    if kind == "mcq":
        question = exam.normalize_question({"kind": "mcq", "prompt": t["prompt"], "options": t["options"], "correct": t["correct"],
                                            "explanation": t.get("explanation", ""), "points": min(int(points), 3)}, 1)
        task = {"type": "test", "task_title": group.get("title", ""), "company_name": "", "description": "", "hint": "", "questions": [question]}
        sealed = grading.seal(task, "midterm", "free", fp=[])
        q["kind"] = "test"
        q["task"] = {k: v for k, v in sealed.items() if k != "token"}
        q["token"] = _repack(sealed["token"], ex=exam_id, no=no, pts=points)
    elif kind == "balance_sheet":
        raw = {k: v for k, v in t.items() if k not in ("id", "type_id", "kind", "points", "variant")}
        task = exam.normalize_balance_sheet(copy.deepcopy(raw), "compile")
        sealed = grading.seal(task, "midterm", "free", fp=[])
        q["task"] = {k: v for k, v in sealed.items() if k != "token"}
        q["token"] = _repack(sealed["token"], ex=exam_id, no=no, pts=points)
    elif kind in BUILDERS:
        public, key = BUILDERS[kind](t)
        q["body"] = public
        q["token"] = grading.pack({"v": 1, "jti": secrets.token_urlsafe(12), "iat": int(time.time()), "kind": kind, "ex": exam_id, "no": no, "pts": points, "key": key})
    else:
        raise ValueError(f"unknown question kind {kind!r}")
    return q


_gen_module = None
_gen_failed = False


def _generator_for(type_id: str):
    """The generator function of a question type (build_midterm_tasks.py), or None. The script is loaded once."""
    global _gen_module, _gen_failed
    if _gen_failed:
        return None
    if _gen_module is None:
        try:
            spec = importlib.util.spec_from_file_location("build_midterm_tasks", GENERATORS)
            _gen_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_gen_module)
        except Exception as e:
            _gen_failed = True
            print(f"[midterm] generators unavailable, the ready-made variants are used: {e!r}")
            return None
    for specs in _gen_module.TYPES.values():
        for tid, _title, _kind, gen, _count in specs:
            if tid == type_id:
                return gen
    return None


def fresh_task(group: dict) -> dict | None:
    """A brand-new task of this question type (new company, new numbers), made on the spot. None if the type has no generator."""
    gen = _generator_for(group["type_id"])
    if gen is None:
        return None
    rng = random.Random(secrets.randbits(64))
    for _ in range(5):
        body = gen(rng)
        if body is not None:
            task = {"id": f"{group['type_id']}_live", "type_id": group["type_id"], "kind": group["kind"]}
            task.update(body)
            return task
    return None


def _seal_any(no: int, group: dict, exam_id: str) -> dict:
    """A fresh task if the type has a generator, else (or if that fails) one of the ready-made variants; a bad one is skipped and logged."""
    candidates = []
    live = None
    try:
        live = fresh_task(group)
    except Exception as e:
        print(f"[midterm] generator of {group['type_id']} failed: {e!r}")
    if live:
        candidates.append(live)
    variants = group["tasks"][:]
    random.shuffle(variants)
    for t in candidates + variants:
        try:
            return _seal_question(no, group, t, exam_id)
        except Exception as e:
            print(f"[midterm] {t.get('id')} skipped: {e!r}")
    raise RuntimeError(f"no valid task for {group['type_id']}")


def build(n: int) -> dict | None:
    """One practice paper: a fresh task of every question type, in the order and with the points of the real exam."""
    types = PAPERS.get(n)
    if not types:
        return None
    exam_id = secrets.token_urlsafe(10)
    questions = [_seal_any(no, group, exam_id) for no, group in enumerate(types, start=1)]
    return {"exam_id": exam_id, "n": n, "title": f"Midterm {n}", "time_limit": TIME_LIMIT_S,
            "total_points": sum(q["points"] for q in questions), "questions": questions}


def practice(type_id: str) -> dict | None:
    """One question of a table type (see table_types.py) for the Balance Sheet tab, in the same sealed format as a paper question."""
    spec = table_types.BY_ID.get(type_id)
    if not spec or not spec.get("generator") or spec.get("classic"):
        return None
    for types in PAPERS.values():
        for group in types:
            if group["type_id"] == spec["generator"]:
                return _seal_any(1, group, "practice")
    return None


# ---------------------------------------------------------------- marking
def _grade_fields(key: dict, answers: dict) -> tuple[dict, int, int, bool]:
    fields, good, units, answered = {}, 0, 0, False
    for fid, spec in key.get("f", {}).items():
        raw = answers.get(fid)
        raw = "" if raw is None else str(raw)[:80].strip()
        if spec["t"] == "n":
            got = grading.parse_amount(raw)
            ok = got is not None and abs(got - spec["v"]) <= TOL
        else:
            ok = raw == spec["v"]
        answered = answered or bool(raw)
        units += 1
        good += ok
        fields[fid] = {"correct": ok, "expected": spec["v"]}
    for gid, spec in key.get("m", {}).items():       # a side of a ledger account: the amounts written there, in any order
        mine = []
        for fid in spec["ids"]:
            raw = answers.get(fid)
            raw = "" if raw is None else str(raw)[:80].strip()
            if raw:
                answered = True
                got = grading.parse_amount(raw)
                mine.append(got if got is not None else float("nan"))
        mine.sort()
        ok = len(mine) == len(spec["v"]) and all(abs(a - b) <= TOL for a, b in zip(mine, spec["v"]))
        units += 1
        good += ok
        fields[gid] = {"correct": ok, "expected": spec["v"], "ids": spec["ids"]}
    return fields, good, units, answered


def grade_one(payload: dict, answers: dict) -> dict:
    if not isinstance(answers, dict) or len(answers) > 200:
        raise grading.TokenError("Invalid answers.")
    kind, pts = payload["kind"], payload["pts"]
    if not any(str(v).strip() for v in answers.values() if v is not None and not (isinstance(v, list) and not v)):
        answers = {}                  # nothing typed or chosen: marked as a blank page (no free points for empty sides of a ledger account)
    if kind in ("balance_sheet", "test"):
        res = grading.grade(payload, answers)
        frac = res["score"] / res["total"] if res["total"] else 0
        detail = {k: res[k] for k in ("cells", "questions") if k in res}
        detail["explanation"] = res.get("explanation", "")
        answered = res["answered"]
    else:
        fields, good, units, answered = _grade_fields(payload["key"], answers)
        frac = good / units if units and answered else 0
        detail = {"fields": fields, "explanation": payload["key"].get("x", "")}
    return {"no": payload["no"], "kind": kind, "max": pts, "points": round(pts * frac, 2), "answered": answered, "detail": detail}


def grade_paper(items: list[dict]) -> dict:
    """items: [{"token": ..., "answers": {...}}, ...] for the questions of ONE paper. Returns the result of every question and the total."""
    if not items:
        raise grading.TokenError("Nothing to mark.")
    payloads, seen, exam_id = [], set(), None
    for it in items:
        p = grading.open_token(it.get("token", ""))
        if "ex" not in p or "no" not in p or "pts" not in p:
            raise grading.TokenError("This is not an exam question.")
        exam_id = exam_id or p["ex"]
        if p["ex"] != exam_id or p["no"] in seen:
            raise grading.TokenError("The questions belong to different papers.")
        if time.time() - p.get("iat", 0) > PAPER_TOKEN_TTL:
            raise grading.TokenError("This paper has expired. Please start a new one.")
        seen.add(p["no"])
        payloads.append((p, it.get("answers") or {}))
    results = sorted((grade_one(p, a) for p, a in payloads), key=lambda r: r["no"])
    elapsed = int(time.time() - min(p.get("iat", 0) for p, _ in payloads))
    return {"exam_id": exam_id, "points": round(sum(r["points"] for r in results), 2), "max": sum(r["max"] for r in results), "questions": results,
            "elapsed": elapsed, "time_limit": TIME_LIMIT_S, "late": elapsed > TIME_LIMIT_S + LATE_GRACE_S}


def grade_single(token: str, answers: dict) -> dict:
    """Marks one practice question (Balance Sheet tab, table types). The Score is not affected."""
    p = grading.open_token(token)
    if "pts" not in p or "no" not in p:
        raise grading.TokenError("This is not a table question.")
    return grade_one(p, answers)
