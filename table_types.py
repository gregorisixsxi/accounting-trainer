"""The table task types of the mid-term, with names to talk about them ("make me a TRIAL-BALANCE task").

This is the single list the website (Balance Sheet tab: choose a table type), midterm.py (practice and paper) and the person or the AI
extending the project work from. To add a type: write its generator in scripts/build_midterm_tasks.py (a function rng -> task body),
add its exemplar to midterm_templates.py, add a renderer MT_RENDER[kind] in index.html and a builder in midterm.py, then add a line here.

code            what the student does                                         real task     generator (build_midterm_tasks.py)
BS-COMPILE      sorts ~12 described items into the 15 captions of the sheet   midterm 1 / 4  m1_q04
BS-TOTALS       sees every line, calculates subtotals and both totals         (practice)     pool.py _totals_raw (the classic Balance Sheet tab)
IS-NATURE       income statement, nature of expense method, 1 trap item       midterm 1 / 5  m1_q05
EVENTS-2        two events on an opening balance sheet + income statement     midterm 1 / 6  m1_q06
JOURNAL         debit / credit accounts of 4 transactions                     midterm 2 / 1  m2_q01
TRIAL-BALANCE   T-accounts -> missing trial balance cells + closing sheet     midterm 2 / 2  m2_q02
T-ACCOUNTS      posts 4 events into 6 ledger accounts, gross profit, stock    midterm 2 / 3  m2_q03
"""
import midterm_templates

TABLE_TYPES = [
    dict(id="compile_bs", code="BS-COMPILE", short="Compile", title="Compile the balance sheet", classic="compile", exemplar="m1_q04", generator="m1_q04",
         blurb="Sort described items into the captions of the balance sheet and add them up.", rated=True),
    dict(id="totals_bs", code="BS-TOTALS", short="Totals", title="Balance sheet totals", classic="totals", exemplar=None, generator=None,
         blurb="Every line is shown: calculate each subtotal and both totals.", rated=True,
         keep="All lines shown, the student calculates every subtotal and both totals. Assets = equity + liabilities.", vary="Company, lines, amounts."),
    dict(id="income_stmt", code="IS-NATURE", short="Income statement", title="Compile the income statement", classic=None, exemplar="m1_q05", generator="m1_q05",
         blurb="Nature of expense method. One item in the data is a trap.", rated=False),
    dict(id="event_impact", code="EVENTS-2", short="Two events", title="Impact of two economic events", classic=None, exemplar="m1_q06", generator="m1_q06",
         blurb="Opening balance sheet, two events: changes, closing values and the effect on profit.", rated=False),
    dict(id="journal", code="JOURNAL", short="Journal", title="Record transactions in the journal", classic=None, exemplar="m2_q01", generator="m2_q01",
         blurb="Choose the debit and the credit account of four transactions.", rated=False),
    dict(id="trial_balance", code="TRIAL-BALANCE", short="Trial balance", title="Ledger, trial balance and closing balance sheet", classic=None, exemplar="m2_q02", generator="m2_q02",
         blurb="Read the T-accounts, complete the trial balance, build the closing balance sheet.", rated=False),
    dict(id="ledger_posting", code="T-ACCOUNTS", short="T-accounts", title="Post the events into ledger accounts", classic=None, exemplar="m2_q03", generator="m2_q03",
         blurb="Write each amount on the right side of the right account, then gross profit and stock.", rated=False),
]
BY_ID = {t["id"]: t for t in TABLE_TYPES}


def catalog() -> list[dict]:
    """What the browser needs to draw the chips of the Balance Sheet tab."""
    return [{k: t[k] for k in ("id", "code", "short", "title", "blurb", "rated")} | {"classic": t["classic"]} for t in TABLE_TYPES]


def describe() -> str:
    """A readable summary (also used to brief an AI that writes new tasks of a type)."""
    ex = {e["id"]: e for e in midterm_templates.EXEMPLARS}
    lines = []
    for t in TABLE_TYPES:
        e = ex.get(t["exemplar"]) or {}
        lines.append(f"{t['code']} ({t['id']}): {t['title']}. {t['blurb']}\n  KEEP: {t.get('keep') or e.get('keep', '')}\n  VARY: {t.get('vary') or e.get('vary', '')}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
