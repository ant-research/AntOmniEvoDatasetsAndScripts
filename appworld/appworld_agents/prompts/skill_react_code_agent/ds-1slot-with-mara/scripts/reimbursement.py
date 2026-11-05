'''Deterministic helpers for reimbursement/split-cost tasks.

Notes store per-person shares as one `- <name> => $<amount>` line per person;
contact records store each person's email and payment transactions record who
paid. Never retype amounts or emails from printed output: parse the note,
select the already-received payments, and resolve recipient emails with these
helpers, keeping the results in variables.

    exec((SKILL_DIR / 'scripts/reimbursement.py').read_text())
    shares = parse_shares(note['content'])                    # {'Adrian': 36.0, ...}
    paid = received_payments(transactions, 'Friends Dinner', my_email)
    emails = resolve_emails(contacts, [n for n in shares if n not in paid])
'''

import re


def parse_shares(content):
    '''Parse one `- <name> => $<amount>` line per person into {name: amount}.

    Accepts an optional leading `$` and an optional decimal part. Raises
    ValueError when no line matches, so an empty mapping is never treated as
    an empty owed list.
    '''
    shares = {}
    line_re = re.compile(
        r'^\s*-\s*([A-Za-z][A-Za-z .\'-]*?)\s*=>\s*\$?(\d+(?:\.\d+)?)\s*$'
    )
    for line in str(content).splitlines():
        match = line_re.match(line)
        if match:
            shares[match.group(1).strip()] = float(match.group(2))
    if not shares:
        raise ValueError('no share lines of the form - NAME => $AMOUNT found')
    return shares


def received_payments(transactions, description, own_email):
    '''Return {first_name: amount} for money you received for a purpose.

    Selects transactions whose receiver email equals own_email AND whose
    description equals `description` case-insensitively, then maps each
    sender's first name to the amount. Matching the description - the same
    string used on the follow-up requests - is the only reliable selector:
    a phrase like 'already sent me their share' supplies no date/time cutoff,
    so a created_at window silently drops an early same-event payment and an
    extra request is then created. Raises ValueError when nothing matches.
    '''
    paid = {}
    for t in transactions:
        if (t.get('receiver', {}).get('email') == own_email
                and str(t.get('description', '')).strip().lower()
                == str(description).strip().lower()):
            name = str(t.get('sender', {}).get('name', '')).strip().split()[0]
            paid[name] = float(t['amount'])
    if not paid:
        raise ValueError(
            'no received transactions match description ' + repr(description)
        )
    return paid


def resolve_emails(contacts, names):
    '''Map first names to contact emails, asserting exactly one match each.

    contacts: a fully fetched contact list. names: the first names to resolve
    to emails. Each name must match exactly one contact; an ambiguous or
    missing name raises AssertionError so a wrong recipient can never be
    requested silently.
    '''
    emails = {}
    for name in names:
        matches = [c for c in contacts if c.get('first_name') == name]
        assert len(matches) == 1, (
            'expected exactly 1 contact named ' + repr(name)
            + ', got ' + str(len(matches))
        )
        emails[name] = matches[0]['email']
    return emails