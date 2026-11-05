# Pagination in AppWorld

## Why Pagination Matters

AppWorld list APIs return **at most 5–20 items per page** depending on the API. If you call a list API without paginating, you only see the first page of results — missing the rest. This causes wrong decisions when you need to act on ALL items or count total results.

## Which APIs Are Paginated

Any API whose API doc includes `page_index` or `page_limit` parameters is paginated — use `paginated_call` for those. Some list-returning APIs do NOT accept pagination parameters (e.g., `file_system.show_directory`, `file_system.show_file`); call those directly — passing them through `paginated_call` injects invalid kwargs and causes a 422 error. When in doubt, check the API doc via `apis.api_docs.show_api_doc(app_name=..., api_name=...)`.

Common paginated APIs across apps:
- `show_song_library`, `show_liked_songs`, `show_downloaded_songs`, `show_album_library`, `show_liked_albums`, `show_playlist_library` (spotify)
- `show_inbox_threads`, `show_sent_emails` (gmail)
- `search_*` APIs across all apps
- Any API whose `api_docs.show_api_doc()` response includes `page_index` or `page_limit` parameters

Common non-paginated list APIs (call directly, do NOT use `paginated_call`):
- `file_system.show_directory`, `file_system.show_file` (file_system)

## How to Paginate

Always use the `paginated_call` helper loaded from `scripts/pagination.py` (ensure `SKILL_DIR` is defined first — see Procedure step 3):

```python
exec((SKILL_DIR / 'scripts/pagination.py').read_text())
all_songs = paginated_call(apis.spotify.show_song_library, access_token=spotify_token)
all_artists = paginated_call(apis.spotify.search_artists, access_token=spotify_token, genre='classical')
# Non-paginated API — call directly, do NOT use paginated_call:
# files = apis.file_system.show_directory(access_token=fs_token, directory_path='~/documents/', recursive=True)
```

Do NOT manually loop pages — manual pagination is error-prone (forgotten pages, wrong `page_limit`, off-by-one errors). `paginated_call` handles all of this automatically.

## sort_by Format

When a paginated API accepts `sort_by`, the value must start with `+` (ascending) or `-` (descending), followed by the field name. Examples: `-created_at` (newest first), `+amount` (smallest first). Values like `created_at_desc` or `created_at` without a prefix cause a 422 error.

## Key Constraints

- Maximum `page_limit` is **20** across all AppWorld APIs. Values above 20 trigger a 422 validation error.
- Some APIs default to a `page_limit` of 5 if not specified. Always set `page_limit=20` for efficiency, or use the helper.
- The stop condition: a page returns fewer items than `page_limit`, or an empty list.

## Common Mistakes

1. Calling a list API without `page_index` — you only get the first page, not all results.
2. Assuming the result count equals the total — the last page may have fewer items.
3. Forgetting to pass required kwargs (like `access_token`) when adding `page_index` and `page_limit`.
4. Using `page_limit > 20` — always stay at or below 20.
5. Using `sort_by` without a `+` or `-` prefix — always prefix with `-` (desc) or `+` (asc).
6. Manually looping pages instead of using `paginated_call` — the helper is less error-prone and sets `page_limit=20` automatically.
7. Printing entire raw results from `paginated_call` when you only need specific fields — large outputs (e.g., search_text_messages returning 30+ full message objects) overwhelm context and cause the agent to lose track. Instead, extract only the fields you need into a list or dict and print the summary: `[(m['text_message_id'], m['sender']['name'], m['message']) for m in results if m['sender']['name'] == 'Leslie Ball']`.
8. Skipping `paginated_call` for non-spotify list APIs (e.g., gmail.show_inbox_threads) — all paginated APIs across all apps must use `paginated_call`, not just music-related ones. Check the API doc for `page_index`/`page_limit` to confirm.
9. Using `paginated_call` for non-paginated list APIs (e.g., `file_system.show_directory`) — if the API doc does NOT include `page_index`/`page_limit`, the API is not paginated; call it directly. `paginated_call` injects these parameters and causes a 422 error on non-paginated APIs.