# Rating / Review Tasks (create or change a star rating)

Use for tasks of the form 'give/rate N stars to <records> [with <predicate>]' or 'if already rated higher/lower, change it to N'. Applies to any app whose rating lives on a separate review record: a create/review API writes a new review with a `rating`, and a separate update-review API changes an existing review's rating.

## 1. Compute the target set
Enumerate the collection(s) the task names fully paginated (SKILL.md step 7). For a union/intersection/difference of collections, build the keep set with `references/multi_criteria_filtering.md` steps 1-2 first. If the selection predicate is not expressible as a server-side filter on the list API, collect all rows and resolve the predicate yourself — never pass a filter parameter the list API doc does not list.

## 2. Resolve a per-record state predicate from a per-record API
When the predicate is a per-user state flag (liked, reviewed, downloaded, in_library, following) that the list row omits, read it per record from the app's per-record private/state API (`show_<entity>_privates` or the reviews list), once per candidate id. Do not infer it from list position or from another collection's membership.

## 3. Classify each target record into create / update / skip
For each record satisfying the predicate, fetch the user's own existing review for it (the reviews list API with a `user_email`/`user` filter, paginated) and compare its rating to the target:

- no existing review -> CREATE via the create/review API with `rating=target`.
- an existing review whose rating differs from target -> UPDATE via the update-review API using that review's id and `rating=target` (this is the only way 'if already rated higher, change it to N' is satisfied).
- an existing review already equal to target -> SKIP; re-rating to the same value is a needless side effect.

## 4. Apply the mutation
Print the doc for BOTH mutation APIs (create/review and update-review) first, then call create once per unreviewed record and update once per record whose rating must change. Never call the create API on a record the user already reviewed — it adds a second review that the evaluator's added-vs-updated assertions (disjoint sets) flag; never lower a rating that already meets the target.

## 5. Verify by re-querying, then finish
Re-query each target record's reviews (filtered to the user) and assert every target record now carries the target rating and that the set of newly-added review records equals exactly the records you targeted for create (and the updated ones equal those targeted for update). Fix any mismatch, then call `apis.supervisor.complete_task()` with no `answer` (state-change task).