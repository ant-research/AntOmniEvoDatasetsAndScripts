# Sending Money to a Person (direct send/pay/transfer tasks)

Use for tasks of the form 'send/pay/transfer $N to <a named person, phone number, or email> [publicly/privately]' in a payments app, where the recipient is one specific person and the task must produce exactly one new transaction (not a refund, bill split, or equal-share split — those have their own procedures). The final state must contain EXACTLY ONE new transaction; any other persisted change (a second transaction, a bank transfer, a receipt file) fails the evaluator's changed-models / exact-transaction-count assertions even when the transaction itself is right.

## 1. Resolve the recipient's identifier from a read API — never from memory
- Phone number given -> call the contact/phone app's `search_contacts(query=<number>)` and keep ONLY the contact whose `phone_number` field equals the given number exactly (after stripping dashes/spaces); the search returns fuzzy, unrelated rows alongside the query, so never take the first row or a name that merely looks right. Take that row's email as the payments identifier.
- Name given -> follow `references/people_references.md` (exact-name match, then take email/phone).
- Email given -> use it verbatim.
Never type a recipient identifier from memory, and never feed the raw phone number as the transaction API's email parameter.

Resolve the identifier in code and pass the resulting variable to the send call — never paste a contact's email as a literal read off the printed search output:

```python
given = '<the phone number from the task>'.replace('-', '').replace(' ', '')
matched = [c for c in contacts if str(c['phone_number']).replace('-', '').replace(' ', '') == given]
assert len(matched) == 1, matched
recipient_email = matched[0]['email']
```

Then use `recipient_email` in the send call's recipient parameter.

## 2. Print the send API doc and map the task qualifiers exactly
Print the send/transfer/create-transaction API doc first and note its recipient parameter (often `receiver_email`), amount parameter, description/note parameter, privacy flag, and the optional payment-source/card parameter. Map the wording before calling:
- amount -> when the task states an amount, use the bare number with no currency symbol or separator. When the task does NOT state an amount ("send the owed money", "reimburse them", "as per my/our text message conversation"), never guess: read the message thread with the recipient through the messaging app's conversation-list API (e.g. the phone app's `show_text_message_window`) and take the amount from the message that matches the task's subject. When the thread contains several amounts, pick the one tied to the task's subject and skip amounts belonging to other events — especially an amount later cancelled by "we'll call it even" / "you pay this time" language, which settles that debt and must not be re-sent.
- 'privately' / 'private' -> pass the privacy flag's true value (often `private=True`); 'publicly' -> its false value. Pass it explicitly so the evaluator's public/private check matches — omitting it publishes a private transaction or hides a public one.
- a quoted note ("with a description note ...", "with the message ...") -> pass that exact string as the send API's description/note parameter so the evaluator's description check matches; keep any separately requested follow-up text message (e.g. "Done. Thanks!") as a distinct messaging-app send, never folded into the transaction description.

## 3. Check the balance BEFORE creating the transaction
Call the account-balance API (e.g. `show_<entity>_balance`) FIRST and branch on its result. Never probe the send API first and use its 422 as the balance check: a balance-funded attempt that fails inside the transaction call can still persist the transaction record, and there is no delete-transaction API to undo it.
- balance >= amount -> create the transaction funded from the balance (no instrument override).
- balance < amount -> skip any balance-funded call entirely and go directly to step 4 (card funding).

## 4. Fund from a valid card and try EVERY candidate
When the balance is insufficient, list the payment cards and drop expired ones by comparing `expiry_year`/`expiry_month` against the current date (from the clock/phone API). Then charge a card through the send API's own source parameter (e.g. `create_transaction(..., payment_card_id=<id>)`). Wrap EACH card in its own try/except so one card's failure never aborts the loop; print the failure and continue to the next card. A 422 error names the reason ('expired' -> skip that card, 'does not have $X' -> the card lacks funds, try the next card).

```python
for card in valid_cards:
    try:
        result = apis.<payments_app>.<send_api>(
            <recipient_param>=recipient_email, amount=amount,
            access_token=token, private=True,
            payment_card_id=card['<card_id_key>'])
        print('success', result)
        break
    except Exception as e:
        print('card failed', card['<card_name_key>'], e)
```

## 5. Never top up the balance to fund a send
Do NOT call the add-to-balance / bank-transfer API (e.g. `add_to_venmo_balance`, `withdraw_from_<entity>_balance`) to fund a plain send. Each such call persists a bank-transfer record and can write a receipt file in the file_system app, adding model changes the evaluator's changed-models assertion rejects — and there is no API to delete them. The only persisted changes a send allows are the transaction itself plus its auto-created notification.

## 6. Verify exactly one new transaction, then finish
Before the send loop in step 4, snapshot the current transaction-id set (`before = {t['transaction_id'] for t in <fully-paginated transaction list>}`). After the send succeeds, run assertions — never stop at printing the transaction: re-read it and assert its receiver email == recipient_email, its amount == the task amount, and its privacy flag == the mapped value, then re-fetch the transaction list and assert exactly `len(before) + 1` ids with the new id present. Re-read the bank-transfer history and receipt/file directories and confirm they are unchanged. Then call `apis.supervisor.complete_task()` with no `answer` (state-change task).

```python
tx = apis.<payments_app>.<show_transaction_api>(transaction_id=result['transaction_id'], access_token=token)
assert tx['amount'] == amount, tx
assert tx['private'] is True, tx
assert tx['receiver']['<email_key>'] == recipient_email, tx
after = {t['transaction_id'] for t in apis.<payments_app>.<list_transactions_api>(access_token=token)}  # fully paginated
assert after == before | {result['transaction_id']} and len(after) == len(before) + 1, (before, after)
```