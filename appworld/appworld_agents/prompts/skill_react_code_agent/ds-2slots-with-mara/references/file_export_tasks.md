# Export / Backup Tasks (write a delimited file from app collections)

Use for tasks of the form 'export/back up a list of every <entity> in my <library>[, my <other collections>, and all my <playlists/lists>] into "<path>"' — the final artifact is a delimited file (e.g. CSV) written through the file_system app, possibly followed by an account deletion/termination. Also use for 'export every <entity> as its own file in a directory, named from the entity title with a task-stated extension' (one file per entity, e.g. one .md per note): for that artifact follow section 8 plus sections 1-3; the single-delimited-file steps 4-7 do NOT apply.

## 1. Resolve the destination path against the file_system app, don't assume `~`
Never assume `~` resolves to the sandbox host's OS home (`/home/admin`). Before writing, discover how the file_system app represents the user's home with ONE call and keep the result in a variable — `show_directory` returns the ENTIRE recursive tree (hundreds of entries), so a second call on `/home` is redundant and printing the raw listing floods context. Derive the home directory as `/home/<first_name lowercased>/` from `show_profile()['first_name']` (fall back to the last-name and first+last variants if none matches) and print only that one entry:

```python
prof = apis.supervisor.show_profile()
listing = apis.file_system.show_directory(directory_path='/', access_token=<fs_token>)
print('listing size:', len(listing))
home = next((p for p in listing
             if p.lower().startswith('/home/') and p.rstrip('/').split('/')[-1] == prof['first_name'].lower()),
            None)
print('home:', home)
```

Then follow the app's path convention consistently for every `directory_exists`/`create_directory`/`create_file`/`show_file` call:

- If the app stores and reports paths with a literal `~` prefix (a `show_file` on a tilde path round-trips `~/...` as `~/...`), pass the task's path VERBATIM including `~/`. Do not strip the `~`, do not prepend `/`, and do not rewrite it as a bare relative path.
- If the app stores absolute paths, expand `~/` to the discovered home (`/home/sabrina/...`) and keep the rest of the path unchanged.

The evaluator compares the recorded path against the requested path (e.g. `.tilde_path == '~/backups/spotify.csv'` or an absolute `backup_file_path`), so any rewrite — dropping `~/`, resolving to `/home/admin`, or using a `~`-less relative path — fails the check.

## 2. Create the parent directory if missing
From the destination path (convention resolved above) derive the parent directory. `directory_exists` first, then `create_directory(directory_path=..., recursive=True, allow_if_exists=True)` when absent. Checking a different variant (e.g. `backups` instead of `~/backups`) targets a different directory.

## 3. Enumerate every named collection, fully paginated
Call the matching library/list API for EACH collection the task names (library, album/folder library, all playlists/lists), each fully paginated (loop `page_index` from 0, reuse one `page_limit`, stop when a page returns fewer than `page_limit` items). Skipping a named collection silently drops records; collection summaries often expose only member ids, not the member fields the output needs.

## 4. Expand nested members, normalize the id key, and deduplicate
Union every entity id from every source: each library/list row's `<entity>_id` plus every id inside each summary's nested member-id list (e.g. album/playlist `song_ids`). Library rows usually key the id as `<entity>_id` while detail-embedded member rows key it as `id` — normalize both to one key, then deduplicate by id (the same entity recurs across collections):

```python
unique = {}
for src_group in [library_rows, parent_a_members, parent_b_members]:
    for row in src_group:
        eid = row.get('<entity>_id', row.get('id'))
        if eid is None or '<title_key>' not in row:
            continue
        unique.setdefault(eid, {'title': row['<title_key>'],
                                'artists': [a['<name_key>'] for a in row['<artists_key>']]})
```

## 5. Fetch missing fields per unique entity and cross-check completeness
Collection summaries often omit fields a row needs (title, artist names). For each unique id whose required fields are missing, call the per-entity detail API once and store the result keyed by id. Before writing, build the expected id set as the union of every member id exposed anywhere and assert it equals `set(unique)`:

```python
expected = set(lib['<entity>_id'] for lib in library_rows)
for summary in other_summaries:
    expected |= set(summary.get('<member_ids_key>', []))
assert set(unique) == expected, ('mismatch', sorted(set(unique) ^ expected))
```

A mismatch means a named collection or member list was dropped or double-counted.

## 6. Write rows as raw delimited text — no quoting, deterministic ordering
Evaluators read the file back and split each line on the single separator (e.g. `line.split(',')` and a multi-value `|`); they do NOT parse CSV quoting and may treat a multi-valued cell as an ordered sequence. So:

- Put NO surrounding quote characters on any value — a `"` you write stays attached to the compared value (normalize_text lowercases and trims whitespace, it does not strip quotes).
- Do not import or use the `csv` module — `csv.writer` / `csv.DictWriter` raise "Usage of the following function is not allowed".
- Sort multi-valued cells (e.g. artists joined by `|`) alphabetically, case-insensitive, before joining, so an order-sensitive comparison matches canonical ground truth and a set-based comparison is unaffected.
- First line is the header verbatim from the task; join fields with the exact delimiter; no trailing delimiter; one row per unique entity.

```python
header = '<header from the task verbatim>'   # e.g. 'Title,Artists'
rows = [header]
for entity in sorted(unique.values(), key=lambda e: e['title'].lower()):
    field2 = '<multi_sep>'.join(sorted(entity['artists'], key=str.lower))
    rows.append(entity['title'] + '<delimiter>' + field2)
content = '\n'.join(rows) + '\n'
```

## 7. Write, verify, and order destructive actions last
`create_file(file_path=<resolved path>, content=..., overwrite=True)`, then `show_file` it and confirm the header line matches, the number of data lines equals the number of unique entities, and no title appears twice. Perform the backup and its verification BEFORE any account deletion/termination the task also asks; re-read the file once more after the deletion to prove the backup survived, then call `complete_task()`.

## 8. One file per entity: export each note/item as its own file named from its title

Use when the task exports every entity as its OWN file in a directory with a task-stated extension and a filename derived from the entity's title (e.g. 'Export all my Simple Note notes to the ~/backups/notes/ directory; name each file by its note title, replacing white space with _ and using a .md extension'). The artifact is N files — one per entity — NOT one joined/header file and NOT a CSV.

1. Enumerate the named collection fully (section 3) and keep each entity's title plus its FULL content string. When a list row omits the content, fetch it once per entity id from the per-entity detail/show API.
2. Derive each filename from the entity's title: replace whitespace with _ (title.replace(' ', '_')), keep every other character verbatim (parentheses, brackets, commas, apostrophes, =), and append the task-stated extension. Preserve the title's original casing; do not quote, truncate, or prefix a header.
3. Write one create_file per entity at <resolved parent dir>/<fname> with content=<that entity's full content string>, overwrite=True. A separate call per entity is required — never join all entities into one delimited file with a header line.
4. Verify, then finish: re-list the directory and assert the written file count equals the number of unique entities, each filename equals its title-derived name, and show_file content equals the entity's full content. Then call apis.supervisor.complete_task() with no answer (state-change task).