# Ledger-to-Payments Reconciliation

Use for tasks of the form 'a note or ledger lists what each person owes; some already paid through a payments app; request or charge the rest.' Also applies to any 'act on the not-yet-done subset' task where the done set lives in a separate history list.

## 1. Choose the correct ledger and anchor the time
Get the current date/time from the clock or phone API. When the task uses a relative time such as 'yesterday' or 'last weekend', compute the target calendar date. Select the ledger whose created_at is consistent with that date (a note written the morning after an event usually describes the prior day). When several records match a keyword, apply `record_disambiguation.md` (date anchor + value cross-matching) to settle on exactly one before proceeding.

## 2. Parse the ledger into name -> amount
Extract every per-person line into a dict `{name: numeric_amount}`. Assert the dict is non-empty and that its size equals the number of lines so no entry is dropped.

## 3. Pull the full payment history
Paginate the payments app history list fully (`page_index` loop, stop on a short or empty page). Never judge who paid from a single default-size page: recent incoming payments often sort after older outbound ones.

## 4. Identify who already paid (match description AND amount AND direction AND date)
Prefer server-side filters (description/query, date range, direction) when the history API exposes them; otherwise filter the fully-paginated list in Python. Keep only INCOMING payments whose description matches the task phrase, then confirm each candidate against the ledger by amount: a candidate counts as paid only if its amount equals that person's ledger amount. Drop amount mismatches — they belong to a different event.

## 5. Act only on the remainder
`remaining = {name: amount for name, amount in ledger.items() if name not in paid_names}`. For each remaining name, resolve the unique recipient identity: search users by that first name and pick the result whose first name matches exactly (disambiguate by last name when more than one matches), then create the request/charge with the exact ledger amount and the task description phrase.

## 6. Verify and finish
Re-read the sent/requested list and assert every remaining person has exactly one new record carrying the correct amount and description. Only then call `apis.supervisor.complete_task()`.