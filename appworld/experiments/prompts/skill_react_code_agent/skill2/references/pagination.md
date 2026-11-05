# Pagination Pattern for List APIs

All list/search APIs in AppWorld return at most `page_limit` results per call.
The default page size is less than 20, so omitting `page_limit` silently truncates results.

## Why Pagination Matters

AppWorld paginated APIs return a maximum of 20 items per call. The default page size
is smaller than 20. If you call a list API without paginating:
- You get only the first page of results.
- Remaining items are silently omitted — no error, no warning.
- Any count, ranking, or comparison built on partial data will be wrong.

The default page size is deliberately small to make pagination errors invisible —
the first page often looks complete but isn't.

## How to Paginate

Load and use the `get_all_pages` helper from `scripts/pagination.py`:

```python
exec((SKILL_DIR / 'scripts/pagination.py').read_text())
songs = get_all_pages(apis.spotify.show_song_library, access_token=token)
```

Never call a list API directly — always wrap in `get_all_pages`.

## When to Use

Apply `get_all_pages` to every API that:
- Is named `show_*`, `search_*`, `list_*`, or `get_*` and returns a list
- Accepts `page_limit` and `page_index` parameters

Even if the first page returns fewer than 20 results, always use the loop pattern —
result counts can change across tasks and the cost of looping is negligible for a single page.

## Common Mistakes

```python
# WRONG — omits page_limit/page_index, gets only the small default page
results = apis.venmo.show_social_feed(access_token=token)

# WRONG — single page_index=0 call, silently misses items on later pages
results = apis.venmo.show_social_feed(access_token=token, page_limit=20, page_index=0)

# CORRECT — uses the helper to loop through all pages
exec((SKILL_DIR / 'scripts/pagination.py').read_text())
results = get_all_pages(apis.venmo.show_social_feed, access_token=token)
```