# People-by-Relationship Lookup

Use when a task names people by relationship (coworker, friend, manager, family, roommate, ...) and you must find those people or act on another app's records whose sender/receiver/owner matches them. A truncated or wrongly-sourced people list silently drops the people you must act on, so enumerate EVERYONE with full pagination — and resolve relationships from the contact app, never the target app's own social graph.

## 1. Confirm the search API's paging before calling it
Print the people app's contact-search API doc and read its `page_index`/`page_limit`. Contact search usually defaults to a small page_limit (commonly 5) and caps near 20: an unpaginated call returns only the first page and silently drops the rest.

    print(apis.api_docs.show_api_doc(app_name='<people_app>', api_name='search_contacts'))

## 2. Use the contact app, not the target app's social graph
A relationship word means the CONTACT-BOOK relationship, not the target app's own friend/follower list (e.g. a payments app's `search_friends`), which counts a different set. Call the people app's relationship-listing API (see its docs for the exact name, e.g. `show_contact_relationships`) and use the returned strings verbatim as the `relationship` argument; never retype relationship names from the task wording.

    <relationship_names> = apis.<people_app>.show_contact_relationships(access_token=<people_token>)

## 3. Fully paginate the search for EVERY relationship the task names, then union
Run the fully-paginated search ONCE PER relationship the task names and UNION the results into one deduped identity set ('friends and roommates' means `set(friend_emails) | set(roommate_emails)` — union, never an intersection, and never just one of the groups). Loop `page_index` from 0 with ONE explicit `page_limit` taken from the doc (never the default, never changing between pages), aggregating until a page returns fewer than `page_limit` items or is empty:

```python
def search_people_all(relationship, limit):
    rows = []
    pi = 0
    while True:
        page = apis.<people_app>.search_contacts(
            access_token=<people_token>,
            relationship=relationship,
            page_index=pi,
            page_limit=limit)
        if not page:
            break
        rows.extend(page)
        if len(page) < limit:
            break
        pi += 1
    return rows

people = {}
for rel in <relationships_the_task_names>:
    people[rel] = search_people_all(rel, 20)
    print(rel, len(people[rel]))
identity_set = {c['<email_or_phone_key>'] for rows in people.values() for c in rows}
```

## 4. Gate: prove the enumeration is complete
The loop must terminate because a page came back shorter than `page_limit`, not because you stopped early. If any relationship's total is an exact multiple of `limit`, one more page may exist — re-run and re-check before filtering. An incomplete enumeration here is the #1 cause of silently missing people (a missing sender/receiver later fails the exact acted-ids assertion).

## 5. Match the target records against the FULL set
Use the complete identity set to filter the target app's records (sender, receiver, owner). Never filter with a partially fetched list, and never substitute the target app's own social list.

## 6. Verify against an independently re-derived expectation
After mutating, re-query the target app and re-derive the expected ids from the contact-resolved identity set and the task's other predicates (date window, status) — do NOT just re-check the set you already mutated, which cannot catch a wrong people source or a truncated enumeration. Assert the acted ids equal the expected ids, fix any mismatch, then call `apis.supervisor.complete_task()`.

## 7. Filter people by whether they have an account in a second app (per-user profile API, never a search API)
When the task acts on the relationship-resolved people who DO or DO NOT have an account in a second app ('message my roommates who do not have a venmo account', 'email the contacts without a splitwise account'), test each person with the second app's per-user profile/show API keyed by their unique identifier (email for most apps; phone_number for phone apps) instead of that app's fuzzy search API.

1. Print the second app's per-user profile API doc first: `print(apis.api_docs.show_api_doc(app_name='<app2>', api_name='<profile_api>'))`; read its identifier parameter and what a missing user returns.
2. Call it once per person with their exact identifier from section 3. A successful result carrying that identifier means the person HAS an account; a not-found failure (e.g. 422 'Account for this email does not exist') means NO account.
3. Keep only the group the task names (has-account or no-account) and act on exactly those identifiers (send the message/email, etc.), then verify the acted set as in step 6.

Never test existence with `search_users`/`search_*`: those are fuzzy, top-N, and serve an unpaginated default page, so the person's exact identifier is routinely absent from the returned rows even when the account exists. Treating 'not in the search results' as 'no account' misclassifies people who have accounts, and an over-broad fuzzy match misclassifies them the other way.

## 8. Given a specific name (not a relationship): paginate and exact-match the contact search
Use when the task names ONE person by name ('send $N to Robert', 'email Sam about the lease', 'pay Ashley back'), so section 3's per-relationship enumeration does not apply. A name query returns fuzzy, unrelated rows alongside the real match, and contact search usually defaults to a small page_limit (commonly 5) — a single unpaginated call both mixes in decoys and silently drops same-name contacts that fall beyond the first page.

1. Print the contact-search API doc first and copy its page_limit cap.
2. Fetch search_contacts(query=<name>) fully paginated, reusing ONE page_limit on every page and stopping only when a page returns fewer than page_limit items or is empty.
3. Keep only contacts whose first_name, last_name, or combined full name equals the given name exactly (case-insensitive; treat hyphens/underscores as spaces, per the SKILL.md Target selection rule). A fuzzy or partial match is NOT a match — never take the first row or a name that merely looks right.
4. Gate on exactly one match. With zero or several matches, re-query with the full name or extra task context before acting; if several contacts share the name, disambiguate with the task's other clues (last name, phone number, email) — never guess.
5. Take that contact's email (or phone_number for phone apps) as the identifier, then proceed with the calling procedure.