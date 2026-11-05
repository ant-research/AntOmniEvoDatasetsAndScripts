# File & Directory Organization Tasks

Use for tasks of the form 'organize/arrange/move the files in <directory> into sub-directories grouped by a file attribute (creation date, type), maintaining the original file names.'

## 1. Resolve the bucket qualifiers from the task wording
Get the current date/time from the clock/phone API. Map each named period to its meaningful parts: when the task says 'this year'/'this month', a bucket is (year, month); the catch-all bucket ('the others'/'the rest'/'everything else') receives every file NOT in a named bucket, including files from the same month of a different year. Do not match a named month bucket by month alone when a year qualifier is present — April of a prior year belongs to the catch-all, not the 'April' bucket.

## 2. List the directory, then fetch the grouping attribute per file
A directory-listing API usually returns only entry paths (names), not the attribute you group by. List the directory (paginate with `page_index`/`page_limit` if the listing doc exposes them), then call the per-file detail/show API once per path to read each file's `created_at` (or the relevant attribute) and store `{path: attribute}`. Do not infer a file's date from its name or list position.

## 3. Classify by the full date, not a month substring
Compare year AND month against the resolved buckets, keep the explicit else branch as the catch-all, and run the classifier script so the current year is read from the environment clock inside the function instead of being retyped as a literal:

```python
exec((SKILL_DIR / 'scripts/file_bucket.py').read_text())
buckets = bucket_files_by_month(file_info, {<month_a>: '<bucket_a>', <month_b>: '<bucket_b>'}, catch_all='<catch_all>')
```

`bucket_files_by_month` binds `this_year = datetime.now().year` internally (never a literal), keeps a file only when `dt.year == this_year` and its month is one of the mapped months, and routes every other file -- including a same-month file from a PRIOR year -- to the catch-all. Adapt only the month->bucket mapping and the catch-all name to the task; never re-type the current year into the call or the mapping, and never re-implement the loop by hand.

## 4. Create targets and move each file preserving its basename
Create each target sub-directory inside the source directory (create-directory API; use `recursive`/`allow_if_exists` as its doc allows). Move each file with `destination = f'{source_dir}/{bucket}/{basename}'`, preserving the original name by reusing its basename, and pass the flag that keeps the file's dates (e.g. `retain_dates=True`) so the attribute the buckets were built from is not overwritten by the move. Do not rename or re-case files.

## 5. Verify and finish
Re-list the source directory and every destination directory; assert every file appears in exactly one sub-directory under its original basename and the counts sum to the original total (the source directory retains no stray files). Only then call `apis.supervisor.complete_task()`.