"""Month/year sub-directory bucketing for file-organization tasks.

Groups an already-fetched {file_path: created_at_iso} mapping into the named
sub-directory buckets of an 'organize files into sub-directories by creation
month' task, plus the task's catch-all bucket ('the others' / 'the rest' /
'everything else'). The current year is read from the frozen environment clock
(datetime.now()) INSIDE this function, so the caller can never hardcode a year
literal -- a hardcoded year routes every 'this year' file into the catch-all
whenever the frozen clock year differs.

Usage (adapt only the month->bucket mapping and the catch-all name):
    exec((SKILL_DIR / 'scripts/file_bucket.py').read_text())
    buckets = bucket_files_by_month(file_info, {<month_a>: '<bucket_a>',
                                                <month_b>: '<bucket_b>'},
                                    catch_all='<catch_all>')

No app, API, directory, or sub-directory name is hardcoded here.
"""


def bucket_files_by_month(file_info, month_to_bucket, catch_all='catch_all'):
    """Split {path: created_at_iso} into named month buckets plus a catch-all.

    A file goes to a month bucket only when its year equals the frozen clock's
    current year AND its month is a key in month_to_bucket; every other file
    (including a same-month file from a prior year) goes to catch_all.
    """
    from datetime import datetime

    this_year = datetime.now().year  # frozen environment clock; never a literal
    buckets = {name: [] for name in list(month_to_bucket.values()) + [catch_all]}
    for path, created in file_info.items():
        dt = datetime.fromisoformat(created)
        if dt.year == this_year and dt.month in month_to_bucket:
            buckets[month_to_bucket[dt.month]].append(path)
        else:
            buckets[catch_all].append(path)
    return buckets