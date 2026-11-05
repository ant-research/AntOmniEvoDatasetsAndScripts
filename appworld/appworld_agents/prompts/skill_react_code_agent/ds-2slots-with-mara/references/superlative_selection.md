# Superlative (Min/Max) Selection Across Collections

Use for questions of the form 'newest/oldest/earliest/latest/highest/lowest/most/least <entity> in or across my <collections>' that return one entity (or every tied entity).

## 1. Resolve the comparison field from the question verb
Map the question's verb to the matching field on the entity's detail doc, then confirm the field exists by printing that detail API's doc: 'released' -> release_date, 'added' -> added_at, 'created' -> created_at, 'downloaded' -> downloaded_at, 'played'/'plays'/'most-played' -> play_count, 'rated'/'highest-rated'/'top-rated' -> rating, 'liked'/'most-liked' -> like_count. A 'highest/top <field>' superlative selects the max numeric VALUE (e.g. rating); a 'most <verb>' superlative selects the max COUNT (e.g. play_count, like_count) — confirm the actual numeric field in the detail doc's response schema before using it. Never default to a date on a collection summary, because summary rows often record when the item was added to the collection, not the entity's own date.

## 2. Enumerate every named collection

The question usually names the collections explicitly; when it names an artist/creator instead, the "collection" is that artist's catalogue via the search API's `artist_id`/creator filter. Call the matching list/library/search endpoint with full pagination using the pagination helper (SKILL.md step 7) so a truncated first page can never hide candidates — a page that fills exactly `page_limit` rows is NOT proof of exhaustion, so the helper keeps paging until a shorter page and raises otherwise.

```python
exec((SKILL_DIR / 'scripts/pagination.py').read_text())
def one_page(**p):
    return apis.<app>.<list_api>(page_index=p['page_index'], page_limit=p['page_limit'], <fixed filter params>)
rows = fetch_all(one_page, PAGE_LIMIT)
```

Omitting a named collection silently drops candidates. The helper aggregates across pages in order, so the answer min/max is taken over the FULL list, never the first page alone.

## 3. Gather candidate ids from every named collection
'across my A, B and C' means the union of every <entity> id exposed by each named collection. Add a collection entry's OWN id to the candidate set only when that entry IS the target entity (a song library's rows are songs); for a collection whose entries CONTAIN the target entity (album/playlist rows embed song_ids), add ONLY the nested child-id list and never the container's own id — album/playlist ids are a different entity's namespace, and passing one to the per-entity detail API silently returns an unrelated record and corrupts the min/max answer with no error. Dedupe into one set:

```python
candidates = set()
for entry in target_entity_entries:                # rows that ARE the target entity
    candidates.add(entry['<entity_id_key>'])       # e.g. song_id on song-library rows
for entry in container_entries:                    # rows that EMBED the target entity
    candidates.update(entry.get('<child_ids_key>', []))  # e.g. song_ids on album/playlist rows
```

## 4. Fetch the comparison field per candidate
Collection summaries usually omit per-member fields, so call the per-item detail API once per unique candidate id and read the comparison field. Do not reuse a parent-level attribute for a child record, and do not answer from list fields alone.

## 5. Select the extremum and compare safely
- ISO 8601 date/time strings compare chronologically as plain strings ONLY when zero-padded and sharing one format; if precision or format varies (date-only vs datetime), parse with `datetime.fromisoformat(...)` first.
- For numeric fields, use max/min over numbers.
- If ties are possible and the question accepts multiple answers, return every tied record.

```python
winner = max(candidates, key=lambda r: r['<field>'])  # 'newest'/'highest'/'most'
# winner = min(candidates, key=lambda r: r['<field>'])  # 'oldest'/'lowest'/'least'
```

## 6. Verify before answering
Re-group and sort the fetched records by the comparison field; confirm the top entry is unchanged. Then re-derive the answer value independently across the full candidate set and assert the chosen record actually attains it — a min/max computed over a truncated first page is the near-certain failure mode for 'least/lowest/most/highest' questions, so the assertion must run over the FULL paginated list (use `fetch_all` again on the same endpoint, not the earlier partial variable):

```python
vals = [c['<field>'] for c in rows]  # after step 4 populated '<field>' on every candidate
target = min(vals)  # 'least'/'lowest'/'oldest' -> min; 'most'/'highest'/'newest' -> max
winners = [r for r in rows if r['<field>'] == target]
assert winners, 'no candidate attains the extremum — the candidate list is incomplete'
```

If the extreme sits near a pagination or collection boundary, re-query the boundary page to confirm nothing more extreme was missed. Print the winner's id, comparison value, and title before answering.

## 7. Answer minimally
Call `apis.supervisor.complete_task(answer=<title>)` with only the entity's title/name, never a sentence.