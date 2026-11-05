"""Parse structured note/document text into per-record entries and filter them.

Many AppWorld tasks ask the agent to extract a filtered subset from a note or
document written as a title line (a movie / person / expense name) followed by
indented `- key: value` sub-lines (e.g. `- director: David Fincher`). Reading
the printed content and retyping the matching values by eye silently adds or
drops entries and is not reproducible. Use these helpers instead:

    exec((SKILL_DIR / 'scripts/keyed_entries.py').read_text())
    entries = parse_keyed_entries(content)
    titles = filter_keyed_entries(content, 'director', 'David Fincher')

`parse_keyed_entries` turns the text into one dict per record (the title under
'_title' plus one key per sub-line); `filter_keyed_entries` returns the titles
whose named field matches the criterion. Both are generic over apps and tasks:
pass the content string, field name, and criterion value as arguments. See
`parse_keyed_entries` for nested `--- <name>` items (e.g. a note that stores its
own `--- <person>` sub-records) and the reimbursement/split rule for the
`- <name> => $<amount>` share format.
"""

import re

_SUBLINE_RE = re.compile(r'^-\s*(.+?):\s*(.*)$')
_NESTED_RE = re.compile(r'^---\s*(.+)$')


def parse_keyed_entries(content):
    """Turn free text into a list of per-record entry dicts.

    Each entry is a title line (a non-indented line) followed by indented
    `- key: value` sub-lines. The returned dict stores the title under
    '_title' plus one key per sub-line, with the value stripped. Blank lines
    and heading lines starting with '#' are skipped, and an indented
    `--- <name>` line nests sub-lines under `<name>` (its value becomes its
    own dict, so callers can read `entry['<name>']['<key>']`). Keys and
    values keep their original casing; callers/filter_keyed_entries normalize
    case when matching.
    """
    if not isinstance(content, str):
        raise TypeError(
            'parse_keyed_entries expected a string, got ' +
            type(content).__name__
        )
    entries = []
    current = None
    slot = None
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = _SUBLINE_RE.match(line)
        if match and current is not None:
            key = match.group(1).strip()
            value = match.group(2).strip()
            if slot is not None:
                slot.setdefault(key, value)
            else:
                current[key] = value
            continue
        nested = _NESTED_RE.match(line)
        if nested and current is not None:
            slot = {}
            current[nested.group(1).strip()] = slot
            continue
        current = {'_title': line}
        slot = None
        entries.append(current)
    return entries


def filter_keyed_entries(content, field, value, value_matcher=None):
    """Return the titles of entries whose `field` matches `value`.

    Parses `content` with parse_keyed_entries and keeps every entry where
    `entry[field]` matches the criterion. The default matcher is
    case-insensitive equality on the stripped field value; pass
    `value_matcher=lambda field_value, value: bool(...)` for another criterion
    (e.g. substring containment for a multi-value field such as a genre list).
    """
    matcher = value_matcher or (
        lambda fv, v: fv.strip().lower() == v.strip().lower()
    )
    return [
        entry['_title']
        for entry in parse_keyed_entries(content)
        if field in entry and matcher(entry[field], value)
    ]