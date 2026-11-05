# Equal-Sharing a Paid Receipt (request each co-sharer)

Use for tasks of the form 'I paid for a receipt/utility in my file system; the amount is shared equally among some named people and me; make requests/charges to the others for their share.' Do not use the ledger-reconciliation procedure here: there is no per-person ledger and no who-already-paid history — the split is one receipt total divided by the number of sharers.

## 1. Anchor the relative time
Get the current date from the clock/phone API, never from memory. Translate the task's relative phrase to a calendar target: 'last month's X bill' = the previous calendar month relative to today (e.g. today 2023-05-18 -> target 2023-04); 'this month' = the current month.

## 2. Locate the receipt in the file system
List the directory (recursively if needed) and pick the file whose folder matches the bill type and whose name carries the target month (e.g. `<type>/<YYYY-MM>.<ext>`). Match the computed month, not 'newest file' or 'first file listed'; the folder may hold many months of the same bill type.

## 3. Read the Total Amount
Open the chosen file and read its full content. Take the line labelled 'Total Amount' (the receipt's final total), not a component charge, service fee, tax, or subtotal line.

## 4. Split equally including the payer
'Shared equally among A, B and me' means the payer is one of the sharers: divisor = number of named people + 1.

```python
share = round(total / (len(named_people) + 1), 2)
```

The payer does not receive a request/charge; only the named other people do.

## 5. Resolve each other participant
Enumerate the named people from the people/contact app (list the relationship names, then search by that relationship fully paginated) and take each person's unique identifier (email/phone) from the matched record — never retype it from the task wording.

## 6. Request/charge and verify
For each other participant, call the payments app's request/charge API with their unique identifier, amount = `share`, and the task's exact description phrase. Re-read the sent/requested list fully paginated and assert every other participant has exactly one new record with amount `share` and the exact note; fix any mismatch before calling `apis.supervisor.complete_task()`.