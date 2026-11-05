'''Derive calendar dates for relative-time phrases from the environment clock.

AppWorld simulates a frozen clock, so the task's 'today' can differ from the
sandbox wall clock. Query the environment clock through the passed callable,
parse the returned date, and derive relative dates in code. Never transcribe
the date printed by a clock lookup into a hardcoded datetime.date(...) literal:
a transcription mistake or a different simulated date silently selects the
wrong records.

Usage:
    exec((SKILL_DIR / 'scripts/relative_dates.py').read_text())
    today, yesterday = today_and_yesterday(apis.phone.get_current_date_and_time)
    day_bounds = {today.isoformat(), yesterday.isoformat()}
    matches = select_records(rows, lambda r: r['created_at'][:10] in day_bounds)
'''

import datetime
import json
import re

_MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11,
    'december': 12,
}


def _parse_date(value):
    '''Parse a clock date string into datetime.date.

    Handles the common AppWorld clock format 'Thursday, May 18, 2023' and
    ISO '2023-05-18'; raises a clear error otherwise.
    '''
    text = str(value).strip()
    match = re.search(r'([A-Za-z]+) +(\d{1,2}), +(\d{4})', text)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month is None:
            raise ValueError('Unknown month name in clock date: ' + text)
        return datetime.date(int(match.group(3)), month, int(match.group(2)))
    try:
        return datetime.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            'Cannot parse environment clock date ' + repr(text)
        ) from exc


def today_and_yesterday(date_time_api):
    '''Return (today, yesterday) as datetime.date from the environment clock.

    date_time_api: a zero-argument callable returning the clock lookup, e.g.
        apis.phone.get_current_date_and_time. It may return a dict such as
        {'date': 'Thursday, May 18, 2023', 'time': '09:08 PM'} or a JSON
        string of the same shape.
    '''
    resp = date_time_api()
    if isinstance(resp, str):
        resp = json.loads(resp)
    today = _parse_date(resp['date'])
    return today, today - datetime.timedelta(days=1)


def current_year(date_time_api):
    '''Return the simulated current year (int) from the environment clock.

    Use for a 'this year' or a named '<Month> of this year' predicate instead
    of typing a 4-digit year literal: derive the year from the environment
    clock so the predicate keeps working when the simulated year differs from
    the literal, which silently selects the wrong records.

        this_year = current_year(apis.phone.get_current_date_and_time)
        named = [r for r in records
                 if datetime.datetime.fromisoformat(r['<date field>']).year
                 == this_year
                 and datetime.datetime.fromisoformat(r['<date field>']).month == 3]
    '''
    today, _ = today_and_yesterday(date_time_api)
    return today.year


def filter_by_year(records, date_field, date_time_api, predicate=None):
    '''Select records whose date_field falls in the simulated current year,
    optionally AND-ed with a second criterion.

    Derives the current year from the environment clock through the passed
    date_time_api callable, then returns every record whose date_field string
    starts with that year and, when `predicate` is given, for which
    `predicate(record)` is true. Use this for "this year" / "released in this
    year" phrases — alone or combined with a second criterion such as a genre
    — instead of writing a year literal: the sandbox wall-clock year can
    differ from the task's simulated year, and a hardcoded 4-digit year
    silently selects the wrong records when the simulated date is a different
    year.

        rows = get_all_pages(apis.<app>.<list_api>, access_token=token)
        matches = filter_by_year(
            rows, 'release_date', apis.phone.get_current_date_and_time,
            predicate=lambda r: r['<other field>'] == '<value>')
    '''
    today, _ = today_and_yesterday(date_time_api)
    year = str(today.year)
    matches = [record for record in records
               if str(record[date_field])[:4] == year
               and (predicate is None or predicate(record))]
    print('filter_by_year: derived current year', year,
          '-', len(matches), 'matches')
    return matches


def since_month_day_this_year(date_time_api, month, day):
    """Return the ISO-8601 lower bound for 'since <Month> <day> of this year'
    as a datetime string at midnight of that calendar day.

    Derives the year from the environment clock so no 4-digit year literal is
    ever typed into a filter. A hardcoded year (e.g. writing the year observed
    in the clock lookup) silently selects the wrong records when the simulated
    year differs; matching only the month prefix drops later months. month and
    day are integers (month=3, day=1 for '1st Mar'). 'since' is inclusive: a
    record stamped on the boundary day itself qualifies.

    Usage:
        start = since_month_day_this_year(
            apis.phone.get_current_date_and_time, 3, 1)
        matches = [r for r in rows if r['<created field>'] >= start]
    """
    today, _ = today_and_yesterday(date_time_api)
    return datetime.datetime(today.year, month, day, 0, 0, 0).isoformat()


def month_bounds(date_time_api, months_ago=0):
    """Return (start, end) date-only ISO strings ('YYYY-MM-DD') covering the
    simulated month `months_ago` months before the current month (0 = this
    month, 1 = last month).

    Derive the year and month from the environment clock through the passed
    date_time_api callable, so the bounds carry no hardcoded 4-digit year or
    month literal. `start` is the first day of the month and `end` is its
    last day, both inclusive. Pass them directly as a server-side date-filter
    pair (e.g. min_created_at/max_created_at), compare the date portion of a
    full timestamp in memory with
    `start <= str(row['<date field>'])[:10] <= end`, or take `start[:7]` as
    the derived `YYYY-MM` prefix for in-memory prefix matching. A hand-typed
    month/year window silently selects the wrong records (or an empty window)
    whenever the simulated 'today' is a different month or year, so never
    type the bounds by hand.

        start, end = month_bounds(apis.phone.get_current_date_and_time)
        rows = get_all_pages(apis.<app>.<list_api>, access_token=token,
                             min_created_at=start, max_created_at=end)
        matching = [r for r in rows
                    if start <= str(r['<date field>'])[:10] <= end]
    """
    import calendar
    today, _ = today_and_yesterday(date_time_api)
    total = today.year * 12 + (today.month - 1) - months_ago
    first = datetime.date(total // 12, total % 12 + 1, 1)
    last_day = calendar.monthrange(first.year, first.month)[1]
    return (
        first.strftime('%Y-%m-%d'),
        datetime.date(first.year, first.month, last_day).strftime('%Y-%m-%d'),
    )