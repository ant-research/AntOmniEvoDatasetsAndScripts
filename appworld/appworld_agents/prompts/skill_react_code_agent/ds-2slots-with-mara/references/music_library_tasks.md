# Music Library: Acting on the Artist-Named Subset (add-to-queue / like / remove)

Use for state-change tasks of the form 'add all songs/tracks by <artist name> [with a numeric predicate such as "played over N times"] to my music player queue' or 'like/remove every song by <artist name> [predicate]'. The target set is every catalogue song whose artist matches the named artist AND whose numeric field satisfies the stated threshold. For 'every song played so far in my queue' PREFIX tasks, route to `references/music_player_queue.md` instead. For 'every song from the artists I follow' (no named artist — the follow list IS the artist set), use the followed-artists variant below instead of sections 1-4.

## Followed-artists variant: 'like/add/remove every song from the artists I follow'
When the task derives the artist set from the user's own follow list instead of naming an artist, the artist ids come from the app's follow-list API (`show_following_artists`-style), never from an artist-name search. Sections 1-4 (single named artist) do not apply.

1. Print the follow-list API doc first, then fetch it fully paginated and collect every followed artist's id. This list is user-scoped (takes `access_token`) and paginates (default page_limit is commonly 5) — reuse ONE `page_limit` copied from the doc on every page and stop when a page returns fewer than `page_limit` items. Do not fuzzy-search artist names instead: the follow list is authoritative, and a name search can match decoy artists or miss followed ones.
   ```python
   print(apis.api_docs.show_api_doc(app_name='<app>', api_name='<follow_list_api>'))
   followed = []
   pi = 0
   while True:
       page = apis.<app>.<follow_list_api>(access_token=token, page_index=pi, page_limit=page_limit)
       if not page:
           break
       followed.extend(page)
       if len(page) < page_limit:
           break
       pi += 1
   artist_ids = [a['<artist_id_key>'] for a in followed]
   ```
2. Print the song-search API doc, then fetch songs PER followed artist through its `artist_id` filter with NO query, fully paginated, and UNION the song ids into one deduped set. Pass only the parameters the song search doc lists (public search APIs take no `access_token`).
   ```python
   song_ids = set()
   for aid in artist_ids:
       pi = 0
       while True:
           page = apis.<app>.<song_search_api>(artist_id=aid, page_index=pi, page_limit=page_limit)
           if not page:
               break
           for s in page:
               song_ids.add(s['<song_id_key>'])
           if len(page) < page_limit:
               break
           pi += 1
   ```
   An `artist_id`-filtered search returns a song ONCE PER credited artist, so a song credited to two followed artists appears twice across the per-artist searches — build `song_ids` as a set and act on each unique id once (acting on the raw rows double-mutates cross-credited songs), and skipping any followed artist silently drops its songs.
3. Read the already-done state first (`show_liked_songs`, fully paginated). When the mutation is not idempotent (liking an already-liked song errors or duplicates a side effect), skip ids already done and mutate each remaining id exactly once.
4. Verify with SKILL.md step 8's exact-set gate, not a print-only subset check: re-read the liked list and `assert set(final_ids) == preexisting_ids | song_ids`. Then call `apis.supervisor.complete_task()` with no `answer` (state-change task).

## 1. Resolve the artist name to an artist id (print the doc, paginate fully, match exactly)
Print the artist search API doc BEFORE the first call and read its parameter list and page caps. Artist search is a paginated search API — never rely on its default first page (commonly 5 rows). Pass exactly the doc-listed parameters (public search APIs take no `access_token`).

```python
print(apis.api_docs.show_api_doc(app_name='<app>', api_name='<artist_search_api>'))
all_artists = []
pi = 0
page_limit = <cap read from the doc>
while True:
    page = apis.<app>.<artist_search_api>(query='<artist name>', page_index=pi, page_limit=page_limit)
    if not page:
        break
    all_artists.extend(page)
    if len(page) < page_limit:
        break
    pi += 1
matched = [a for a in all_artists if a['name'].strip().lower() == '<artist name>'.strip().lower()]
assert len(matched) == 1, matched
artist_id = matched[0]['<artist_id_key>']
```

Match case-insensitively and treat hyphens/underscores as spaces. If zero or several rows match, print every candidate and re-query before mutating — never pick by list position.

## 2. Filter server-side on artist id + threshold, then cross-check unfiltered
Print the song search API doc first. If it exposes an `artist_id` parameter and `min_<field>`/`max_<field>` numeric filters, pass the resolved artist id and the numeric filter together in ONE call (full pagination with ONE page_limit copied from the doc). Map the task wording with the SKILL.md Filtering rule's inclusive-boundary table — never guess the boundary ('over N' is strictly greater, 'at least N' is inclusive). Then re-query the same API with ONLY the artist id (no numeric filter), fully paginated, and confirm the filtered ids equal the rows whose raw field genuinely satisfies the threshold — catches a wrong direction or an off-by-one boundary.

## 3. Apply the mutation once per matched song id
Print the mutation API doc first (queue-add endpoints take `access_token` plus `song_id`/`album_id`/`playlist_id`). Loop over the matched ids and call the mutation once per matched id — never hardcode a single id from a glance. Skip ids already in the done state when the mutation is not idempotent.

```python
print(apis.api_docs.show_api_doc(app_name='<app>', api_name='<add_api>'))
for sid in match_ids:
    apis.<app>.<add_api>(access_token=token, <song_id_param>=sid)
```

## 4. Verify the end state, then finish
Verify against the SKILL.md Queue/playlist adds rule: re-read the queue/library list fully and assert every matched song id is present AND the pre-existing entries are unchanged (same ids, same order) — the mutation is append-only, never a play/replace/reorder. Then call `apis.supervisor.complete_task()` with no `answer` (state-change task).