'''Non-flooding cross-app account-membership checks.

A recurring AppWorld task shape asks which members of a resolved candidate set
have, or do not have, an account on another app ('siblings and roommates who do
not have a venmo account'). Never answer it with a per-candidate free-text
search followed by a print loop: AppWorld list `query` arguments are unreliable
and can return the whole collection, and printing it floods the context and
triggers the initialization repetition loop. Fetch the target app's full user
collection ONCE and check membership in memory. Requires scripts/pagination.py
to already be loaded (Procedure step 2).

    exec((SKILL_DIR / 'scripts/account_check.py').read_text())
    people = <resolve the people group first>   # list of records with 'email'
    membership = account_membership(
        apis.<app>.<search_users_api>, [c['email'] for c in people], <token>)
    lacking = [c for c in people if not membership[c['email']]]  # 'who do not have'
    having = [c for c in people if membership[c['email']]]        # 'who do have'
'''


def account_membership(search_api, emails, access_token=None, email_field='email'):
    '''Return an {email: has_account} dict for each candidate in `emails`,
    derived from ONE full-collection fetch of `search_api` with no query.

    search_api: the target app's user list/search function, e.g.
        apis.venmo.search_users. It is called without a free-text query so it
        returns every user; get_all_pages (scripts/pagination.py) must already
        be loaded so all pages are collected.
    emails: candidate identifiers, e.g. [c['email'] for c in candidates].
    access_token: the target app's access token, or None when its search API
        takes no token (e.g. spotify.search_users).
    email_field: key holding the identifier on each account record.

    Prints one compact 'account exists: <id> -> True/False' line per candidate
    and returns the mapping keyed by the original email string. The fetched
    collection never leaves this function, so the result cannot flood the
    context or trigger the repetition loop.
    '''
    if 'get_all_pages' not in globals():
        raise RuntimeError(
            'get_all_pages is not loaded; run the pagination.py exec from '
            'Procedure step 2 first.')
    kwargs = {} if access_token is None else {'access_token': access_token}
    rows = get_all_pages(search_api, **kwargs)
    have = {str(row.get(email_field)).strip().casefold() for row in rows}
    result = {}
    for email in emails:
        key = str(email).strip().casefold()
        result[email] = key in have
        print('account exists:', email, '->', result[email])
    return result