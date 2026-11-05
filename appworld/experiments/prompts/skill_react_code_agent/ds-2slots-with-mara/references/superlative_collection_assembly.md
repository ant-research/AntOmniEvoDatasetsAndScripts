# Assembling a New Collection from Per-Collection Superlatives

Use for state-change tasks of the form 'make/assemble/create a <new collection type> called <title> containing only the most/least <field> <entity> from each of my <collections>' (e.g. 'make a playlist with the most-played song from each of my playlists'). The output is ONE winner per parent collection — the member whose comparison field is the max/min within that parent — collected into a brand-new collection, not one global winner and not the parents themselves. Read-only superlative QUESTIONS that return a single title route to `references/superlative_selection.md` instead.

## 1. Enumerate every parent collection fully
Print the parent collection's list/library API doc first and read its parameter list and page caps. 'each of my playlists' means EVERY parent, so paginate the library/list call fully (SKILL.md Procedure step 7: loop `page_index` from 0 with ONE `page_limit` copied from the doc, stop when a page returns fewer than `page_limit` items). Stopping after the default first page silently drops parents and their winners.

## 2. For each parent, read its members, then the per-member comparison field
Print the per-parent detail API doc (it returns the parent's nested member list) and the per-member detail API doc. Map the task's superlative word to the member's comparison field using the mapping in `references/superlative_selection.md` step 1 ('played'/'most-played' -> `play_count`, 'rated'/'highest-rated'/'top-rated' -> `rating`). Parent and member summary rows omit that field, so fetch it from the per-member detail API once per member id — never from a parent-level attribute or list position.

```python
winners = []
for parent in parents:
    detail = apis.<app>.<show_parent_api>(<parent_id_param>=parent['<id_key>'], access_token=token)
    best_id = None
    best_val = None
    for m in detail.get('<member_list_key>', []):
        v = apis.<app>.<show_entity_api>(<entity_id_param>=m['<id_key>']).get('<field>')
        if best_val is None or v > best_val:  # 'most' -> '>'; 'least'/'fewest' -> '<'
            best_val = v
            best_id = m['<id_key>']
    winners.append(best_id)
```

The strict comparison keeps the FIRST member on a within-parent tie. Re-read the task wording for a named tie-breaker before relying on that order; when the task names none, keep the first.

## 3. Create the new collection, then add each winner exactly once
Print the create and add API docs first. Create the collection with the exact title from the task, then loop over `winners` and call the add API once per winner id (SKILL.md Queue/playlist adds rule: the add API, never a play/replace/reorder API). Each winner is added exactly once — no duplicates, no loser members, and no pre-existing collection is touched.

```python
new = apis.<app>.<create_api>(title='<task title>', access_token=token)
for wid in winners:
    apis.<app>.<add_api>(<parent_id_param>=new['<id_key>'], <entity_id_param>=wid, access_token=token)
```

## 4. Verify the exact member set, then finish
Re-read the new collection and assert its member ids equal `sorted(winners)` — every winner present and no extra id (SKILL.md Procedure step 8 exact-set assert). Call `apis.supervisor.complete_task()` with no `answer` (state-change task).