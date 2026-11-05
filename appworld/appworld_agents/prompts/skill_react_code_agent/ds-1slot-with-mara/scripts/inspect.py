"""Safe, non-flooding inspection of AppWorld list results.

get_all_pages returns full lists. Never print such a list whole with a manual
for-loop (`for r in rows: print(r)`): a dump of dozens or hundreds of records
floods the context and causes a repetition loop over initialization steps
instead of proceeding to filter and mutate. Use the helpers below instead.

    exec((SKILL_DIR / 'scripts/inspect.py').read_text())
    rows = get_all_pages(apis.<app>.<list_api>, access_token=token)
    preview(rows)
    matches = select_records(rows, lambda r: r['<field>'] == '<value>')"""


def preview(records, n=3):
    """Print a bounded preview of a fetched collection: the total count plus
    the first `n` records, then return the collection unchanged.

    Use this instead of a `for r in records: print(r)` loop to learn the
    record schema — a full dump floods the context and triggers an
    initialization repetition loop.
    """
    if not isinstance(records, list):
        raise TypeError(
            'preview expected a list, got ' + type(records).__name__
        )
    print('count =', len(records))
    for record in records[:n]:
        print(record)
    return records


def select_records(records, predicate, label='matches', show=5):
    """Filter `records` in memory with `predicate`, printing only the matches.

    Prints the number of matches and at most `show` of them, then returns all
    matches so the caller can keep processing. Match subsets are usually small;
    unlike a full input dump this does not flood the context.
    """
    if not isinstance(records, list):
        raise TypeError(
            'select_records expected a list, got ' + type(records).__name__
        )
    matches = [record for record in records if predicate(record)]
    print(label + ': ' + str(len(matches)))
    for record in matches[:show]:
        print(record)
    return matches


def resolve_one(records, predicate, describe=None):
    """Resolve a descriptor to exactly one record, or fail loudly.

    Filter `records` in memory with `predicate`. Print the match count and,
    when it is not exactly one, a numbered sampling of the matched records
    (rendered with `describe(record)` for a compact identifier) so a
    multi-match can be disambiguated. Assert exactly one match and return
    that record, so every identifier and field comes from the returned dict
    and is never re-typed from printed output. Use it for any named
    descriptor — a contact name, a note title, an alarm label — instead of
    taking a positional first hit inside an unfiltered result:

        match = resolve_one(
            contacts,
            lambda c: (c.get('first_name') or '').lower() == '<first_name>',
            describe=lambda c: (c.get('first_name'), c.get('last_name')))
        receiver_email = match['email']
    """
    if not isinstance(records, list):
        raise TypeError(
            'resolve_one expected a list, got ' + type(records).__name__
        )
    view = describe or repr
    matches = [record for record in records if predicate(record)]
    print('resolve_one matches: ' + str(len(matches)))
    for i, record in enumerate(matches[:10], 1):
        print('  ' + str(i) + '. ' + str(view(record)))
    assert len(matches) == 1, (
        'resolve_one expected exactly 1 match, got ' + str(len(matches))
    )
    return matches[0]


def id_of(records, predicate, id_field, describe=None):
    """Return the identifier field of the single record matching `predicate`.

    Wrap resolve_one so the id is derived from the stored record in one
    expression and assigned to a variable that follow-up API calls reference,
    instead of a literal retyped from printed output.

        detail_id = id_of(rows, lambda r: r['label'] == '<name>', '<id field>')
        items = get_all_pages(apis.<app>.<list_api>, <id field>=detail_id)

    A literal copied from a printed record (e.g. <id field>=3) is exactly the
    failure this wrapper prevents: a transcription slip silently targets the
    wrong record. Assert exactly one match via resolve_one and that the record
    carries `id_field`.
    """
    record = resolve_one(records, predicate, describe=describe)
    assert id_field in record, (
        'id_of: record has no field ' + repr(id_field) + '; keys: '
        + repr(sorted(record.keys()))
    )
    return record[id_field]