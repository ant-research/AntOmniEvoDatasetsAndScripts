# Multi-Criteria Keep/Remove Filtering

Use for tasks of the form "keep entries satisfying condition A and/or B; remove the rest; leave other collections unchanged," and for "act on every record matching a predicate" tasks (like/star/react/forward/comment/follow/unfollow/send/delete/download), including "download all <entity>s from my <collections> that I have <predicate>d" (target set = members of the named collections that also satisfy the second-collection predicate; download each member). For follow/unfollow tasks, the already-done state is the app's own follow list (a `show_following_<entities>`-style API, user-scoped and paginated): read it fully paginated before mutating and skip ids already followed/unfollowed, then verify the follow list afterwards with the exact-set gate.

## 1. Gather source lists
Fetch every relevant source list with full pagination (loop `page_index` from 0 until a page returns fewer than `page_limit` items or is empty) and store the ids.

## 2. Build the keep set
- keep if A or B -> `keep_ids = set(a_ids) | set(b_ids)`
- keep if A and B -> `keep_ids = set(a_ids) & set(b_ids)`
- keep if A only -> `keep_ids = set(a_ids) - set(b_ids)`

## 3. Handle nested per-member conditions
When a criterion on a group entry is defined over its members (e.g. a group is kept when its flag is set OR when every member is in the keep set), fetch each group's member ids and evaluate the condition over members:

```python
keep_groups = set()
for group in groups:
    if group['group_id'] in flag_group_ids:
        keep_groups.add(group['group_id'])
    elif all(mid in member_keep_ids for mid in group['member_ids']):
        keep_groups.add(group['group_id'])
```

## 3a. Cross-collection membership / negation ('act on every X who <has/has not> <verb>ed any <thing> in a second collection')
When the keep predicate lives in a SECOND collection the task names (e.g. "unfollow every artist who has not sung even one liked song" — "sung a liked song" lives in the liked-songs list, not the followed-artists list), build the keep set from that collection's nested references and join on ids, never on names or list position.

1. Fetch collection A (the records you may act on) fully paginated and keep each row keyed by its id field.
2. Fetch collection B (the evidence list the task names) fully paginated and expand EVERY nested reference to A's entities into one id set. A row in B can reference several A ids, so collect them all, not just the first:
   `ref_ids = set(ref['<id_key>'] for row in rows_b for ref in row.get('<nest_key>', []))`
3. `keep_ids = {row['<a_id>'] for row in rows_a if row['<a_id>'] in ref_ids}`. A positive verb ("has") acts on `keep_ids`; a negative verb ("has not"/"never"/"not even one") acts on the complement (`[a for a in rows_a if a['<a_id>'] not in ref_ids]`).
4. Act only on records in A: the verb is a membership test of A's ids against B, so records absent from A are never touched and records in A that pass are untouched. Then continue to step 4 to cross-check before mutating.

## 3b. Scope-is-parent, predicate-is-child binding ('act on all the <child> from my <parent collection> that I have <state>')
When the task names a CHILD entity inside a PARENT collection plus a per-child state ('download all the songs from my album library that I have liked', 'star all the episodes of my podcast library that I have played'), the state predicate modifies the CHILD entity, not the parent collection.

1. Fetch the parent collection fully paginated and expand EVERY row's nested child-id list into one scope set:
   `scope_ids = {cid for row in parent_rows for cid in row['<child_ids_key>']}`
2. Fetch the child-level state list the predicate names (`show_liked_songs`, `show_played_...`) fully paginated into `predicate_ids`.
3. `keep_ids = scope_ids & predicate_ids` — act only on that intersection, never on a whole parent.

Never substitute the parent-level state list (e.g. `show_liked_albums`): 'liked albums' is a different relation from 'liked songs' — downloading every song of a liked album over-includes non-liked tracks and under-includes liked tracks that live in un-liked library albums. Then continue to step 5 to mutate and step 8 to verify the acted ids against the independently re-derived `scope_ids & predicate_ids`.

## 4. Cross-check against the full collection
Query the unfiltered full collection and confirm the keep set equals the records that genuinely satisfy the stated thresholds. If the filtered result disagrees with a per-record check of the raw thresholds, the boolean combination or threshold direction (>= vs >) is wrong — fix it before mutating.

## 5. Act on the matching set itself (like/star/react/forward/comment/send/delete/download tasks)
Accept/approve and reject/deny/decline of PENDING request/invite records have their own procedure (`references/accept_reject_requests.md`) — for those verbs route there instead of this step.

When the task verb applies to the matching set (e.g. "like all X from yesterday involving Y", "comment on all payments from my friends", "delete all text messages from <number>", "download every song in my library that I have liked"), loop over `match_ids` and call the per-record mutation API once per id. Look up the mutation API doc with `show_api_doc` first so the id parameter and required fields are known. Do not print the raw source rows first — print the match set and act on it.

If any <people> in the predicate are named by relationship ("my friends", "coworkers", "family"), resolve them from the phone/contact app FIRST — never the target app's own friend/social list, which counts a different set — print and follow the people look-up procedure:
    print((SKILL_DIR / 'references/people_references.md').read_text())

For message-thread delete/remove tasks ("delete all messages FROM X"), searching by the other party's identifier (phone number/email) returns the whole thread in BOTH directions. Filter by direction before acting: "from X" -> keep only rows whose sender/from identifier equals X; "to X" -> keep only rows whose receiver/to identifier equals X. Deleting the whole thread removes messages you sent too, which the verifier counts as extra removed ids.

```python
mutation_doc = apis.api_docs.show_api_doc(app_name='<app>', api_name='<mutation_api>')
print(mutation_doc)
for mid in match_ids:
    apis.<app>.<mutation_api>(access_token=token, <id_param>=mid)
```

If the mutation is not idempotent (liking an already-liked record or downloading an already-downloaded one errors or duplicates a side effect), read the current state field first (like_count / liked list / seen list / downloaded list), skip ids already in the done state, and act only on the remainder. Download-style mutations are non-idempotent the same way: a `download_<entity>` call on an already-downloaded id returns 422 with an "already downloaded" message and creates no record, so read each id's done state first (`downloaded: true` in `show_<entity>_privates`, or membership in the `show_downloaded_<entities>` list) and skip ids already downloaded. Catch only that "already downloaded" 422 and continue; never wrap the call in a blanket `except Exception` that also swallows auth/token/validation failures. For rating/review verbs ("rate N stars") a rating lives on a separate review record with create-vs-update semantics, so use the rating procedure (`references/rating_review_tasks.md`) instead of this loop.

## 6. Act only on the complement
Otherwise `to_act = [id for id in collection_ids if id not in keep]`. For removal tasks, call the remove API once per id. For request tasks (request payment from those who have not paid yet), call the create/request API once per id using that entry's values from the source record (amount, message). For remind/nudge tasks (remind those who have not replied/approved for N days), route to `references/remind_tasks.md`: a reminder is its own notification API taking only the record id and `access_token`, never the create/request API with amount/message. Never re-act on entries already in the keep set (already done), and leave other collections unchanged.

## 7. Minimize dump volume
Never print a whole high-cardinality collection (feeds, histories). Fetch pages into a variable and print only `len(rows)` plus the ids of the rows you will actually mutate: `print([r['id'] for r in match_rows])`. Dumping hundreds of raw rows is what stalls a run before the mutation step.

## 8. Verify by re-querying
Re-query each mutated collection and assert the resulting ids match the expected state (the keep set for removals, or match_ids + existing state for add-style mutations). For add-style mutations, capture the already-done ids (`preexisting_ids`) BEFORE mutating from the app's done-state LIST API (`show_liked_*`, `show_downloaded_*`) paginated fully over the WHOLE collection — not per-record `show_<entity>_privates` probes of only `match_ids`, which miss records already in the done state that sit outside the target set (e.g. songs already downloaded that are not in this task's scope) and make the exact-set check below falsely flag them as extras. Fall back to per-record `show_<entity>_privates` only when no done-state list API exists, and then probe the full source collection, not just `match_ids`. Re-derive `expected_ids` independently from the predicate inputs — the contact-resolved people set and the date window — not from the id set you already chose, because re-checking your own chosen set cannot catch a wrong people/date selection:

```python
final_ids = [entry['id'] for entry in re_query_api()]
expected_ids = preexisting_ids | set(match_ids)  # add-style: done ids captured BEFORE mutating
assert set(final_ids) == expected_ids, (final_ids, expected_ids)
```

An exact-set `assert` (not a print-only subset check) must gate `complete_task`: every target id present AND no extra id added. Fix any mismatch before calling `complete_task`.