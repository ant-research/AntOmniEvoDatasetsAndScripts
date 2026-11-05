'''Clock-derived payment-card selection for funding a failed send/pay call.

When a send/pay API call fails with an insufficient-balance error, fund the
payment in the SAME call by passing an optional payment_card_id. Pick the card
in code: query the environment clock, keep only the cards whose expiry is not
already past the simulated today, and print the surviving card ids so the
caller can iterate them by id. Never type the simulated date or the expiry
threshold as a literal: a hardcoded year/month matches only the task whose
simulated date it was copied from, and silently keeps an expired card (or
drops a still-valid one) whenever the simulated date differs.

Usage:
    exec((SKILL_DIR / 'scripts/payment_cards.py').read_text())
    cards = apis.<app>.show_payment_cards(access_token=<token>)
    today, valid = valid_payment_cards(
        cards, apis.phone.get_current_date_and_time)
    for card in valid:
        try:
            result = apis.<app>.<send_api>(
                ..., payment_card_id=card['payment_card_id'])
            break
        except Exception as exc:
            print('card', card['payment_card_id'], 'failed:', str(exc)[:200])
'''

import datetime
import json
import re

_MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11,
    'december': 12,
}


def _parse_clock_date(value):
    '''Parse the environment clock's date string into datetime.date.

    Handles the AppWorld clock format 'Thursday, May 18, 2023' and the ISO
    '2023-05-18'; raises a clear error otherwise.
    '''
    text = str(value).strip()
    match = re.search(r'([A-Za-z]+) +(\d{1,2}), +(\d{4})', text)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month is None:
            raise ValueError('Unknown month name in clock date: ' + text)
        return datetime.date(
            int(match.group(3)), month, int(match.group(2)))
    return datetime.date.fromisoformat(text)


def valid_payment_cards(cards, date_time_api):
    '''Return (today, valid_cards): the cards whose expiry is not already past
    the environment clock's simulated today.

    date_time_api: a zero-argument callable returning the clock lookup, e.g.
        apis.phone.get_current_date_and_time. It may return a dict such as
        {'date': 'Thursday, May 18, 2023', 'time': '12:00 PM'} or a JSON
        string of the same shape. A card whose expiry_year/expiry_month equals
        the current month remains valid through that month's end, so it is
        kept; only cards already past the current month are dropped.
    '''
    if not isinstance(cards, list):
        raise TypeError(
            'valid_payment_cards expected a list, got ' +
            type(cards).__name__
        )
    resp = date_time_api()
    if isinstance(resp, str):
        resp = json.loads(resp)
    today = _parse_clock_date(resp['date'])
    valid = [
        c for c in cards
        if (c['expiry_year'], c['expiry_month']) >= (today.year, today.month)
    ]
    print('payment cards: simulated today =', today.isoformat())
    print('valid (not expired) card ids =',
          [c['payment_card_id'] for c in valid])
    return today, valid