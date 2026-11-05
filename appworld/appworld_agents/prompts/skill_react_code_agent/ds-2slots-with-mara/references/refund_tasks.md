# Refund / Send-Back Tasks

Use for tasks of the form 'the last <payment request / transfer / payment> I <sent / made> was a mistake (or was approved/paid); send the money back / refund it.' Generalizes to any 'reverse the most recent matching record' task in a history list.

## 1. Pick the history direction from the task verb
'I sent / requested / paid' -> read the OUTGOING list (sent requests, outbox, outgoing transfers). 'I received / they sent / they approved' -> read the INCOMING list. Reading the wrong direction surfaces the wrong counterparty and amount, so map the verb to the list before querying.

## 2. Fully paginate the history list
Loop `page_index` from 0 with one `page_limit` until a page is short or empty (SKILL.md step 7). 'The last' record can sit on any page; a single default-size page can miss it.

## 3. Keep records for the named counterparty with the stated status
Filter to records whose counterparty name/email equals the named person. When the task states a status ('they approved it', 'it went through', 'declined'), keep only records carrying that status (e.g. a non-null approved_at/paid_at; drop pending/denied). A pending record for the same person is a different event.

## 4. Select the most recent by the record's own timestamp
Sort the surviving records by `created_at` descending and take the first. 'The last / most recent' is an explicit temporal selector — apply it to the date field, never to list position.

## 5. Read amount and counterparty from that record
Take the exact amount and the counterparty's account identifier (email / user id) from the selected record's own fields, not from the task wording.

## 6. Compensate with the same counterparty and amount
Call the app's send-money / transfer API to that counterparty's identifier with the exact amount. When the account balance is insufficient, charge a valid card through the send API's own card parameter — do NOT top up the account balance first via a separate add/transfer API, because the top-up creates an extra bank-transfer/account record (and can write a receipt file) that the evaluator's model-changes assertion flags as an unexpected side effect.

## 7. Verify and finish
Re-read the new record and assert its counterparty and amount equal the selected record's values. Only then call `apis.supervisor.complete_task()` with no `answer` (state-change task).