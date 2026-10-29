"""Builds midterm1_tasks.json and midterm2_tasks.json.

Every task copies the TYPE and the LOGIC of a real midterm task (see midterm_templates.py) with new companies, numbers and
wording. Answers are always computed here, never typed by hand. Tasks the website can already render are also checked with exam.py.

    python scripts/build_midterm_tasks.py            # rewrites both files in the project folder (same seed = same files)
    python scripts/build_midterm_tasks.py 7          # another seed = another set of numbers
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import exam  # noqa: E402
from midterm_templates import ASSET_KEYS, COMPILE_DESCRIPTION, ROWS, sheet_sides  # noqa: E402

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 20261029
CUR = "th USD"          # unit of the table tasks; the real exam says thHUF, the site is English/USD only (see generator.ENGLISH_RULE)
NOT_AFFECTED = "Not affected by the event!"

COMPANIES = [
    "Alder Foods", "Birchwood Supplies", "Cedar Point Trading", "Dunmore Industries", "Elmstead Goods", "Foxglove Retail",
    "Granite Hill", "Harbor Light", "Ivory Peak Systems", "Juniper Works", "Kestrel Logistics", "Lakeshore Textiles",
    "Meadowbrook Farm", "Northgate Metals", "Oakridge Furnishings", "Pinecrest Foods", "Quarry Lane Tools", "Riverbend Trading",
    "Silverline Print", "Tamarack Paper", "Union Street Cafe", "Valley Forge Parts", "Willow Creek Dairy", "Yarrow Electronics",
]


# ---------------------------------------------------------------- helpers
def comma(v: int) -> str:
    return f"{v:,}"


def spaced(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def pick_options(rng, correct, mistakes, step, fmt, allow_negative=False):
    """Correct value + 3 distractors (typical mistakes first, then neighbours); returns (option texts, index of the correct one)."""
    wrong = []
    for m in mistakes:
        if m != correct and m not in wrong and (allow_negative or m >= 0):
            wrong.append(m)
    rng.shuffle(wrong)
    wrong = wrong[:3]
    k = 1
    while len(wrong) < 3:
        for cand in (correct + k * step, correct - k * step):
            if len(wrong) < 3 and cand != correct and cand not in wrong and (allow_negative or cand >= 0):
                wrong.append(cand)
        k += 1
    values = [correct] + wrong
    rng.shuffle(values)
    return [fmt(v) for v in values], values.index(correct)


def split_amount(rng, total: int, parts: int, unit: int = 10) -> list[int]:
    """total split into `parts` positive multiples of `unit`."""
    slots = total // unit
    parts = min(parts, slots)
    cuts = sorted(rng.sample(range(1, slots), parts - 1)) if parts > 1 else []
    edges = [0] + cuts + [slots]
    return [(b - a) * unit for a, b in zip(edges, edges[1:])]


# ---------------------------------------------------------------- numeric multiple choice
def m1_q01(rng):
    """Accounting equation: Assets = Liabilities + Equity; one item is missing."""
    while True:
        cash = 500 * rng.randint(4, 18)
        tang = 500 * rng.randint(4, 16)
        inv = 500 * rng.randint(2, 8) if rng.random() < 0.35 else 0
        re = rng.choice([-3000, -2000, -1500, -1000, 1000, 1500, 2000, 3000])
        ltl = 500 * rng.randint(6, 20)
        assets = cash + tang + inv
        contributed = assets - ltl - re
        if contributed >= 1000:
            break
    missing = rng.choices(["contributed", "liabilities", "cash"], [6, 2, 2])[0]
    items = [("Cash", cash), ("Tangible assets", tang)] + ([("Inventories", inv)] if inv else []) + [
        ("Retained earnings", re), ("Long-term liabilities", ltl), ("Contributed capital", contributed)]
    names = {"contributed": "Contributed capital", "liabilities": "Long-term liabilities", "cash": "Cash"}
    given = "; ".join(f"{n}: {comma(v)}$" for n, v in items if n != names[missing])
    ask = {"contributed": "Contributed capital for the period was:", "liabilities": "Long-term liabilities at the end of the period were:",
           "cash": "Cash at the end of the period was:"}[missing]
    prompt = (f"Use the accounting equation to calculate the missing amount!\nAt the end of the current accounting period, "
              f"account balances were as follows: {given}. {ask}")
    if missing == "contributed":
        correct, calc = assets - ltl - re, f"{assets}-{ltl}-({re})"
        mistakes = [assets - ltl + re, assets - ltl, assets + ltl - re]
        why = f"Equity = Assets - Liabilities = {comma(assets)} - {comma(ltl)} = {comma(assets - ltl)}; Contributed capital = Equity - Retained earnings = {comma(assets - ltl)} - ({comma(re)}) = {comma(correct)}."
    elif missing == "liabilities":
        correct, calc = assets - contributed - re, f"{assets}-{contributed}-({re})"
        mistakes = [assets - contributed + re, assets - contributed, assets + contributed + re]
        why = f"Equity = {comma(contributed)} + ({comma(re)}) = {comma(contributed + re)}; Liabilities = Assets - Equity = {comma(assets)} - {comma(contributed + re)} = {comma(correct)}."
    else:
        correct, calc = ltl + contributed + re - tang - inv, f"{ltl}+{contributed}+({re})-{tang}-{inv}"
        mistakes = [ltl + contributed - re - tang - inv, ltl + contributed - tang - inv, ltl + contributed + re]
        why = f"Assets = Liabilities + Equity = {comma(ltl)} + {comma(contributed + re)} = {comma(ltl + contributed + re)}; Cash = {comma(ltl + contributed + re)} - {comma(tang + inv)} = {comma(correct)}."
    options, idx = pick_options(rng, correct, mistakes, 500, str)
    return dict(points=2, prompt=prompt, options=options, correct=idx, calc=calc, explanation=why)


def m1_q02(rng):
    """Operating profit from a short list of items (nature of expense method)."""
    price, mat = 1000 * rng.randint(30, 120), 1000 * rng.randint(3, 15)
    wages, dep, amt = 1000 * rng.randint(5, 25), 1000 * rng.randint(1, 6), 1000 * rng.randint(1, 6)
    last = rng.choices(["other income", "other expenses", "capitalised own output"], [5, 3, 2])[0]
    base = price - mat - wages - dep
    sign = -1 if last == "other expenses" else 1
    correct = base + sign * amt
    prompt = (f"The company sells its product for {comma(price)} USD, used raw materials {comma(mat)} USD, salaries and wages expense "
              f"{comma(wages)} USD, depreciation expense {comma(dep)} USD and {last} {comma(amt)} USD. The operating profit:")
    mistakes = [base, base - sign * amt, mat + wages + dep, price + amt if sign > 0 else price - amt]
    options, idx = pick_options(rng, correct, mistakes, 1000, lambda v: f"{comma(v)} USD")
    calc = f"{price}-{mat}-{wages}-{dep}{'-' if sign < 0 else '+'}{amt}"
    why = (f"Operating profit = revenue - material expenses - personnel expenses - depreciation {'-' if sign < 0 else '+'} {last} = "
           f"{comma(price)} - {comma(mat)} - {comma(wages)} - {comma(dep)} {'-' if sign < 0 else '+'} {comma(amt)} = {comma(correct)} USD.")
    return dict(points=2, prompt=prompt, options=options, correct=idx, calc=calc, explanation=why)


def m1_q03(rng):
    """Value of purchased inventory after a return (or price reduction) of r percent."""
    item = rng.choice(["raw materials", "goods", "packaging materials", "spare parts"])
    x, term, r = 100 * rng.randint(4, 25), rng.choice([8, 10, 14, 15, 20, 30]), rng.choice([5, 10, 15, 20, 25])
    reason = rng.choice(["Due to quality complaints", "Because of damage during the delivery", "Because of a wrong specification"])
    prompt = (f"The following economic events occurred at the company during the reporting period.\n"
              f"The company purchased {item} worth {comma(x)} Th USD with a {term}-day payment term.\n"
              f"{reason}, {r}% of the {item} were returned to the supplier.\n"
              f"Determine the balance sheet value of the {item} after these events.")
    back = x * r // 100
    correct = x - back
    mistakes = [x, x + back, x - 2 * back]
    options, idx = pick_options(rng, correct, mistakes, 10, lambda v: f"{spaced(v)} Th USD")
    why = (f"The purchase on credit increases the {item} to {comma(x)} Th USD; the {r}% return takes {comma(back)} Th USD back, "
           f"so the balance sheet value is {comma(x)} - {comma(back)} = {comma(correct)} Th USD. The payment term does not change the value.")
    return dict(points=2, prompt=prompt, options=options, correct=idx, calc=f"{x}-{x}*{r / 100}", explanation=why)


def m2_q04(rng):
    """Credit (or debit) turnover of Accounts Receivable."""
    co = rng.choice(COMPANIES)
    opening, received, cash_paid = 100 * rng.randint(8, 30), 100 * rng.randint(1, 6), 100 * rng.randint(2, 12)
    cost = 100 * rng.randint(15, 40)
    sale = cost + 100 * rng.randint(5, 25)
    days = rng.choice([5, 8, 10, 15, 30])
    credit, closing = received + cash_paid, opening + sale - received - cash_paid
    ask_credit = rng.random() < 0.75
    prompt = (f"At {co}, you know the following information about the period in question:\n"
              f"• Beginning balance of Accounts receivable is {comma(opening)} Th USD,\n"
              f"• {comma(received)} Th USD was received into the bank account for a previous sale,\n"
              f"• Inventory worth {comma(cost)} Th USD was invoiced for {comma(sale)} Th USD, with a payment deadline of {days} days,\n"
              f"• A customer paid in cash for a previous sale of {comma(cash_paid)} Th USD.\n"
              f"How much is the {'credit' if ask_credit else 'debit'} turnover of Accounts receivable on the period-end general ledger statement?")
    if ask_credit:
        correct, calc, mistakes = credit, f"{received}+{cash_paid}", [opening + sale, closing, sale]
        why = f"The credit side shows the decreases: {comma(received)} + {comma(cash_paid)} = {comma(credit)} Th USD. The sale ({comma(sale)}) is a debit; the closing balance is {comma(closing)}."
    else:
        correct, calc, mistakes = sale, f"{sale}", [credit, opening + sale, closing]
        why = f"The debit side shows the increases: the sale invoiced for {comma(sale)} Th USD (the cost of {comma(cost)} is not a receivable). The opening balance is not turnover."
    options, idx = pick_options(rng, correct, mistakes, 100, lambda v: f"{comma(v)} Th USD")
    return dict(points=2, prompt=prompt, options=options, correct=idx, calc=calc, explanation=why)


def m2_q05(rng):
    """Operating profit of a manufacturer: financial items must be left out."""
    while True:
        rev, mat, pers, dep = 10 * rng.randint(20, 80), 10 * rng.randint(3, 15), 10 * rng.randint(2, 10), 10 * rng.randint(1, 6)
        interest, oexp, oinc = 10 * rng.randint(3, 12), 10 * rng.randint(2, 8), 10 * rng.randint(5, 18)
        correct = rev - mat - pers - dep - oexp + oinc
        if correct >= 100:
            break
    received = rng.random() < 0.6
    prompt = (f"A manufacturing company achieved {rev} thUSD in revenue during the year. Its material-type expenses were {mat} thUSD, "
              f"personnel-type expenses {pers} thUSD, and depreciation {dep} thUSD. The company {'received' if received else 'paid'} "
              f"{interest} thUSD in interest, and had {oexp} thUSD in other expenses and {oinc} thUSD in other income. Determine the operating profit!")
    mistakes = [correct + interest if received else correct - interest, correct + oexp, correct - dep]
    options, idx = pick_options(rng, correct, mistakes, 10, str)
    why = (f"Operating profit = {rev} - {mat} - {pers} - {dep} - {oexp} + {oinc} = {correct} thUSD. "
           f"The interest {'received' if received else 'paid'} is a financial item, so it is not part of the operating profit.")
    return dict(points=2, prompt=prompt, options=options, correct=idx, calc=f"{rev}-{mat}-{pers}-{dep}-{oexp}+{oinc}", explanation=why)


def m2_q06(rng):
    """Gross profit of one sale: an advance received earlier is a cash matter, not a profit item."""
    while True:
        s = 50 * rng.randint(6, 30)
        c = 50 * rng.randint(3, int(s / 50) - 1)
        a = 50 * rng.randint(2, max(2, s // 100))
        if s - c >= 100 and a < s:
            break
    prompt = (f"A business has a sales price of {s} USD, for this transaction the buyer previously gave an advance of {a} USD, "
              f"and the purchase price of the goods was {c} USD. What is the business's gross profit?")
    correct = s - c
    mistakes = [s - c - a, -(s - c - a), s - c + a, s - a]
    options, idx = pick_options(rng, correct, mistakes, 50, str, allow_negative=True)
    why = f"Gross profit = sales price - purchase price = {s} - {c} = {correct} USD. The advance of {a} USD only decides when the money arrived."
    return dict(points=2, prompt=prompt, options=options, correct=idx, calc=f"{s}-{c}", explanation=why)


# ---------------------------------------------------------------- theory multiple choice (stem, right option, 3 wrong options, explanation)
FS = "Balance Sheet", "Income Statement", "Statement of Changes in Equity", "Statement of Cash Flows"
THEORY = {
    "m1_q07": (2, [
        ("Net income for the period appears on the:", "Income Statement and Statement of Changes in Equity.",
         ["Balance Sheet only.", "Statement of Cash Flows only.", "Balance Sheet and Statement of Cash Flows."],
         "Net income is the result of the income statement and is added to retained earnings in the statement of changes in equity."),
        ("The ending balance in Contributed Capital appears on the:", "Balance Sheet and Statement of Changes in Equity.",
         ["Income Statement and Statement of Cash Flows.", "Income Statement only.", "Balance Sheet only."],
         "Contributed capital is an equity item: the balance sheet shows its ending balance and the statement of changes in equity shows its movement."),
        ("The ending balance in Accounts Receivable appears on the:", "Balance Sheet only.",
         ["Income Statement and Balance Sheet.", "Statement of Changes in Equity only.", "Income Statement only."],
         "Receivables are assets; only the balance sheet shows their ending balance."),
        ("Sales revenue for the period appears on the:", "Income Statement only.",
         ["Balance Sheet only.", "Balance Sheet and Statement of Changes in Equity.", "Statement of Changes in Equity only."],
         "Revenue is an income statement item; only its result (profit) flows on to equity."),
    ]),
    "m1_q08": (2, [
        ("A general partnership is made up of:", "partners.", ["shareholders.", "sole proprietors.", "debenture holders."],
         "The owners of a partnership are its partners."),
        ("A company limited by shares (Ltd.) is owned by:", "shareholders.", ["partners.", "creditors.", "sole proprietors."],
         "The owners of a company limited by shares are the shareholders."),
        ("A sole proprietorship is owned by:", "one individual.", ["shareholders.", "the members of a partnership.", "the government."],
         "A sole proprietorship has exactly one owner."),
        ("Which form of business has an owner who is personally liable for all debts of the business?", "Sole proprietorship",
         ["Corporation", "Limited-liability partnership", "Public limited company"],
         "A sole proprietor has unlimited personal liability; in the other forms the liability is limited."),
    ]),
    "m1_q09": (1, [
        ("Under the function of expense method, operating profit/loss is compiled using:",
         "Net sales revenue - direct cost of sales - indirect cost of sales + other income - other expenses",
         ["Net sales revenue - material type expenses - personnel type expenses - depreciation + other income - other expenses",
          "Financial income - financial expenses", "Net sales revenue - material type expenses - personnel type expenses - depreciation"],
         "The function of expense method groups costs by function: direct and indirect cost of sales."),
        ("Which of the following is NOT used when compiling operating profit under the nature of expense method?", "Financial income",
         ["Depreciation", "Personnel type expenses", "Capitalised own output"],
         "Financial income belongs to the profit/loss from financial transactions, below the operating profit."),
        ("Profit/loss from financial transactions is compiled using:", "Financial income - financial expenses",
         ["Net sales revenue - financial expenses", "Operating profit + financial income", "Financial income + financial expenses"],
         "It is the balance of financial income and financial expenses."),
        ("Profit or loss before tax is compiled using:", "Operating profit + profit/loss from financial transactions",
         ["Operating profit - financial income", "Net sales revenue - operating profit", "Profit/loss from financial transactions - tax expense"],
         "Operating profit plus the result of financial transactions gives the profit before tax."),
    ]),
    "m1_q10": (1, [
        ("For a trading company, revenue comes mainly from selling:", "Goods purchased for resale",
         ["Own production inventories/products", "Bank services", "Leased assets"],
         "A trading company resells goods that it bought."),
        ("For a bank, revenue comes mainly from:", "Interest and fees on financial services",
         ["Own production inventories/products", "Goods purchased for resale", "Leased buildings"],
         "A bank earns its income from financial services."),
        ("For a consulting firm, revenue comes mainly from:", "Services provided to clients",
         ["Own production inventories/products", "Goods purchased for resale", "Interest received"],
         "A service provider sells services."),
        ("For a company that leases out machines, revenue comes mainly from:", "Lease payments received",
         ["Own production inventories/products", "Bank services", "Taxes"],
         "The main activity is leasing, so the lease payments are the revenue."),
    ]),
    "m1_q11": (1, [
        ("The company paid its trade payables from its bank account. Select the incorrect statement!", "The company's balance sheet total increased.",
         ["The company's liquid assets decreased.", "The company's trade payables decreased.", "The economic event does not affect the profit or loss account."],
         "Assets and liabilities both decreased, so the balance sheet total decreased."),
        ("The owners paid additional capital into the company's bank account. Select the incorrect statement!", "The company's liabilities increased.",
         ["The company's equity increased.", "The company's liquid assets increased.", "The balance sheet total increased."],
         "Contributed capital is equity, not a liability."),
        ("The company repaid part of its bank loan from its bank account. Select the incorrect statement!", "The balance sheet total remained unchanged.",
         ["The company's liquid assets decreased.", "The company's liabilities decreased.", "The company's equity did not change."],
         "Assets and liabilities both decreased, so the balance sheet total decreased."),
        ("The company received a long-term bank loan into its bank account. Select the incorrect statement!", "The company's equity increased.",
         ["The company's liquid assets increased.", "The company's long-term liabilities increased.", "The balance sheet total increased."],
         "A loan is a liability; equity does not change."),
    ]),
    "m1_q12": (1, [
        ("From the following economic events, select which one qualifies as a complex economic event!", "Interest on a bank loan was paid from the bank account.",
         ["The company withdrew cash from its bank account into the till.", "A supplier invoice was settled by bank transfer.", "Retained earnings were transferred to contributed capital."],
         "A complex event affects both the balance sheet and the income statement; paid interest is an expense."),
        ("From the following economic events, select which one qualifies as a complex economic event!", "Goods were sold on credit and the invoice was issued.",
         ["A customer paid an outstanding invoice by bank transfer.", "The owners increased the contributed capital by a bank transfer.", "A short-term loan was converted into a long-term loan."],
         "The sale creates revenue (income statement) and a receivable (balance sheet)."),
        ("From the following economic events, select which one qualifies as a complex economic event!", "The company paid salaries to its employees from the bank account.",
         ["The company repaid a bank loan from its bank account.", "The company purchased equipment on credit.", "The company received an advance from a customer in cash."],
         "Salaries are an expense: the event changes the balance sheet and the profit."),
        ("From the following economic events, select which one qualifies as a complex economic event!", "Depreciation of machinery was recorded at year end.",
         ["Cash was deposited into the bank account.", "Raw materials were purchased on credit.", "The owners contributed capital in cash."],
         "Depreciation is an expense and decreases the value of the machinery."),
    ]),
    "m2_q07": (1, [
        ("Paying a supplier's invoice from the bank account would result in which of the following?", "A debit to trade payables and a credit to bank account.",
         ["A debit to bank account and a credit to trade payables.", "A debit to trade payables and a credit to goods.", "A debit to both trade payables and bank account."],
         "The liability decreases (debit) and the bank balance decreases (credit)."),
        ("Purchasing goods on credit would result in which of the following?", "A debit to goods and a credit to trade payables.",
         ["A debit to trade payables and a credit to goods.", "A debit to goods and a credit to cash.", "A credit to both goods and trade payables."],
         "The inventory increases (debit) and the liability increases (credit)."),
        ("The owners investing cash in the company would result in which of the following?", "A debit to cash and a credit to contributed capital.",
         ["A debit to contributed capital and a credit to cash.", "A debit to cash and a credit to bank loan.", "A debit to both cash and contributed capital."],
         "Cash increases (debit) and the owners' capital increases (credit)."),
        ("Repaying a bank loan from cash would result in which of the following?", "A debit to bank loan and a credit to cash.",
         ["A debit to cash and a credit to bank loan.", "A debit to bank loan and a credit to contributed capital.", "A credit to both bank loan and cash."],
         "The liability decreases (debit) and cash decreases (credit)."),
    ]),
    "m2_q08": (1, [
        ("A chronological record of all transactions of a company is called a(n):", "journal", ["account", "trial balance", "balance sheet"],
         "The journal records the transactions in the order in which they happen."),
        ("A list of all account balances at a point in time, used to check that total debits equal total credits, is called a(n):", "trial balance",
         ["journal", "income statement", "ledger"], "The trial balance lists the balances of all accounts."),
        ("The collection of all the accounts of a company is called the:", "ledger", ["journal", "trial balance", "balance sheet"],
         "The ledger contains all the accounts."),
        ("The process of transferring the amounts from the journal to the accounts is called:", "posting", ["closing", "adjusting", "reconciling"],
         "Posting moves the journal entries into the ledger accounts."),
    ]),
    "m2_q09": (1, [
        ("The adjusted trial balance is prepared after:", "the adjusting entries have been posted to the ledger.",
         ["the financial statements are issued.", "the post-closing trial balance is prepared.", "the first transaction is recorded."],
         "Adjusting entries come first; the financial statements are then prepared from the adjusted trial balance."),
        ("Adjusting entries are made:", "at the end of the accounting period before the financial statements are prepared.",
         ["every time a transaction occurs.", "after the financial statements are issued.", "only at the beginning of the period."],
         "They bring the accounts up to date before the statements are prepared."),
        ("Which financial statement is prepared first?", "Income statement",
         ["Balance sheet", "Statement of changes in equity", "Statement of cash flows"],
         "The profit from the income statement is needed for the equity and then for the balance sheet."),
        ("The trial balance is used to check that:", "total debits equal total credits.",
         ["all transactions were recorded.", "assets exceed liabilities.", "revenues exceed expenses."],
         "A balanced trial balance only proves that debits equal credits."),
    ]),
    "m2_q10": (1, [
        ("Liabilities do not include:", "Accounts receivable", ["Trade payables", "Long-term bank loan", "Taxes payable"],
         "Accounts receivable are assets."),
        ("Stockholders' equity does not include:", "Bank loan",
         ["Contributed capital", "Retained earnings", "Profit after tax for the year"], "A bank loan is a liability."),
        ("Current assets do not include:", "Machinery", ["Inventories", "Accounts receivable", "Cash in bank"],
         "Machinery is a non-current (fixed) tangible asset."),
        ("Non-current assets do not include:", "Raw materials", ["Patents", "Buildings", "Long-term loans given"],
         "Raw materials are inventories, which are current assets."),
    ]),
    "m2_q11": (1, [
        ("The operating profit is increased by:", "Gain on sale of equipment",
         ["Interest received on loans granted", "Gain on sale of bonds", "Dividends received"],
         "The gain on the sale of equipment is other income; the other items are financial income."),
        ("The profit from financial transactions is decreased by:", "Interest paid on loans",
         ["Paid default interest", "Loss on sale of equipment", "Depreciation expense"],
         "Interest paid on loans is a financial expense; the other items are operating expenses."),
        ("The operating profit is decreased by:", "Depreciation expense",
         ["Interest paid on loans", "Loss on sale of bonds", "Interest received on deposits"],
         "Depreciation is an operating expense; the other items belong to the financial transactions."),
        ("The profit from financial transactions is increased by:", "Interest received on long-term loans granted",
         ["Gain on sale of equipment", "Received default interest", "Capitalised own output"],
         "Interest received is financial income; the other items belong to the operating profit."),
    ]),
    "m2_q12": (1, [
        ("Which of the following slows down the collection of receivables?", "Extending payment deadlines",
         ["Offering early payment discounts", "Sending reminders to overdue customers", "Requiring advance payments"],
         "A longer payment deadline means the customers pay later."),
        ("Which of the following measures reduces the risk of bad debts?", "Checking the creditworthiness of customers before selling on credit",
         ["Extending payment deadlines", "Selling to all customers on credit without limits", "Issuing invoices as late as possible"],
         "Checking the customers keeps unreliable payers away."),
        ("Why does a company grant a cash discount to its customers?", "To encourage customers to pay earlier",
         ["To increase the sales price", "To delay the delivery", "To reduce the taxes payable"],
         "The discount is the reward for early payment."),
        ("Which action speeds up the cash inflow from customers?", "Requiring advance payments from customers",
         ["Extending the payment deadline", "Issuing invoices later", "Selling more on credit"],
         "An advance brings the money in before the delivery."),
    ]),
    "m2_q13": (1, [
        ("What does a cash budget show?", "Planned cash inflows and outflows for a future period",
         ["The profit of the previous year", "The value of non-current assets", "The amount of tax payable to the authorities"],
         "A cash budget plans the cash movements."),
        ("Which of the following is a cash inflow in a cash budget?", "Collection from customers",
         ["Payment of salaries", "Repayment of a bank loan", "Purchase of equipment"], "Money received from customers is an inflow."),
        ("Which of the following is NOT included in a cash budget?", "Depreciation expense",
         ["Payments to suppliers", "Cash sales", "Loan repayments"], "Depreciation is not a cash movement."),
        ("Who mainly uses a cash budget?", "Management, to plan liquidity",
         ["The tax authority, to assess VAT", "Investors, only to calculate dividends", "Auditors, to certify the balance sheet"],
         "The cash budget is an internal planning tool."),
    ]),
}


def theory_task(rng, type_id, i):
    points, bank = THEORY[type_id]
    stem, right, wrong, why = bank[i]
    options = [right] + wrong
    rng.shuffle(options)
    return dict(points=points, prompt=stem, options=options, correct=options.index(right), explanation=why)


# ---------------------------------------------------------------- balance sheet: compile from items (midterm 1, task 4)
ITEMS = {
    "intangible": ["Patent purchased by the company", "Accounting software purchased and used in the office",
                   "Trademark registered and paid for by the company", "Licence for using a recipe database"],
    "tangible": ["Machinery used in the production", "Delivery vans used for transportation", "Office building owned by the company",
                 "Display shelves and counters in the shop", "Computers and servers used by the staff"],
    "fininv": ["Loan given for a 4-year maturity", "Shares of a partner company held for the long term", "Bonds bought with a 6-year maturity"],
    "inventories": ["Raw materials for production", "Finished goods waiting for sale", "Goods bought for resale",
                    "Packaging materials in the warehouse", "Work in progress on the shop floor"],
    "receivables": ["Amount not yet collected for goods sold to a customer", "Unpaid invoices issued to retail partners",
                    "Receivable from a customer for services provided"],
    "securities": ["Treasury bills bought as a short-term investment", "Shares of a listed company bought for trading"],
    "liquid": ["Cash in the company's bank account", "Cash held in the till", "Money on the settlement deposit account"],
    "prepaid": ["Insurance fee paid in advance for the next period", "Rent for the next quarter paid in advance"],
    "contributed": ["Capital provided to the company by the owners", "Share capital paid in by the founders"],
    "retained": ["Accumulated profit from years before the current year", "Profits retained from earlier years"],
    "profit": ["Profit after tax of the current year", "After-tax profit earned this year"],
    "provisions": ["Provision for expected warranty claims", "Provision for a pending lawsuit"],
    "longterm": ["Bank loan borrowed for 8 years", "Investment loan to be repaid in 6 years", "Development loan borrowed for 12 years"],
    "current": ["Debt due to purchase of goods (45 days payment deadline)", "VAT payable to the tax authority",
                "Salaries owed to employees for the last month", "Short-term loan to be repaid within 6 months",
                "Social security contributions payable"],
    "deferred": ["Rent received in advance for the next period", "Subscription fees received for the next period"],
}
RANGES = {"intangible": (30, 120), "tangible": (120, 320), "fininv": (40, 120), "inventories": (60, 200), "receivables": (60, 180),
          "securities": (30, 100), "liquid": (50, 200), "prepaid": (10, 40), "contributed": (200, 400), "profit": (40, 120),
          "provisions": (15, 50), "longterm": (100, 250), "current": (40, 150), "deferred": (10, 40)}
BUSINESSES = [("a bakery", "Goldcrust Bakery"), ("a furniture manufacturer", "Oakline Furniture"), ("a pharmacy", "GreenCross Pharmacy"),
              ("a printing house", "Bluepoint Printing"), ("a toy manufacturer", "Little Fox Toys"), ("a dairy farm", "Sunmeadow Dairy"),
              ("a bicycle shop", "Spoke & Chain"), ("a hotel", "Harbor View Hotel"), ("a car repair shop", "Torque Garage"),
              ("a clothing manufacturer", "Linen Lane Apparel")]


def m1_q04(rng):
    chosen = ["tangible", "inventories", "liquid", "contributed", "retained", "profit", "longterm", "current"]
    chosen += rng.sample(["intangible", "fininv", "receivables", "securities", "prepaid", "provisions", "deferred"], 3)
    items, used = [], {}   # (key, name, amount)

    def add(key, amount=None):
        pool = [n for n in ITEMS[key] if n not in used.get(key, [])]
        name = rng.choice(pool)
        used.setdefault(key, []).append(name)
        lo, hi = RANGES.get(key, (0, 0))
        items.append([key, name, amount if amount is not None else 5 * rng.randint(lo // 5, hi // 5)])

    for key in chosen:
        if key != "retained":
            add(key)
    for key in rng.sample(["current", "tangible", "inventories", "longterm", "liquid"], 3):
        add(key)
    assets = sum(a for k, _, a in items if k in ASSET_KEYS)
    others = sum(a for k, _, a in items if k not in ASSET_KEYS)
    retained = assets - others
    if not 20 <= retained <= 250:
        return None
    add("retained", retained)
    rng.shuffle(items)

    sums = {k: sum(a for key, _, a in items if key == k) for k in ROWS}
    industry, company = rng.choice(BUSINESSES)
    description = COMPILE_DESCRIPTION.format(industry=industry)
    parts = [f"{ROWS[k]}: " + " + ".join(str(a) for key, _, a in items if key == k) + f" = {sums[k]}." for k in ROWS if sums[k]]
    raw = {
        "task_title": "Compile the Balance Sheet", "company_name": company, "unit": "thousand USD", "columns": ["Amount"],
        "description": description,
        "accounts": [{"name": n, "amount": a, "row": ROWS[k]} for k, n, a in items],
        "sides": sheet_sides(sums),
        "hint": "Decide for every item whether it is a non-current asset, a current asset, equity or a liability (long-term or current), then add up the items of the same line. A line without items is 0.",
        "explanation": " ".join(parts) + f" Total assets = total equity and liabilities = {assets}.",
    }
    exam.normalize_balance_sheet(raw, "compile")       # same checks as the website: items add up, sheet balances, enough input rows
    return dict(points=8, variant="compile", **raw)


# ---------------------------------------------------------------- income statement (midterm 1, task 5)
def m1_q05(rng):
    while True:
        rev = 500 * rng.randint(10, 24)
        cogs = int(round(rev * rng.choice([0.45, 0.5, 0.55]) / 100)) * 100        # trap: not used by the nature of expense method
        mat, serv = 100 * rng.randint(3, 12), 100 * rng.randint(2, 8)
        wage = 100 * rng.randint(8, 30)
        soc = int(round(wage * 0.25 / 50)) * 50
        dep = 100 * rng.randint(2, 8) if rng.random() < 0.4 else 0
        oinc = 100 * rng.randint(1, 5) if rng.random() < 0.25 else 0
        fin_exp, fin_inc = 100 * rng.randint(1, 5), 100 * rng.randint(1, 5)
        A = rev + oinc - mat - serv - wage - soc - dep
        if A >= 300:
            break
    B = fin_inc - fin_exp
    co = rng.choice(COMPANIES)
    info = [
        {"heading": "Sold goods to domestic customers", "lines": [{"label": "net selling price", "amount": rev}, {"label": "cost of goods sold", "amount": cogs}]},
        {"lines": [{"label": "Paid interest on current loans", "amount": fin_exp}]},
        {"lines": [{"label": "Interest received on long-term loans granted", "amount": fin_inc}]},
    ]
    if oinc:
        info.insert(1, {"lines": [{"label": "Default interest received from a customer", "amount": oinc}]})
    resources = [{"label": "Material cost", "amount": mat}, {"label": "Cost of services used", "amount": serv},
                 {"label": "Payroll (Cost of wages and salaries)", "amount": wage}, {"label": "Social security", "amount": soc}]
    if dep:
        resources.insert(2, {"label": "Depreciation", "amount": dep})
    info.append({"heading": "Value of the resources used in the current period according to their cost types", "lines": resources})
    rows = [
        ("I.", "Total net sales (Revenue)", rev), ("II.", "Capitalised own output", None), ("III.", "Other income", oinc or None),
        ("IV.", "Material type expenses", mat + serv), ("V.", "Employee benefits expense", wage + soc), ("VI.", "Depreciation", dep or None),
        ("VII.", "Other expenses", None), ("A", "Operating profit/loss", A), ("VIII.", "Financial income", fin_inc),
        ("IX.", "Financial expense", fin_exp), ("B", "Profit/Loss from financial transactions", B), ("C", "Profit or loss before tax", A + B),
    ]
    why = (f"Revenue {rev}; the cost of goods sold ({cogs}) is not used by the nature of expense method. Material type expenses = {mat} + {serv} = {mat + serv}; "
           f"employee benefits = {wage} + {soc} = {wage + soc}" + (f"; depreciation {dep}" if dep else "") + (f"; other income {oinc}" if oinc else "") +
           f". Operating profit = {rev}" + (f" + {oinc}" if oinc else "") + f" - {mat + serv} - {wage + soc}" + (f" - {dep}" if dep else "") + f" = {A}. "
           f"Financial result = {fin_inc} - {fin_exp} = {B}. Profit before tax = {A} + ({B}) = {A + B}.")
    return dict(
        points=4, unit="thUSD", company=f'"{co}" Ltd.',
        description=f'To compile the income statement of "{co}" Ltd., we have the following information (data in thousands of USD - thUSD):',
        information=info, instruction="Compile the Profit and Loss Statement",
        answer_format=["Enter a value without space and other punctuation! For example: 1234567",
                       "If there is a negative value, mark it this way: -1234 (No space!)", "Do not mark positive values with + sign!"],
        rows=[{"no": n, "label": l, "answer": a, "bold": n in ("A", "B", "C")} for n, l, a in rows], explanation=why)


# ---------------------------------------------------------------- impact of economic events (midterm 1, task 6)
EVENTS = {
    "default_interest_received": dict(
        text="The company received default interest of {x} th USD because of the late settlement of trade receivables from the previous period. "
             "The interest on late payments was credited to the bank account.",
        effects={"Cash at bank": 1, "Profit after tax": 1}, income="Other income"),
    "loan_repaid": dict(text="The company transferred {x} th USD of its short-term loan from the bank account.",
                        effects={"Cash at bank": -1, "Short-term loan": -1}),
    "goods_on_credit": dict(text="The company purchased goods worth {x} th USD from a supplier with a 15-day payment deadline.",
                            effects={"Goods": 1, "Trade payables": 1}),
    "receivable_settled": dict(text="A customer settled its overdue invoice of {x} th USD by bank transfer.",
                               effects={"Trade receivables": -1, "Cash at bank": 1}),
    "default_interest_paid": dict(text="The company paid default interest of {x} th USD to a supplier from its bank account because of a late payment.",
                                  effects={"Cash at bank": -1, "Profit after tax": -1}, expense="Other expense"),
    "capital_paid_in": dict(text="The owners paid an additional {x} th USD of capital into the company's bank account.",
                            effects={"Cash at bank": 1, "Subscribed capital": 1}),
    "payable_paid": dict(text="The company settled {x} th USD of its trade payables from the bank account.",
                         effects={"Trade payables": -1, "Cash at bank": -1}),
    "short_loan_received": dict(text="The company received a short-term bank loan of {x} th USD into its bank account.",
                                effects={"Cash at bank": 1, "Short-term loan": 1}),
    "goods_shortage": dict(text="During the inventory count, a shortage worth {x} th USD was identified in the goods.",
                           effects={"Goods": -1, "Profit after tax": -1}, expense="Other expense"),
}
ASSET_LINES = ["Goods", "Trade receivables", "Cash at bank"]
EL_LINES = ["Subscribed capital", "Profit after tax", "Short-term loan", "Trade payables"]
IS_OPTIONS = ["Net sales revenue", "Other income", "Financial income", "Material type expense", "Personnel type expense", "Depreciation",
              "Other expense", "Financial expense", NOT_AFFECTED]


def m1_q06(rng):
    for _ in range(500):
        keys = rng.sample(list(EVENTS), 2)
        xs = [10 * rng.randint(2, 9) for _ in keys]
        with_payables = any("Trade payables" in EVENTS[k]["effects"] for k in keys)
        g, r, c, loan = 50 * rng.randint(1, 4), 50 * rng.randint(1, 4), 50 * rng.randint(4, 8), 50 * rng.randint(2, 4)
        pay = 50 * rng.randint(1, 3) if with_payables else 0
        capital = g + r + c - loan - pay
        if capital < 100 or capital % 50:
            continue
        opening = {"Goods": g, "Trade receivables": r, "Cash at bank": c, "Subscribed capital": capital, "Profit after tax": 0, "Short-term loan": loan}
        if with_payables:
            opening["Trade payables"] = pay
        state, ok = dict(opening), True
        for k, x in zip(keys, xs):
            for line, sgn in EVENTS[k]["effects"].items():
                state[line] += sgn * x
        if all(v >= 0 for v in state.values()):
            break
    else:
        raise RuntimeError("m1_q06: no feasible opening balance")
    a_lines = [l for l in ASSET_LINES if l in opening]
    e_lines = [l for l in EL_LINES if l in opening]
    state, income_total, expense_total, events = dict(opening), 0, 0, []
    for n, (k, x) in enumerate(zip(keys, xs), start=1):
        ev = EVENTS[k]
        change = {line: ev["effects"].get(line, 0) * x for line in opening}
        for line in opening:
            state[line] += change[line]
        inc = x if "income" in ev else 0
        exp = x if "expense" in ev else 0
        income_total += inc
        expense_total += exp

        def row(line):
            affected = change[line] != 0
            given = (not affected) and (n == 2 or rng.random() < 0.5)
            return {"label": line, "change": change[line], "closing": state[line], "given": given}

        events.append({
            "no": n, "text": ev["text"].format(x=x),
            "balance_sheet": {
                "assets": [row(l) for l in a_lines], "equity_and_liabilities": [row(l) for l in e_lines],
                "total_assets": {"change": sum(change[l] for l in a_lines), "closing": sum(state[l] for l in a_lines)},
                "total_equity_and_liabilities": {"change": sum(change[l] for l in e_lines), "closing": sum(state[l] for l in e_lines)}},
            "income_statement": {
                "income": {"item": ev.get("income", NOT_AFFECTED), "change": inc, "closing": income_total},
                "expense_cost": {"item": ev.get("expense", NOT_AFFECTED), "change": exp, "closing": expense_total},
                "profit_or_loss": {"change": inc - exp, "closing": income_total - expense_total}},
            "explanation": " ".join(f"{line} {'+' if v > 0 else ''}{v}." for line, v in change.items() if v)
                           + (f" Income {inc}: {ev['income']}." if inc else "") + (f" Expense {exp}: {ev['expense']}." if exp else "")
                           + ("" if inc or exp else " Only the balance sheet changes; the profit is not affected."),
        })
    co = rng.choice(COMPANIES)
    return dict(
        points=8, company=co, unit=CUR,
        instructions=["If there is no change, indicate this with a zero: 0",
                      "In the \"closing\" column, use the minus sign ONLY to indicate negative values, written directly in front of the sum, DO NOT use a plus sign here. If the value is zero, indicate it with a zero, like this: 0",
                      "Select the impact of the events affecting the profit or loss (only where there is such) on income, costs/expenses from the drop-down list! If none are affected, select the \"Not affected by the event!\" marking and enter zero!"],
        description=f"Take note of the following information regarding the economic transactions of \"{co}\" Ltd. during the reporting period.",
        opening_balance_sheet={
            "assets": [{"label": l, "amount": opening[l]} for l in a_lines], "equity_and_liabilities": [{"label": l, "amount": opening[l]} for l in e_lines],
            "total_assets": sum(opening[l] for l in a_lines), "total_equity_and_liabilities": sum(opening[l] for l in e_lines)},
        income_statement_options=IS_OPTIONS, events=events)


# ---------------------------------------------------------------- journal (midterm 2, task 1)
ACCOUNTS = ["Bank account", "Bank loan", "Buildings", "Cash", "Contributed capital", "Cost of goods sold", "Dividends", "Equipment", "Goods",
            "Land", "Long-term bank loan", "Rent expense", "Salaries and wages expense", "Sales revenue", "Trade payables", "Trade receivables", "Vehicles"]
J_FIRST = [("Purchased land and paid cash of ${x}", "Land", "Cash"), ("Purchased equipment and paid ${x} from the bank account", "Equipment", "Bank account"),
           ("The owners invested ${x} in cash in the company", "Cash", "Contributed capital"),
           ("Borrowed ${x} from the bank (long-term loan); the money was credited to the bank account", "Bank account", "Long-term bank loan"),
           ("Purchased goods for ${x} on credit (payment term: 30 days)", "Goods", "Trade payables"),
           ("Purchased a vehicle and paid ${x} by bank transfer", "Vehicles", "Bank account")]
J_SECOND = [("Declared and paid ${x} in dividends to stockholders by money transfer", "Dividends", "Bank account"),
            ("Paid ${x} salaries to the employees in cash", "Salaries and wages expense", "Cash"),
            ("Paid ${x} of trade payables by bank transfer", "Trade payables", "Bank account"),
            ("Paid rent of ${x} in cash", "Rent expense", "Cash"),
            ("Repaid ${x} of the long-term bank loan from the bank account", "Long-term bank loan", "Bank account")]
J_SALE = [("Goods sold for ${x} in cash.", "Cash", "Sales revenue"), ("Goods sold on credit for ${x}.", "Trade receivables", "Sales revenue"),
          ("Goods sold for ${x}; the customer paid by bank transfer.", "Bank account", "Sales revenue")]


def m2_q01(rng):
    first, second, sale = rng.choice(J_FIRST), rng.choice(J_SECOND), rng.choice(J_SALE)
    amounts = [100 * rng.randint(4, 12), 100 * rng.randint(3, 10)]
    price = 100 * rng.randint(4, 12)
    cogs = 50 * rng.randint(2, int(price / 50) - 1)
    entries = [(first, amounts[0]), (second, amounts[1]), (sale, price),
               (("The cost of goods sold is ${x}.", "Cost of goods sold", "Goods"), cogs)]
    return dict(
        points=8, description="Required: Record the given transactions in the journal!",
        account_options=ACCOUNTS,
        entries=[{"no": i, "text": t.replace("${x}", f"${amt}"), "debit": d, "credit": c, "amount": amt}
                 for i, ((t, d, c), amt) in enumerate(entries, start=1)],
        explanation=" ".join(f"{i}. Debit {d}, credit {c}, {amt}." for i, ((_, d, c), amt) in enumerate(entries, start=1)))


# ---------------------------------------------------------------- ledger accounts -> trial balance -> closing balance sheet (midterm 2, task 2)
def m2_q02(rng):
    cash_name = rng.choice(["Cash", "Bank account"])
    for _ in range(1000):
        T, G, C = 10 * rng.randint(15, 40), 10 * rng.randint(10, 30), 10 * rng.randint(8, 25)
        K, L = 10 * rng.randint(20, 40), 10 * rng.randint(10, 25)
        serv, wages, profit = 10 * rng.randint(3, 9), 10 * rng.randint(4, 12), 10 * rng.randint(1, 4)
        P = T + G + C - K - L - profit
        if P >= 30:
            break
    revenue = serv + wages + profit
    spec = [("Other equipment", "debit", T, True), ("Goods", "debit", G, True), (cash_name, "debit", C, False),
            ("Contributed capital", "credit", K, False), ("Long-term loans", "credit", L, True), ("Trade payables", "credit", P, True),
            ("Cost of services used", "debit", serv, False), ("Cost of salaries and wages", "debit", wages, False),
            ("Sales revenue", "credit", revenue, False)]
    asked = set(rng.sample([n for n, _, _, busy in spec if busy and n != "Other equipment"], 2))
    ledger, trial = [], []
    for name, side, balance, busy in spec:
        other = [10 * rng.randint(2, 15) for _ in range(rng.choice([1, 2]))] if busy else []
        main = split_amount(rng, balance + sum(other), rng.choice([1, 2, 3]) if busy else 1)
        debit, credit = (main, other) if side == "debit" else (other, main)
        assert sum(debit) - sum(credit) == (balance if side == "debit" else -balance)
        ledger.append({"account": name, "debit": debit, "credit": credit})
        trial.append({"account": name, "debit": balance if side == "debit" else 0, "credit": balance if side == "credit" else 0, "asked": name in asked})
    bs_net = (T + G + C) - (K + L + P)
    return dict(
        points=8, unit=CUR,
        description="Based on the data of the given ledger accounts, determine the missing values in the trial balance, and prepare the Closing Balance Sheet!",
        ledger=ledger, trial_balance=trial,
        trial_balance_totals={"balance_sheet_accounts_total": {"debit": bs_net, "credit": 0}, "profit_and_loss_accounts_total": {"debit": 0, "credit": profit}},
        closing_balance_sheet={
            "assets": [{"label": "Tangible assets", "answer": T}, {"label": "Supplies", "answer": G},
                       {"label": "Cash", "answer": C if cash_name == "Cash" else 0},
                       {"label": "Cash at bank", "answer": C if cash_name == "Bank account" else 0}, {"label": "Total Assets", "answer": T + G + C}],
            "equity_and_liabilities": [{"label": "Contributed capital", "answer": K}, {"label": "Retained earnings", "answer": 0},
                                       {"label": "Net Income", "answer": profit}, {"label": "Liabilities", "answer": L + P},
                                       {"label": "Total Owner's Equity and Liabilities", "answer": K + profit + L + P}]},
        explanation=f"Every balance is the difference of the two sides. Net income = revenue {revenue} - costs {serv + wages} = {profit}. "
                    f"Assets {T + G + C} = capital {K} + net income {profit} + liabilities ({L} + {P}) = {K + profit + L + P}.")


# ---------------------------------------------------------------- recording events in ledger accounts (midterm 2, task 3)
def m2_q03(rng):
    while True:
        goods0, bank0 = 100 * rng.randint(3, 8), 100 * rng.randint(4, 9)
        cost, price = 10 * rng.randint(15, 40), 0
        price = cost + 10 * rng.randint(10, 40)
        buy, pct = 10 * rng.randint(20, 50), rng.choice([5, 10, 20])
        disc = buy * pct // 100
        if buy * pct % 100 == 0 and cost <= goods0 and buy - disc <= bank0:
            break
    rest = buy - disc
    days = rng.choice([10, 15, 20, 30])
    events = [
        f"The company sold inventory worth {cost} thousand USD to its domestic partner for {price} thousand USD in cash.",
        f"Our company purchased inventory worth {buy} thousand USD. Payment for the inventory is due in {days} days.",
        f"Our company complained about the quality of the goods upon receipt, so it subsequently requested and received a {pct}% discount. The invoice was corrected.",
        "Our company has transferred its entire trade payables from the settlement deposit account. The settlement deposit account debit notification has been received.",
    ]
    e = lambda event, side, amount: {"event": event, "side": side, "amount": amount}      # noqa: E731
    accounts = [
        {"name": "Goods", "opening": {"side": "debit", "amount": goods0}, "entries": [e(1, "credit", cost), e(2, "debit", buy), e(3, "credit", disc)]},
        {"name": "Trade payables", "opening": None, "entries": [e(2, "credit", buy), e(3, "debit", disc), e(4, "debit", rest)]},
        {"name": "Cash", "opening": None, "entries": [e(1, "debit", price)]},
        {"name": "Bank account", "opening": {"side": "debit", "amount": bank0}, "entries": [e(4, "credit", rest)]},
        {"name": "Cost of goods sold", "opening": None, "entries": [e(1, "debit", cost)]},
        {"name": "Sales revenue", "opening": None, "entries": [e(1, "credit", price)]},
    ]
    return dict(
        points=6, unit="thousand USD", economic_events=events,
        task="Record economic events in thousands of USD! Answer the questions! Only write the amount on the appropriate side of the ledger accounts, NOT the event number!",
        accounts=accounts,
        questions=[{"prompt": "What is the Gross Profit from Sales?", "format": "for a profit: +1234, for a loss: -1234", "answer": price - cost},
                   {"prompt": "What is the merchandise account balance at the end of the period?", "format": "only the amount, without spaces or other punctuation marks",
                    "answer": goods0 - cost + buy - disc}],
        explanation=f"Gross profit = sales revenue {price} - cost of goods sold {cost} = {price - cost}. Goods: {goods0} - {cost} + {buy} - {disc} = {goods0 - cost + buy - disc}. "
                    f"The discount is {pct}% of {buy} = {disc}; the remaining payable {buy} - {disc} = {rest} is paid from the bank account.")


# ---------------------------------------------------------------- the two files
TYPES = {
    "midterm1": [
        ("m1_q01", "Accounting equation: the missing amount", "mcq", m1_q01, 5),
        ("m1_q02", "Operating profit from a short list of items", "mcq", m1_q02, 5),
        ("m1_q03", "Balance sheet value of purchased materials after a return", "mcq", m1_q03, 5),
        ("m1_q04", "Compile the balance sheet from a list of items", "balance_sheet", m1_q04, 5),
        ("m1_q05", "Compile the income statement (nature of expense method)", "income_statement", m1_q05, 4),
        ("m1_q06", "Impact of two economic events on the balance sheet and the income statement", "event_impact", m1_q06, 4),
        ("m1_q07", "Which statements show the item", "mcq", None, 4),
        ("m1_q08", "Types of business organisations", "mcq", None, 4),
        ("m1_q09", "Income statement formulas", "mcq", None, 4),
        ("m1_q10", "Main source of revenue", "mcq", None, 4),
        ("m1_q11", "Select the incorrect statement about an economic event", "mcq", None, 4),
        ("m1_q12", "Complex economic event", "mcq", None, 4),
    ],
    "midterm2": [
        ("m2_q01", "Record transactions in the journal", "journal", m2_q01, 4),
        ("m2_q02", "Ledger accounts, trial balance and closing balance sheet", "trial_balance", m2_q02, 3),
        ("m2_q03", "Record economic events in ledger accounts, gross profit and merchandise balance", "ledger_posting", m2_q03, 4),
        ("m2_q04", "Credit (debit) turnover of Accounts receivable", "mcq", m2_q04, 5),
        ("m2_q05", "Operating profit of a manufacturer (financial items left out)", "mcq", m2_q05, 5),
        ("m2_q06", "Gross profit of a sale with an advance", "mcq", m2_q06, 5),
        ("m2_q07", "Debit and credit of a transaction", "mcq", None, 4),
        ("m2_q08", "Accounting records: definitions", "mcq", None, 4),
        ("m2_q09", "Trial balance and financial statements", "mcq", None, 4),
        ("m2_q10", "Classification: what does not belong", "mcq", None, 4),
        ("m2_q11", "What changes the operating profit", "mcq", None, 4),
        ("m2_q12", "Collection of receivables", "mcq", None, 4),
        ("m2_q13", "Cash budget", "mcq", None, 4),
    ],
}
NOTES = {
    "midterm1": "Practice tasks modelled on midterm 1. Tables: balance_sheet (same format as exam.normalize_balance_sheet, variant compile), income_statement, event_impact. mcq = exam.normalize_question format.",
    "midterm2": "Practice tasks modelled on midterm 2. Tables: journal, trial_balance, ledger_posting. mcq = exam.normalize_question format.",
}


def build(midterm: str, rng_seed: int) -> dict:
    out = []
    for type_id, title, kind, gen, count in TYPES[midterm]:
        rng = random.Random(f"{rng_seed}-{type_id}")
        tasks, seen, tries = [], set(), 0
        while len(tasks) < count:
            tries += 1
            if tries > 500:
                raise RuntimeError(f"{type_id}: could not make {count} different tasks")
            i = len(tasks)
            body = gen(rng) if gen else theory_task(rng, type_id, i)
            if body is None:
                continue
            key = json.dumps(body, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            task = {"id": f"{type_id}_{len(tasks) + 1:02d}", "type_id": type_id, "kind": kind}
            task.update(body)
            if kind == "mcq":
                assert len(task["prompt"]) <= 500, (task["id"], len(task["prompt"]))
                exam.normalize_question({k: task[k] for k in ("kind", "points", "prompt", "options", "correct", "explanation", "calc") if k in task}, 1)
            tasks.append(task)
        out.append({"type_id": type_id, "title": title, "kind": kind, "tasks": tasks})
    return {"source": midterm, "note": NOTES[midterm], "seed": rng_seed, "currency": CUR, "types": out}


def main():
    for midterm in TYPES:
        data = build(midterm, SEED)
        path = ROOT / f"{midterm}_tasks.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{path.name}: " + ", ".join(f"{t['type_id']}x{len(t['tasks'])}" for t in data["types"]))


if __name__ == "__main__":
    main()
