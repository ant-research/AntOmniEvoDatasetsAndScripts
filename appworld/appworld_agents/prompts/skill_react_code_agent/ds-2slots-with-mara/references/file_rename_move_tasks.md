# File Rename & Relocate Tasks (bulk rename by attribute + move a dated subset)

Use for state-change tasks of the form 'add a <prefix or suffix> (e.g. the date string "YYYY_MM_DD-") to every file name in a directory, based on each file's creation date, and then move the files that fail a date predicate (e.g. "not from this year") to another directory.' Also use for either half alone: a pure bulk rename (prefix/suffix on every file) or a pure move of the stale dated subset. For grouping files into SUB-DIRECTORIES while preserving their original names, route to `references/file_organization.md` instead.

## 1. Resolve the reference date and list every file
Get the current date/time from `datetime.now()` or the phone/clock API — never hardcode the year. Derive the task period from it ('this year' = the clock's current year). List the directory with `entry_type='files'` (non-recursive unless the task names sub-directories) and keep the full path list.

## 2. Fetch the per-file attribute before mutating anything
The rename and the move predicate both depend on each file's own creation date. Read each file's detail/show API once per path to collect `{path: created_at}` BEFORE any rename or move, so no mutation can overwrite the date both decisions rely on.

## 3. Rename EVERY file in one pass, before any relocation
The prefix/suffix is computed from each file's OWN `created_at`. Apply it to every file in the directory — 'all file names' means the entire directory, including the files that will later be moved away. Derive the prefix from the task's LITERAL format string, copying every separator character exactly as written: map `YYYY` -> `%Y`, `MM` -> `%m`, `DD` -> `%d`, and keep every other character (underscores between fields, a trailing `-` or `_` before the basename) exactly where the task writes it. The literal "YYYY_MM_DD-" maps to `strftime('%Y_%m_%d-')` -> "2023_05_18-<basename>", while "YYYY-MM-DD_" maps to `strftime('%Y-%m-%d_')` -> "2023-05-18_<basename>". Rename via the move/rename API (destination = same directory + prefix/suffix + original basename) and pass the flag that keeps the file's dates (`retain_dates=True`).

```python
for path, created in file_info.items():
    dt = datetime.fromisoformat(created)
    parent, sep, basename = path.rpartition('/')
    prefix = dt.strftime(<fmt mapped from the task's literal>)  # "YYYY_MM_DD-" -> '%Y_%m_%d-'
    new_path = parent + sep + f"{prefix}{basename}"
    apis.<file_app>.<move_api>(source_file_path=path, destination_file_path=new_path,
        access_token=token, retain_dates=True)
```

Split parent from basename with `str.rpartition('/')` — never import `os`/`shutil`, which are disabled in the REPL.

## 4. Then move the failing subset with its renamed name
Decide the subset from the ORIGINAL creation date recorded in step 2 (e.g. 'not from this year' -> `dt.year != this_year`). Create the destination directory first if needed (`recursive`/`allow_if_exists` as its doc allows). Move each member with destination = destination directory + the ALREADY-RENAMED basename, again passing `retain_dates=True`. The rename and the move compose — a file that is moved is still renamed first.

## 5. Verify both directories, then finish
Re-list the source and destination directories and verify against the NEW (renamed) paths recorded in steps 3-4, never the pre-rename names. Source: every remaining file carries the prefix/suffix, no old (unprefixed) name remains, and the remaining set equals exactly the this-year files (remaining count + moved count = original total). Destination: it may already hold pre-existing out-of-scope files when the task starts — leave them byte-identical (never rename, move, or delete them) and do not assert the destination contains only your moved subset. Assert instead that every basename you moved is present in the destination and carries its prefix. Verify each renamed basename matches a regex built from the task's LITERAL separators, not a format remembered from elsewhere: map `YYYY` -> `\d{4}`, `MM` -> `\d{2}`, `DD` -> `\d{2}` and copy every separator exactly ("YYYY_MM_DD-" -> `^\d{4}_\d{2}_\d{2}-`). A regex built from the same wrong template as the rename is self-consistent and falsely passes, so confirm the separators word-for-word against the task text first. Only then call `apis.supervisor.complete_task()` with no `answer` (state-change task).