"""The real midterm tasks (exemplars) and the rules for making identical new ones.

generator.py shows examples_for(topic) to the model after its system prompt, so the AI imitates these tasks: same question types,
same wording, same logic, same kind of wrong options. scripts/build_midterm_tasks.py makes midterm1_tasks.json / midterm2_tasks.json from the same types.

To teach the AI one more task: add a dict to EXEMPLARS (copy a similar one) and run `python midterm_templates.py` to check it.
Exemplars with kind "mcq" and "balance_sheet" are rendered by the website today and are shown to the model; the other kinds
(income_statement, event_impact, journal, trial_balance, ledger_posting) need a table renderer first (see todotask.txt) and are kept here as the specification.
Keep this file out of public repositories: it contains real exam tasks.
"""
import json
import random
import re

# ---------------------------------------------------------------- balance sheet captions (the real midterm layout)
ROWS = {  # key: caption exactly as printed in the exam
    "intangible": "I. Intangible assets", "tangible": "II. Tangible assets", "fininv": "III. Financial investments",
    "inventories": "I. Inventories", "receivables": "II. Accounts receivables", "securities": "III. Securities", "liquid": "IV. Liquid assets",
    "prepaid": "C. Prepaid expenses and accrued income",
    "contributed": "I. Contributed capital", "retained": "IV. Retained earnings", "profit": "VII. Profit after tax for the year",
    "provisions": "E. Provisions", "longterm": "II. Long term liabilities", "current": "III. Current liabilities",
    "deferred": "G. Accrued expenses and deferred income",
}
ASSET_KEYS = {"intangible", "tangible", "fininv", "inventories", "receivables", "securities", "liquid", "prepaid"}
GROUPS = [  # (side, group title, caption keys, subtotal label); a group without a title is a single caption line
    (0, "A. Non-current assets", ["intangible", "tangible", "fininv"], "Total non-current assets"),
    (0, "B. Current assets", ["inventories", "receivables", "securities", "liquid"], "Total current assets"),
    (0, "", ["prepaid"], None),
    (1, "D. Stockholders' equity", ["contributed", "retained", "profit"], "Total stockholders' equity"),
    (1, "", ["provisions"], None),
    (1, "F. Liabilities", ["longterm", "current"], "Total liabilities"),
    (1, "", ["deferred"], None),
]


def sheet_sides(sums: dict | None = None) -> list[dict]:
    """The two-sided table of a 'compile' task. With sums every row carries its value; without, only the captions (what the model writes)."""
    sides = [{"title": "Assets", "groups": [], "total": {"label": "TOTAL ASSETS", "mode": "hidden"}},
             {"title": "Equity and liabilities", "groups": [], "total": {"label": "TOTAL EQUITY AND LIABILITIES", "mode": "hidden"}}]
    for side, title, keys, subtotal in GROUPS:
        rows = [{"label": ROWS[k], "blank": True, **({"values": [sums.get(k, 0)]} if sums is not None else {})} for k in keys]
        group = {"title": title, "rows": rows}
        if subtotal:
            group["subtotal"] = {"label": subtotal, "mode": "hidden"}
        sides[side]["groups"].append(group)
    return sides


COMPILE_DESCRIPTION = (
    "About the balance sheet value items of {industry} firm you know the following:\n"
    "Task: Based on the data provided, compile the company's balance sheet for 31 December 20XX! You don't have to summarize, "
    "but each balance sheet group can include multiple items.\n"
    "Enter a value without space and other punctuation! For example: 1234\n"
    "If there is a negative value, mark it this way: -100 (No space!)\n"
    "If the amount is zero, mark it this way: 0")

# (key, item text, amount): midterm 1, task 4 (a bakery). Assets 880 = equity and liabilities 880.
COMPILE_ITEMS = [
    ("current", "Debt due to purchase of raw materials (30 days payment deadline)", 60),
    ("tangible", "Production machinery used in production", 260),
    ("retained", "Accumulated profit from years prior to the current year", 115),
    ("fininv", "Loan given for a 3-year maturity", 90),
    ("receivables", "Consideration not yet collected for finished goods sold to a partner", 155),
    ("liquid", "Cash in bank account", 180),
    ("intangible", "Invoicing and inventory software purchased by the company", 75),
    ("profit", "Profit after tax in the current year", 95),
    ("longterm", "Development loan borrowed for 10 years", 170),
    ("contributed", "Capital provided to the company by the founders", 300),
    ("inventories", "Raw material for production", 120),
    ("current", "VAT debt", 140),
]


def _compile_raw(items, industry: str, company: str, with_plug: bool) -> dict:
    """with_plug=True is what the model writes (the retained earnings item has no amount: the website calculates it)."""
    sums = {}
    for key, _, amount in items:
        sums[key] = sums.get(key, 0) + amount
    accounts = [({"name": n, "row": ROWS[k], "plug": True} if with_plug and k == "retained" else {"name": n, "amount": a, "row": ROWS[k]}) for k, n, a in items]
    return {"task_title": "Compile the Balance Sheet", "company_name": company, "unit": "thousand USD", "columns": ["Amount"],
            "description": COMPILE_DESCRIPTION.format(industry=industry), "accounts": accounts,
            "sides": sheet_sides(None if with_plug else sums),
            "hint": "Decide for every item whether it is a non-current asset, a current asset, equity or a liability (long-term or current), then add up the items of the same line. A line without items is 0.",
            "explanation": "Add up the items of every line: " + " ".join(f"{ROWS[k]}: {sums[k]}." for k in ROWS if k in sums) + " Total assets = total equity and liabilities = 880."}


# ---------------------------------------------------------------- the exemplars
def _q(prompt, options, correct, points=1, calc=None, explanation=""):
    q = {"kind": "mcq", "points": points, "prompt": prompt, "options": options, "correct": correct}
    if calc:
        q["calc"] = calc
    q["explanation"] = explanation
    return q


EXEMPLARS = [
    # ---------------------------------------------------------------- MIDTERM 1
    dict(id="m1_q01", midterm=1, number=1, points=2, kind="mcq", title="Accounting equation: the missing amount",
         question=_q("Use the accounting equation to calculate the missing amount!\nAt the end of the current accounting period, account balances were as follows: "
                     "Cash: 5,000$; Tangible assets: 3,000$; Retained earnings: -1,000$; Long-term liabilities: 6,500$. Contributed capital for the period was:",
                     ["5500", "2500", "3500", "4500"], 1, 2, "5000+3000-6500-(-1000)",
                     "Assets 8,000 = liabilities 6,500 + equity 1,500; equity = contributed capital + retained earnings, so contributed capital = 1,500 - (-1,000) = 2,500."),
         keep="Assets, liabilities and equity items are given with their balances; ONE is missing (usually contributed capital; also long-term liabilities or cash). "
              "Retained earnings may be negative. Options are plain numbers, 1,000 or 500 apart.",
         vary="Item values, which item is missing, whether inventories are also given, the sign of retained earnings.",
         distractors="Wrong sign of retained earnings; retained earnings forgotten; neighbouring values."),
    dict(id="m1_q02", midterm=1, number=2, points=2, kind="mcq", title="Operating profit from a short list of items",
         question=_q("The company sells its product for 60.000 Ft, used raw materials 5.000 Ft, salaries and wages expense 10.000 Ft, depreciation expense 1.000 Ft and other income 2.000 Ft. The operating profit:",
                     ["46.000 Ft", "44.000 Ft", "62.000 Ft", "16.000 Ft"], 0, 2, "60000-5000-10000-1000+2000",
                     "Operating profit = revenue - materials - personnel - depreciation + other income = 46,000."),
         keep="One sentence with revenue, material, personnel, depreciation expenses and ONE more item (other income, other expenses or capitalised own output). Financial items are never included.",
         vary="Amounts, the last item.", distractors="The last item forgotten; revenue + the last item; the sum of the costs."),
    dict(id="m1_q03", midterm=1, number=3, points=2, kind="mcq", title="Balance sheet value of purchased materials after a return",
         question=_q("The following economic events occurred at the company during the reporting period.\nThe company purchased raw materials worth 1,000 Th HUF with an 8-day payment term.\n"
                     "Due to quality complaints, 10% of the materials were returned to the supplier.\nDetermine the balance sheet value of the raw materials after these events.",
                     ["900 Th HUF", "1 010 Th HUF", "1 100 Th HUF", "1 000 Th HUF"], 0, 2, "1000-1000*0.1",
                     "The purchase on credit adds 1,000; the 10% return takes 100 back: 900. The payment term does not matter."),
         keep="Two events (purchase on credit with a payment term, then a percentage returned). Options use a space as the thousands separator.",
         vary="Item, amount, term, percentage, reason of the return.", distractors="No return (original value); the return added instead of subtracted; the return deducted twice."),
    dict(id="m1_q04", midterm=1, number=4, points=8, kind="balance_sheet", title="Compile the balance sheet from a list of items",
         raw=_compile_raw(COMPILE_ITEMS, "a bakery", "Oven & Rye Bakery", with_plug=True),
         check_raw=_compile_raw(COMPILE_ITEMS, "a bakery", "Oven & Rye Bakery", with_plug=False),
         keep="The table always lists ALL 15 captions (A-G as printed below); captions without items are 0 (3 or 4 of them). 12 items written descriptively, several items on the same line "
              "(2-3 lines combine 2 items), only the retained earnings item is the plug (no amount, the website calculates it). Totals and subtotals are hidden. Amounts are small whole numbers (60-300) in thousand USD.",
         vary="Industry, company, which captions are empty, items and amounts (assets total about 800-1,000).",
         distractors="Items that look alike but belong to different lines: loan GIVEN (financial investment) vs loan BORROWED (liability), long-term (10 years) vs current (30 days) debts, "
                     "'not yet collected' (receivable) vs 'cash'."),
    dict(id="m1_q05", midterm=1, number=5, points=4, kind="income_statement", title="Compile the income statement (nature of expense method)",
         statement="To compile the income statement of \"BSc\" Ltd., we have the following information (data in thousands of HUF - thHUF):\n"
                   "Sold goods to domestic customers: net selling price 8000, cost of goods sold 4000. Paid interest on current loans 200. Interest received on long-term loans granted 300. "
                   "Value of the resources used in the current period according to their cost types: Material cost 600, Cost of services used 400, Payroll (cost of wages and salaries) 2000, Social security 500.\n"
                   "Instruction: Compile the Profit and Loss Statement. Enter a value without space and other punctuation! If there is a negative value, mark it this way: -1234 (no space!). Do not mark positive values with + sign!\n"
                   "Table rows: I. Total net sales (Revenue) | II. Capitalised own output | III. Other income | IV. Material type expenses | V. Employee benefits expense | VI. Depreciation | VII. Other expenses | "
                   "A Operating profit/loss | VIII. Financial income | IX. Financial expense | B Profit/Loss from financial transactions | C Profit or loss before tax. "
                   "Answer cells: I, IV, V, A, VIII, IX, B, C (rows without data have no answer cell).",
         solution="I 8000; IV 600+400=1000; V 2000+500=2500; A 8000-1000-2500=4500; VIII 300; IX 200; B 100; C 4600. The cost of goods sold (4000) is NOT used by the nature of expense method.",
         keep="Same row list and instruction texts. The data contain ONE trap (cost of goods sold). Material type = material cost + services; employee benefits = payroll + social security. Interest items are financial.",
         vary="Amounts, optional extra lines (depreciation, other income) which add an answer cell, a negative financial result."),
    dict(id="m1_q06", midterm=1, number=6, points=8, kind="event_impact", title="Impact of two economic events on the balance sheet and the income statement",
         statement="Opening balance sheet: Assets: Goods 100, Trade receivables 100, Cash at bank 300 (total 500). Equity and liabilities: Subscribed capital 350, Profit after tax 0, Short-term loan 150 (total 500).\n"
                   "Instructions: if there is no change write 0; in the 'closing' column use the minus sign only for negative values, never a plus; select the impact on income / costs-expenses from the drop-down list, "
                   "if none are affected select 'Not affected by the event!' and enter zero.\n"
                   "Event 1: The company received default interest of 30 th HUF because of the late settlement of trade receivables from the previous period. The interest on late payments was credited to the bank account.\n"
                   "Event 2: The company transferred 70 th HUF of its short-term loan from the bank account.\n"
                   "For each event: a balance sheet table (every line: change, closing; totals) and an income statement table (Income, Expense/cost with a drop-down, Profit or loss: change, closing).",
         solution="Event 1: Cash +30 -> 330; Profit after tax +30 -> 30; totals +30 -> 530 / 530. Income statement: Income (other income) +30 -> 30; Expense/cost Not affected 0 -> 0; Profit +30 -> 30. "
                  "Event 2: Cash -70 -> 260; Short-term loan -70 -> 80; totals -70 -> 460 / 460; no effect on the profit.",
         keep="Two events on the same opening balance sheet; the closing values of event 1 are the base of event 2. Unaffected lines of event 2 are given, affected ones and the totals are answers. Closing values are cumulative.",
         vary="Events from: default interest received/paid (other income/other expense), loan repaid or received, goods bought on credit, receivable settled, payable paid, capital paid in, inventory shortage."),
    dict(id="m1_q07", midterm=1, number=7, points=2, kind="mcq", title="Which statements show the item",
         question=_q("The ending balance in Retained Earnings appears on the:",
                     ["Income Statement and Statement of Cash Flows.", "Balance Sheet only.", "Statement of Changes in Equity only.", "Balance Sheet and Statement of Changes in Equity."], 3, 2,
                     None, "Retained earnings is an equity item: the balance sheet shows its ending balance and the statement of changes in equity its movement."),
         keep="'The ending balance in X appears on the:' with four combinations of the financial statements.", vary="The item (cash, receivables, contributed capital, net income, revenue)."),
    dict(id="m1_q08", midterm=1, number=8, points=2, kind="mcq", title="Types of business organisations",
         question=_q("A limited-liability partnership is made up of:", ["members.", "sole proprietors.", "shareholders."], 0, 2, None, "The owners of a limited-liability partnership are its members."),
         keep="'X is made up of / owned by:' with three or four kinds of owners.", vary="The form of business."),
    dict(id="m1_q09", midterm=1, number=9, points=1, kind="mcq", title="Income statement formulas",
         question=_q("Under the nature of expense method, operating profit/loss is compiled using:",
                     ["Net sales revenue - material type expenses - personnel type expenses - depreciation + other income - other expenses",
                      "Net sales revenue + capitalised own output - direct cost of sales - indirect cost of sales + other income - other expenses",
                      "Net sales revenue + capitalised own output + other income - material type expenses - personnel type expenses - depreciation - other expenses",
                      "Net sales revenue - direct cost of sales - indirect cost of sales + other income - other expenses", "Financial income - financial expenses"], 2, 1, None,
                     "The nature of expense method groups the costs by type and includes the capitalised own output."),
         keep="Long formula options built from the same pieces; the wrong ones mix the nature and the function of expense methods or leave a piece out.", vary="Method, which result line (operating, financial, before tax)."),
    dict(id="m1_q10", midterm=1, number=10, points=1, kind="mcq", title="Main source of revenue",
         question=_q("For a manufacturer, revenue comes mainly from selling:", ["Bank services", "Own production inventories/products", "Leased assets", "Taxes"], 1, 1, None,
                     "A manufacturer sells what it produces."),
         keep="'For a X, revenue comes mainly from selling/...:' with one right option.", vary="The kind of business."),
    dict(id="m1_q11", midterm=1, number=11, points=1, kind="mcq", title="Select the incorrect statement about an economic event",
         question=_q("The company purchased raw materials with later payment (not for cash). Select the incorrect statement!",
                     ["As a result of the economic event, assets and liabilities increased to the same extent.", "The economic event does not affect the profit or loss account.",
                      "Due to economic events, the company's trade payables increased.", "As a result of economic events, the company's balance sheet total decreased."], 3, 1, None,
                     "Assets and liabilities both increase, so the balance sheet total increases; the statement that it decreased is incorrect."),
         keep="One simple event, four statements, exactly ONE incorrect (the question asks for the incorrect one).", vary="The event."),
    dict(id="m1_q12", midterm=1, number=12, points=1, kind="mcq", title="Complex economic event",
         question=_q("From the following economic events, select which one qualifies as a complex economic event!",
                     ["The company transferred the due installment of its long-term loan.", "Transfer of after-tax profit to retained earnings.",
                      "During the inventory count, a shortage was identified in the goods.",
                      "The receivable related to the sale of goods has been settled. The sales price of the goods has been credited to the bank account."], 2, 1, None,
                     "ASSUMED KEY (the screenshot has no answer): a complex event affects both the balance sheet and the income statement; the shortage is an expense."),
         keep="Four events, one of them changes the balance sheet AND the profit (assumed definition of 'complex').", vary="The events."),
    # ---------------------------------------------------------------- MIDTERM 2
    dict(id="m2_q01", midterm=2, number=1, points=8, kind="journal", title="Record transactions in the journal",
         statement="Required: Record the given transactions in the journal! (General journal: Ref. nr., Transaction explanation, Record: Debit / Credit chosen from drop-down lists, Amount: Debit / Credit given.)\n"
                   "1. Purchased land and paid cash of $900. 2. Declared and paid $800 in dividends to stockholders by money transfer. 3. Goods sold for $600 in cash. 4. The cost of goods sold is $200.",
         solution="1. Debit Land, credit Cash, 900. 2. Debit Dividends, credit Bank account, 800. 3. Debit Cash, credit Sales revenue, 600. 4. Debit Cost of goods sold, credit Goods, 200. "
                  "(The drop-down account names are not visible in the screenshot: assumed.)",
         keep="Four transactions, one debit account and one credit account each, the amount is given. Transaction 3 is a sale and 4 is its cost of goods sold (smaller than the sale).",
         vary="The asset purchase / financing event, the payment or distribution, cash or credit sale, amounts."),
    dict(id="m2_q02", midterm=2, number=2, points=8, kind="trial_balance", title="Ledger accounts, trial balance and closing balance sheet",
         statement="T-accounts (debit side | credit side): Other equipment 310, 200 | 100, 150. Goods 100, 260 | 40, 30, 80. Cash 150 | -. Contributed capital - | 310. Long-term loans 40 | 240. "
                   "Trade payables 200, 150, 80 | 520. Cost of services used 60 | -. Cost of salaries and wages 90 | -. Sales revenue - | 170.\n"
                   "Task: Based on the data of the given ledger accounts, determine the missing values in the trial balance, and prepare the Closing Balance Sheet! "
                   "Enter values without spaces; negative values with -; if the amount is zero write 0.\n"
                   "Trial balance (Balance: Debit | Credit) rows: Other equipment, Goods, Cash, Contributed capital, Long-term loans, Trade payables, Balance sheet accounts total, Cost of services used, Cost of salaries and wages, Sales revenue, Profit & loss accounts total. "
                   "Closing Balance Sheet 31.12.202X: Tangible assets, Supplies, Cash, Cash at bank, Total Assets | Contributed capital, Retained earnings, Net Income, Liabilities, Total Owner's Equity and Liabilities.",
         solution="Other equipment 260 D; Goods 210 D (asked); Cash 150 D; Contributed capital 310 C; Long-term loans 200 C; Trade payables 90 C (asked); Balance sheet accounts total 20 D / 0; "
                  "Cost of services 60 D; salaries 90 D; Sales revenue 170 C; Profit & loss accounts total 0 / 20 C. Closing balance sheet: Tangible assets 260; Supplies 210; Cash 150; Cash at bank 0; Total assets 620 | "
                  "Contributed capital 310; Retained earnings 0; Net income 20; Liabilities 200+90=290; Total 620.",
         keep="9 ledger accounts (equipment, goods, cash | capital, long-term loans, trade payables | two costs, sales revenue); 2 busy accounts are asked in the trial balance; the totals are net balances (balance sheet accounts net = the profit). "
              "The closing balance sheet has every cell asked, including the zeros.",
         vary="Amounts (they must balance: assets = capital + loans + payables + profit), which two accounts are asked, 'Cash' or 'Bank account'."),
    dict(id="m2_q03", midterm=2, number=3, points=6, kind="ledger_posting", title="Record economic events in ledger accounts, gross profit and merchandise balance",
         statement="Opening balances: Goods B: 400, Bank account B: 500. Events (thousand HUF): 1. The company sold inventory worth 260 to its domestic partner for 530 in cash. "
                   "2. Our company purchased inventory worth 410. Payment for the inventory is due in 20 days. 3. Our company complained about the quality of the goods upon receipt, so it subsequently requested and received a 10% discount. The invoice was corrected. "
                   "4. Our company has transferred its entire trade payables from the settlement deposit account. The settlement deposit account debit notification has been received.\n"
                   "Task: Record economic events! Only write the amount on the appropriate side of the ledger accounts (Goods, Trade payables, Cash, Bank account, Cost of goods sold, Sales revenue), NOT the event number! "
                   "Then: What is the Gross Profit from Sales? (profit +1234, loss -1234) What is the merchandise account balance at the end of the period?",
         solution="Goods: credit 260, debit 410, credit 41. Trade payables: credit 410, debit 41, debit 369. Cash: debit 530. Bank account: credit 369. Cost of goods sold: debit 260. Sales revenue: credit 530. "
                  "Gross profit = 530 - 260 = 270. Goods balance = 400 - 260 + 410 - 41 = 509.",
         keep="Always these four events in this order and these six accounts. The sale is in cash, the discount is a percentage of the purchase, the payables are paid in full from the bank account.",
         vary="Amounts (the discount must be a whole number, bank balance >= payment), percentage, payment term."),
    dict(id="m2_q04", midterm=2, number=4, points=2, kind="mcq", title="Credit (debit) turnover of Accounts receivable",
         question=_q("At FGH Company, you know the following information about the period in question:\n• Beginning balance of Accounts receivable is 1,600 Th HUF,\n"
                     "• 300 Th HUF was received into the bank account for a previous sale,\n• Inventory worth 3,000 Th HUF was invoiced for 5,000 Th HUF, with a payment deadline of 8 days,\n"
                     "• A customer paid in cash for a previous sale of 800 Th HUF.\nHow much is the credit turnover of Accounts receivable on the period-end general ledger statement?",
                     ["1,100 Th HUF", "6,600 Th HUF", "4,500 Th HUF", "5,500 Th HUF"], 0, 2, "300+800",
                     "The credit side shows the decreases: 300 + 800 = 1,100. The sale (5,000) is a debit; the closing balance is 5,500."),
         keep="Four bullet points: opening balance, collection into the bank, a sale (cost AND price are given: the price is the receivable), a cash collection. Asked: credit turnover (or debit turnover).",
         vary="Amounts, payment deadline, credit or debit turnover.", distractors="Opening balance + sale; the closing balance; the debit turnover."),
    dict(id="m2_q05", midterm=2, number=5, points=2, kind="mcq", title="Operating profit of a manufacturer (financial items left out)",
         question=_q("A manufacturing company achieved 400 thHUF in revenue during the year. Its material-type expenses were 70 thHUF, personnel-type expenses 30 thHUF, and depreciation 20 thHUF. "
                     "The company received 90 thHUF in interest, and had 40 thHUF in other expenses and 150 thHUF in other income. Determine the operating profit!",
                     ["480", "430", "390", "370"], 2, 2, "400-70-30-20-40+150", "Operating profit = 400 - 70 - 30 - 20 - 40 + 150 = 390; the interest received is a financial item."),
         keep="The financial item (interest) is in the text only as a trap; the rest is the nature of expense method.", vary="Amounts, interest received or paid.",
         distractors="Interest added to the result; other expenses ignored; depreciation counted twice."),
    dict(id="m2_q06", midterm=2, number=6, points=2, kind="mcq", title="Gross profit of a sale with an advance",
         question=_q("A business has a sales price of 600 USD, for this transaction the buyer previously gave an advance of 200 USD, and the purchase price of the goods was 350 USD. What is the business's gross profit?",
                     ["50", "-50", "450", "250"], 3, 2, "600-350", "Gross profit = sales price - purchase price = 250. The advance only decides when the money arrived."),
         keep="Gross profit = sales price - purchase price; the advance is the distractor.", vary="Amounts.", distractors="Sales price - purchase price - advance; its negative; sales price - purchase price + advance."),
    dict(id="m2_q07", midterm=2, number=7, points=1, kind="mcq", title="Debit and credit of a transaction",
         question=_q("Borrowing cash from a bank would result in which of the following?",
                     ["A debit to cash and a credit to bank loan.", "A debit to cash and a credit to contributed capital.", "A debit to bank loan and a credit to cash.", "A debit to both cash and bank loan."], 0, 1, None,
                     "Cash increases (debit) and the liability increases (credit)."),
         keep="'X would result in which of the following?' with the four debit/credit combinations of the same two accounts plus a wrong account.", vary="The transaction."),
    dict(id="m2_q08", midterm=2, number=8, points=1, kind="mcq", title="Accounting records: definitions",
         question=_q("A record of all the changes in a particular asset, liability, or shareholders' equity during a period is called a (n):", ["journal", "account", "trial balance.", "transaction"], 1, 1, None,
                     "An account records all the changes of one item."),
         keep="A definition and four basic accounting terms.", vary="The term (journal, ledger, trial balance, posting)."),
    dict(id="m2_q09", midterm=2, number=9, points=1, kind="mcq", title="Trial balance and financial statements",
         question=_q("The financial statements are prepared from the", ["unadjusted trial balance.", "adjusted trial balance.", "adjustments.", "ledger."], 1, 1, None,
                     "The statements are prepared from the adjusted trial balance."),
         keep="The accounting cycle: order of the steps.", vary="The step asked."),
    dict(id="m2_q10", midterm=2, number=10, points=1, kind="mcq", title="Classification: what does not belong",
         question=_q("Assets do not include:", ["Accounts receivable", "Property, plant, and equipment.", "Cash and cash equivalents.", "Accounts payable"], 3, 1, None, "Accounts payable is a liability."),
         keep="'X do not include:' three members of the group and one outsider.", vary="The group (assets, liabilities, equity, current assets)."),
    dict(id="m2_q11", midterm=2, number=11, points=1, kind="mcq", title="What changes the operating profit",
         question=_q("The operating profit is decreased by", ["Paid default interest", "Loss on sale of bonds", "Gain on sale of equipment", "Interest paid on loans"], 0, 1, None,
                     "Default interest paid is an other expense (operating); the loss on bonds and the interest paid are financial expenses."),
         keep="Operating items (other income/expenses, depreciation) against financial items (interest, bonds, dividends).", vary="Increased or decreased, operating or financial result."),
    dict(id="m2_q12", midterm=2, number=12, points=1, kind="mcq", title="Collection of receivables",
         question=_q("How can the collection of receivables be accelerated?", ["Offering early payment discounts", "Extending payment deadlines", "Reducing allowances", "Issuing invoices as late as possible"], 0, 1, None,
                     "An early payment discount rewards early payment."),
         keep="One measure speeds up the collection; the others slow it down.", vary="The question about receivables."),
    dict(id="m2_q13", midterm=2, number=13, points=1, kind="mcq", title="Cash budget",
         question=_q("Why is a cash budget prepared?", ["To present payroll", "To close bank accounts", "To fill tax forms", "To plan cash inflows and outflows"], 3, 1, None, "A cash budget plans the cash movements."),
         keep="Purpose / content of a planning document.", vary="The question about the cash budget."),
]


def all_exemplars() -> list[dict]:
    return EXEMPLARS


# ---------------------------------------------------------------- what the generator shows to the model
def _usd(text: str) -> str:
    """The site is English/USD only (generator.ENGLISH_RULE): the exemplars keep their structure, the currency is switched."""
    text = re.sub(r"\bTh HUF\b|\bthHUF\b|\bth HUF\b", lambda m: m.group(0).replace("HUF", "USD"), text)
    return re.sub(r"\bFt\b|\bHUF\b", "USD", text)


def _compact(obj) -> str:
    return _usd(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def examples_for(topic: str, max_chars: int = 5800) -> str:
    """Text appended to the system prompt of generator.py for this topic ('' when there is nothing to show)."""
    if topic == "balance_sheet":
        e = next(x for x in EXEMPLARS if x["id"] == "m1_q04")
        text = ("\n\nREAL MIDTERM TASK (compile variant). Write new tasks of EXACTLY this type: same description text (change only the kind of business), the same 15 captions A-G "
                "(use these captions character for character, they are the real exam's; ignore other numbering above), the same kind of item wording, the same logic. "
                f"{e['keep']} Different companies, items and numbers every time; do NOT copy these items or amounts.\n"
                f"EXAMPLE OF THE JSON TO WRITE:\n{_compact(e['raw'])}")
        return text[:max_chars]
    if topic == "terminology":
        mcq = [x for x in EXEMPLARS if x["kind"] == "mcq"]
        calc = [x for x in mcq if x["question"].get("calc")]
        theory = [x for x in mcq if not x["question"].get("calc")]
        random.shuffle(theory)
        head = ("\n\nREAL MIDTERM QUESTIONS. Write new questions of EXACTLY these types (same stems, same wording style, same logic and the same kind of wrong options); "
                "change the numbers, the items or the situation, never copy an example. Calculation types (the wrong options are typical mistakes, 'calc' is mandatory):\n")
        text, used = head, 0
        for x in calc:
            line = f"- {x['title']}. {x['keep']} Wrong options: {x.get('distractors', 'typical mistakes')}\n  {_compact(x['question'])}\n"
            if len(text) + len(line) < max_chars - 1600:
                text, used = text + line, used + 1
        text += "Concept types (one right option; 'Select the incorrect statement!' asks for the wrong one):\n"
        for x in theory:
            line = f"- {x['title']}: {x['keep']}\n  {_compact(x['question'])}\n"
            if len(text) + len(line) < max_chars:
                text += line
        return text
    return ""


# ---------------------------------------------------------------- self-check: python midterm_templates.py
if __name__ == "__main__":
    import exam
    for x in EXEMPLARS:
        if x["kind"] == "mcq":
            exam.normalize_question(x["question"], 1)
            assert len(x["question"]["prompt"]) <= 500, x["id"]
        elif x["kind"] == "balance_sheet":
            sheet = exam.normalize_balance_sheet(x["check_raw"], "compile")
            exam.normalize_balance_sheet({**x["raw"]}, "compile")   # the model's version (plug item) must also work
            assert any(a.get("plug") for a in x["raw"]["accounts"])
    print(f"{len(EXEMPLARS)} exemplars ok; " + ", ".join(f"{t}: {len(examples_for(t))} chars" for t in ("balance_sheet", "terminology")))
