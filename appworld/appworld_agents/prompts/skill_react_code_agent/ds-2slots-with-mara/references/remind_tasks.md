# Remind / Nudge Stale Pending Requests

Use for tasks of the form 'send a reminder [on <app>] for all my <payment requests / invoices / bills> [to <named people or a relationship group>] that have not been approved, denied, or paid for N or more days' — i.e. any 'remind or nudge every record still stuck in a pending state for >= N days' task. A reminder sends a notification and does NOT change the record's status, so it is distinct from accept/reject (`references/accept_reject_requests.md`): never approve/deny, and never expect the reminder to move a request out of the pending list. For 'my requests TO <people>' the source is the OUTGOING/SENT list and the counterparty is the receiver/recipient field — never the received/incoming list, which is the opposite direction.

## 1. Print the reminder API doc first
The reminder is its own per-record notification API (e.g. `remind_payment_request`) taking the record id plus `access_token` — it does NOT take an amount, message, or any field copied from the source row. Print its doc and pass exactly the parameters it lists. Never substitute the create/request API and never attach amount/message kwargs to the reminder call.

## 2. Resolve relationship-named people from the contact app
For 'to my roommates/friends/...', follow `references/people_references.md`: enumerate the contact book's relationship values, search each named relationship fully paginated, union the results into one identifier set, and match it against the record's counterparty field. Never guess identities and never use the target app's own friend/social list.

## 3. Pull the still-pending rows fully paginated
Call the outgoing list API with the server-side status filter whose value means pending ('not approved or denied/paid' IS that pending value) — read the valid status strings from the doc (e.g. `status='pending'`). Page fully, reusing ONE `page_limit` read from the doc on every page. If the doc lists no status filter, fetch all rows and keep the ones whose status/approved_at/denied_at fields are still unset.

## 4. Age the pending rows by datetime, not string comparison
A still-pending record has been unresolved since it was created, so 'for N or more days' means `created_at <= now - timedelta(days=N)` (age >= N days), not `updated_at` and not the 'last N days including today' window (`>= now - (N-1) days`). Compute `cutoff = now - timedelta(days=N)` from a naive `datetime.now()` and compare with `datetime.fromisoformat(row['<created_at_field>'])` (SKILL.md Dates rule, which also forbids lexicographic ISO string comparison).

```python
target_ids = [r['<id_key>'] for r in pending_rows
              if r['<counterparty>']['<email_or_phone>'] in people_ids
              and datetime.fromisoformat(r['<created_at_field>']) <= cutoff]
```

## 5. Remind once per id, then verify against the unfiltered collection
Loop over `target_ids` and call the reminder API once per id (record id + `access_token`), skipping any row whose status is no longer pending at mutation time. Confirm each call returned its per-record success message (e.g. `'reminder sent'`) before proceeding. Then re-query the UNFILTERED collection (no status filter) fully paginated and independently re-derive the expected ids — people set AND still-pending AND `datetime.fromisoformat(created_at) <= cutoff` — and assert the acted set equals it (re-deriving from the unfiltered list catches a wrong status value that the filtered list would hide). The reminder notification is delivered to the COUNTERPARTY (the person being reminded), not to you, so your own notification list shows zero newly added reminders even when every reminder succeeded — do NOT use your own notification list as the pass/fail gate and do NOT re-send because that list stayed empty. Then call `apis.supervisor.complete_task()` with no `answer` (state-change task).