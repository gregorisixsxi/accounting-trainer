"""Server-side grading, so that results (and therefore ratings and the archive) cannot be forged in the browser.

When a task is served, seal() removes everything that gives the answers away (values of the cells the student
has to type, the right options, the explanations) and packs them into a signed token. The browser sends the token
back with the student's answers to /api/check, where open_token() verifies the signature and grade() does the marking.
The token is signed with TASK_SECRET (set it in the environment; without it a random secret is used and tokens
of open pages stop working after a restart).
"""
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import time

import rating

SECRET = (os.environ.get("TASK_SECRET") or "").encode() or secrets.token_bytes(32)
if not os.environ.get("TASK_SECRET"):
    print("[grading] TASK_SECRET is not set: using a random secret (open tasks become invalid after a restart). Set it in the environment.")

TOKEN_TTL_SECONDS = 6 * 3600
MIN_SECONDS_PER_POINT = float(os.environ.get("MIN_SECONDS_PER_POINT", "1.2"))   # answers submitted faster than this per point are "too fast" (no rating)


class TokenError(ValueError):
    pass


# ---------------------------------------------------------------- tokens
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()


def pack(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    return body + "." + _sign(body)


def open_token(token: str) -> dict:
    try:
        body, sig = str(token).split(".")
    except ValueError:
        raise TokenError("Invalid task token.")
    if not hmac.compare_digest(_sign(body), sig):
        raise TokenError("Invalid task token.")
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        raise TokenError("Invalid task token.")
    if time.time() - payload.get("iat", 0) > TOKEN_TTL_SECONDS:
        raise TokenError("This task has expired. Please start a new one.")
    return payload


# ---------------------------------------------------------------- sealing
def _condition(task: dict) -> str:
    text = task.get("description", "")
    if task.get("items"):
        text += "\n\nBalance sheet value items:\n" + "\n".join(f"{i['name']}: {i['amount']:,}" for i in task["items"])
    return text[:6000]


def seal(task: dict, topic: str, source: str) -> dict:
    """Returns the task as the browser gets it: no answers, plus a token that carries them."""
    public = copy.deepcopy(task)
    payload = {
        "v": 1, "jti": secrets.token_urlsafe(12), "iat": int(time.time()), "topic": topic, "source": source,
        "kind": task["type"], "title": task.get("task_title", ""), "company": task.get("company_name", ""),
        "condition": _condition(task), "explanation": task.get("explanation", ""),
        "penalty": rating.task_penalty(task["type"], task.get("variant", ""), source),
    }
    if task["type"] == "balance_sheet":
        ncols = len(task["columns"])
        key = {}

        def seal_row(label, cells):
            for i, cell in enumerate(cells):
                if cell["blank"]:
                    key[cell["id"]] = {"v": cell["value"], "l": label + (f" · {task['columns'][i]}" if ncols > 1 else "")}
                if cell["blank"] or cell["hidden"]:
                    cell["value"] = None                      # not sent to the browser

        for side in public["sides"]:
            for group in side["groups"]:
                for row in group["rows"]:
                    seal_row(row["label"], row["cells"])
                if group["subtotal"]:
                    seal_row(group["subtotal"]["label"], group["subtotal"]["cells"])
            seal_row(side["total"]["label"], side["total"]["cells"])
        payload["key"] = key
        public["explanation"] = ""
    else:
        key = []
        for q in public["questions"]:
            key.append({"id": q["id"], "k": q["kind"], "c": q["correct"], "t": q.get("tolerance", 0), "p": q.get("points", 1),
                        "e": q.get("explanation", ""), "q": q["prompt"], "o": q.get("options"), "u": q.get("unit", "")})
            q.pop("correct", None)
            q["explanation"] = ""
        payload["key"] = key
    public["token"] = pack(payload)
    return public


# ---------------------------------------------------------------- grading
def parse_amount(text) -> float | None:
    """'1 250', '+100', '$1,250', '1.250.000' ... -> number; None if it is not a number."""
    t = re.sub(r"[\s ]", "", str(text))
    t = re.sub(r"[A-Za-z$€£]", "", t)
    t = re.sub(r"^\+", "", t)
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    t = t.replace(",", "")
    return float(t) if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


def _fmt(n) -> str:
    return f"{int(n):,}" if float(n).is_integer() else f"{n:,.2f}"


def grade(payload: dict, answers: dict) -> dict:
    """Marks the answers. Returns score/total (points), per-item verdicts for the browser and the archive review."""
    if not isinstance(answers, dict) or len(answers) > 80:
        raise TokenError("Invalid answers.")
    review, answered = [], False

    if payload["kind"] == "balance_sheet":
        cells, score = {}, 0
        for cid, spec in payload["key"].items():
            raw = str(answers.get(cid, "") if answers.get(cid) is not None else "")[:40].strip()
            got = parse_amount(raw)
            good = got is not None and abs(got - spec["v"]) <= 0.5
            score += good
            answered = answered or bool(raw)
            cells[cid] = {"correct": good, "expected": spec["v"]}
            review.append({"label": spec["l"], "yours": raw, "expected": _fmt(spec["v"]), "correct": good, "note": ""})
        total = len(cells)
        result = {"cells": cells, "explanation": payload.get("explanation", "")}
    else:
        questions, score, total = {}, 0, 0
        for n, q in enumerate(payload["key"], start=1):
            given, kind, correct, points = answers.get(q["id"]), q["k"], q["c"], q["p"]
            total += points
            options = q.get("o") or (["True", "False"] if kind == "tf" else [])
            if kind == "numeric":
                raw = str(given if given is not None else "")[:40].strip()
                got = parse_amount(raw)
                tol = max(q.get("t") or 0, 0.005 if abs(correct) < 10 else 0.5)
                good = got is not None and abs(got - correct) <= tol
                yours, expected, exp_payload = raw, _fmt(correct) + (f" {q['u']}" if q.get("u") else ""), correct
                answered = answered or bool(raw)
            else:
                if kind == "multi":
                    chosen = sorted({i for i in (given or []) if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(options)})
                    right = sorted(correct)
                elif kind == "tf":
                    chosen = [] if given is None else [0 if str(given).lower() == "true" else 1]
                    right = [0 if correct else 1]
                else:
                    chosen = [given] if isinstance(given, int) and not isinstance(given, bool) and 0 <= given < len(options) else []
                    right = [correct]
                good = chosen == right
                yours, expected = "; ".join(options[i] for i in chosen), "; ".join(options[i] for i in right)
                exp_payload = right if kind != "tf" else bool(correct)
                answered = answered or bool(chosen)
            score += points if good else 0
            questions[q["id"]] = {"correct": good, "expected": exp_payload, "explanation": q.get("e", "")}
            review.append({"label": f"Q{n}. {q['q']}"[:300], "yours": yours, "expected": expected, "correct": good, "note": q.get("e", "")})
        result = {"questions": questions, "explanation": ""}

    result.update(score=score, total=total, answered=answered, review=review)
    return result
