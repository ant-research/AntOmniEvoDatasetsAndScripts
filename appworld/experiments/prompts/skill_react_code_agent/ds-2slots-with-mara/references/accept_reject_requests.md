# Accept / Reject Pending Requests & Invites

Use for tasks of the form 'accept/approve (or reject/deny/decline) all pending <payment requests / invitations / notifications> from <named people or relationship groups, e.g. roommates, coworkers>.' Covers both directions of the same workflow.

## 1. Map the task verb to a direction and a mutation API
- 'accept' / 'approve' -> the app's approve/accept mutation endpoint (e.g. `approve_payment_request`, `accept_group_invitation`).
- 'reject' / 'decline' / 'deny' -> the app's deny/decline mutation endpoint (e.g. `deny_payment_request`).
Print the received/incoming list API doc AND the mutation API doc before calling either; read the valid status values and the mutation's required parameters from them instead of guessing.

## 2. Resolve the requester set from the contact app
For relationship-named people (roommates, coworkers, friends), follow the people look-up procedure (SKILL.md step 6 -> `references/people_references.md`): enumerate `show_contact_relationships`, search each named relationship fully paginated, union the results, and take each contact's unique identifier (email/phone). Match that identifier against the record's sender/inviter field; never guess identities and never use the target app's own friend list.

## 3. Pull only the still-pending rows
Call the app's received/incoming list API with the server-side status filter whose value means 'pending' (read that value from the doc), fully paginated. Do not pull the whole history and hand-filter when a status filter parameter exists.

## 4. Restrict to the named requesters
Keep the pending rows whose sender/inviter identifier is in the requester set; act on exactly those ids and nothing else.

## 5. Apply the accept/reject mutation once per id
Pass the access_token and the record id taken from the source row. Skip any row whose status no longer equals 'pending' before mutating.

## 6. Verify both sides, then finish
Re-query the pending list and assert zero target rows remain, AND re-query the resolved list (status 'approved' for accept, 'denied' for reject) and assert every target id now carries the resolved status and that no request was added or removed. Then call `apis.supervisor.complete_task()` with no `answer` (state-change task).