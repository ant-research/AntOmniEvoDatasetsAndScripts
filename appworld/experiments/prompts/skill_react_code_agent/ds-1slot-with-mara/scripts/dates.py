'''Oldest/newest record selection that compares parsed datetimes, not strings.

Raw date strings are only ordered correctly by `<`/`>`/`sorted` when every
value shares the exact same format, precision and offset. AppWorld detail
fields are ISO `YYYY-MM-DDTHH:MM:SS`, but selection must not depend on that:
a mixed format (trailing offset, missing zero-padding) silently mis-orders
records and returns the wrong member. Parse each value with
`datetime.datetime.fromisoformat` (module form, per the time-shift rule),
then pick the minimum (oldest) or maximum (newest).

Load when a question or action selects the newest/oldest
(latest/earliest/most-recent) record by a date field, then call it on any
fetched record list:

    exec((SKILL_DIR / 'scripts/dates.py').read_text())
    rows = ...  # any list of per-item detail dicts fetched via get_all_pages
    oldest = oldest_by_field(rows, 'release_date')
    newest = newest_by_field(rows, 'created_at')

`records` is any list of dicts and `date_field` is the record key holding the
date, e.g. 'release_date' for a released question or 'created_at'/'added_at'
for lifecycle timestamps.
'''
import datetime


def _parse(value):
    if isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    return value


def oldest_by_field(records, date_field):
    '''Return the record with the smallest parsed date_field value.'''
    return min(records, key=lambda record: _parse(record[date_field]))


def newest_by_field(records, date_field):
    '''Return the record with the largest parsed date_field value.'''
    return max(records, key=lambda record: _parse(record[date_field]))