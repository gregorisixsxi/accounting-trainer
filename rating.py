"""FACEIT-style rating: Score (called ELO inside the code and the database), levels 1-10 and a calibration phase.

How it works
  * A new account starts at 1000 Score and has CALIBRATION_TASKS scored tasks of calibration. While it calibrates
    the level is not shown ("?"), rating moves fast (K=125) and, when the calibration ends, the rating is capped
    at the top of level 7 (1700). Nobody can finish calibration above Level 7.
  * Afterwards (PRO accounts only: free accounts keep their Score and level but earn nothing more and are not on
    the leaderboard) every scored task moves the rating by  K * weight * (accuracy - expected_accuracy)  with K=30.
    The expected accuracy rises with the rating (about 54% at 1000, 75% at 1500, 93% at 2000), so the higher you
    are the more precise you must be to keep climbing; a harder task type lowers the expectation a little.
    Level 10 (2001+) therefore needs near-perfect accuracy over many tasks.
  * Small tasks count less (weight = tasks points / 6, at most 1) and after calibration at most DAILY_GAIN_CAP
    points can be gained per UTC day.

The level thresholds are the FACEIT ones.
"""

LEVEL_FLOORS = [100, 801, 951, 1101, 1251, 1401, 1551, 1701, 1851, 2001]   # ELO needed for level 1..10
START_ELO = 1000
MIN_ELO, MAX_ELO = 100, 3000
CALIBRATION_TASKS = 5
CALIBRATION_TOPICS = ("balance_sheet",)   # only these task types count towards calibration
CALIBRATION_MAX_ELO = 1700          # top of level 7
CALIBRATION_MAX_LEVEL = 7
K_CALIBRATION, K_RANKED = 125, 30     # +25% over the first version (100 / 24)
DAILY_GAIN_CAP = 125

# how much harder than average a task type is (lowers the expected accuracy)
DIFFICULTY_PENALTY = {"totals": 0.0, "compile": 0.04, "comparative": 0.08, "test": 0.0}
PRO_PENALTY = 0.05                  # AI-generated tasks are a bit less predictable


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def level_for(elo: int) -> int:
    level = 1
    for i, floor in enumerate(LEVEL_FLOORS, start=1):
        if elo >= floor:
            level = i
    return level


def expected_accuracy(elo: int, penalty: float = 0.0) -> float:
    base = 0.45 + 0.5 * (elo - 800) / 1400
    return clamp(base, 0.35, 0.93) - penalty


def task_penalty(kind: str, variant: str, source: str) -> float:
    key = variant if kind == "balance_sheet" else "test"
    return DIFFICULTY_PENALTY.get(key, 0.0) + (PRO_PENALTY if source == "pro" else 0.0)


def rating_delta(elo: int, score: int, total: int, penalty: float, calibrating: bool) -> float:
    accuracy = score / total if total else 0.0
    weight = min(1.0, total / 6)
    k = K_CALIBRATION if calibrating else K_RANKED
    return k * weight * (accuracy - expected_accuracy(elo, penalty))


def apply_result(state: dict, score: int, total: int, penalty: float, today: str) -> tuple[dict, dict]:
    """state: elo, calibration_left, elo_day, elo_day_gain. Returns (new_state, rating info for the browser)."""
    elo, left = int(state["elo"]), int(state["calibration_left"])
    day_gain = int(state["elo_day_gain"]) if state.get("elo_day") == today else 0
    calibrating = left > 0

    delta = round(rating_delta(elo, score, total, penalty, calibrating))
    if not calibrating and delta > 0:
        delta = max(0, min(delta, DAILY_GAIN_CAP - day_gain))     # grinding guard
    new_elo = int(clamp(elo + delta, MIN_ELO, MAX_ELO))
    gained = max(0, new_elo - elo)

    completed = False
    if calibrating:
        left -= 1
        if left == 0:
            completed = True
            new_elo = min(new_elo, CALIBRATION_MAX_ELO)           # best possible result of calibration: Level 7
    new_state = {
        "elo": new_elo,
        "level": min(level_for(new_elo), CALIBRATION_MAX_LEVEL) if left > 0 else level_for(new_elo),   # still calibrating: at most Level 7
        "calibration_left": left,
        "elo_day": today,
        "elo_day_gain": day_gain + gained if not calibrating else 0,
    }
    info = {
        "elo_before": elo, "elo_after": new_elo, "delta": new_elo - elo,
        "level_before": level_for(elo), "level_after": new_state["level"],
        "calibrating": left > 0, "calibration_left": left, "calibration_total": CALIBRATION_TASKS,
        "calibration_completed": completed,
        "daily_cap_reached": (not calibrating) and delta == 0 and day_gain >= DAILY_GAIN_CAP,
    }
    return new_state, info


def profile_view(user: dict) -> dict:
    """What the profile screen needs: level, progress to the next level and calibration status."""
    elo, left = int(user.get("elo", START_ELO)), int(user.get("calibration_left", CALIBRATION_TASKS))
    calibrating = left > 0
    level = level_for(elo)
    if calibrating:
        level = min(level, CALIBRATION_MAX_LEVEL)
    points_total = int(user.get("points_total", 0))
    out = {
        "level": level,
        "elo": elo,
        "calibrating": calibrating,
        "calibration_left": left,
        "calibration_total": CALIBRATION_TASKS,
        "calibration_done": CALIBRATION_TASKS - left,
        "tasks": int(user.get("tasks_total", 0)),
        "accuracy": round(100 * int(user.get("points_correct", 0)) / points_total) if points_total else None,
    }
    if calibrating:
        out.update(progress=round(100 * (CALIBRATION_TASKS - left) / CALIBRATION_TASKS), next_level=None, elo_to_next=None)
    elif level >= 10:
        out.update(progress=100, next_level=None, elo_to_next=0)
    else:
        lo, hi = LEVEL_FLOORS[level - 1], LEVEL_FLOORS[level]
        out.update(progress=round(100 * (elo - lo) / (hi - lo)), next_level=level + 1, elo_to_next=hi - elo)
    return out
