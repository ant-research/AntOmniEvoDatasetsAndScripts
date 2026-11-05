# Music Player Queue: 'Played So Far' / Queue-Prefix Actions

Use for state-change tasks of the form 'like/star/remove every song (or track) played so far in my music-player queue, including the current one.' The target set is the queue PREFIX up to and including the currently playing song. The queue is the source of truth for play order: it lists every entry with an order position and marks the current song with an `is_current` flag. Songs AFTER the current one are future queued items, not yet played — act on them too and the evaluator's exact acted-ids assertion fails.

## 1. Read the queue doc and fetch the full queue, then derive the current position
Print the queue API doc and the per-record mutation API doc before calling either. Fetch the queue and derive the current position programmatically from the `is_current` marker — never eyeball the printed list and retype a magic position number:

```python
queue = apis.<music_app>.<show_queue_api>(access_token=<token>)
current_pos = next(e['position'] for e in queue if e['is_current'])
```

## 2. Cross-check the current song, then build the prefix set
Confirm the current-song detail API (`show_current_<entity>`) returns the same id/title as the queue entry at `current_pos`; if they disagree, re-read both before acting.

'Played so far, including the current one' = every entry with `position <= current_pos`. An exclusive wording ('up to', 'before the current one', 'excluding the current') drops the entry at `current_pos` — re-read the boundary words before choosing `<=` vs `<`.

```python
target_ids = [e['<id_key>'] for e in queue if e['position'] <= current_pos]
```

## 3. Apply the mutation once per id
Loop over `target_ids` and call the per-record mutation once per id with the access_token. Skip ids already in the done state when the mutation is not idempotent (an already-liked id errors or duplicates a side effect).

## 4. Verify against the queue prefix, then finish
Re-query the done-state list (e.g. liked songs) fully paginated and assert every target id is present. Print THAT list API's doc first and read its `page_limit` cap — liked/starred lists commonly cap the page_limit near 20, so guessing a larger value raises a 422 validation error before anything runs. Re-derive `expected_ids` from the queue prefix (`position <= current_pos`) rather than re-checking the ids you already chose, so a wrong current-position derivation is still caught. Then call `apis.supervisor.complete_task()` with no `answer` (state-change task).