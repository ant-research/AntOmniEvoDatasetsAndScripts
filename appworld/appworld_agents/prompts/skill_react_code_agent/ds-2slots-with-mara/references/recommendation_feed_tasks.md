# Building a New Collection from a Recommendation / Discovery Feed

Use for state-change tasks of the form 'add all <app>-recommended / recommended-for-you / discover <genre-or-category> <entities> [released in/from <period>] to a new <collection> called <title>' - the source is the app's personalized recommendation/discovery feed, the predicate (genre/category and/or the entity's own date) is evaluated per entity, and the output is a brand-new collection created with the task's exact title. Use the same procedure when only one predicate half is present (genre-only or date-only).

## 1. Resolve the source as the recommendation feed, not a library list or a name search

'<app>-recommended' / 'recommended for you' / 'discover' means the app's personalized recommendation feed, never the user's own library list, an artist/entity name search, or a guessed 'for you' set. Discover the feed endpoint's exact name first: list the app's APIs and pick the endpoint whose description names personalized recommendations (commonly a `show_recommendations`-style API). Print its doc - it is user-scoped (takes `access_token`), paginated (default `page_limit` usually 5, cap around 20), and lists no genre/date filter parameters. Paginate it fully (SKILL.md Procedure step 7: ONE `page_limit` copied from the doc on every page, stop when a page returns fewer than `page_limit` items) and collect every recommended entity id. Substituting the library or a name search silently swaps the candidate population for a different one and fails the evaluator's exact-set assertion.

```python
print(apis.api_docs.show_api_doc(app_name='<app>', api_name='<feed_api>'))
rows = []
pi = 0
page_limit = <cap read from the doc>
while True:
    page = apis.<app>.<feed_api>(access_token=token, page_index=pi, page_limit=page_limit)
    if not page:
        break
    rows.extend(page)
    if len(page) < page_limit:
        break
    pi += 1
```

## 2. Load the genre/category value from the app's enumeration API

For a genre/category predicate, load the valid values from the app's enumeration API (`show_genres`-style) and confirm the task's target appears there verbatim (case-sensitive). Use the enumerated spelling in every comparison (SKILL.md Procedure step 5 and the Filter values rule) - never retype the task wording.

## 3. Evaluate the predicate on per-entity detail, not the feed row

The recommendation feed row typically carries only the entity id/title/album/artist-name and OMITS the entity's genre and its own release date, and the feed API lists no server-side genre/date filters. Fetch the entity's detail once per recommended id (`show_<entity>`-style) and read the genre field and the entity's own release-date field from it. Never infer genre from an artist/album name, and never treat a collection `added_at` as the entity's own `release_date`. (If the feed doc does list a genre/date filter, prefer it and cross-check the filtered ids against the raw rows per the SKILL.md Filtering rule; otherwise per-record detail is required.)

```python
matches = []
for r in rows:
    d = apis.<app>.<show_entity_api>(<entity_id_param>=r['<entity_id_key>'])
    if (d.get('<genre_field>', '') == '<target genre from the enumeration>'
            and datetime.fromisoformat(d['<release_date_field>']).year == datetime.now().year):
        matches.append(d['<entity_id_key>'])
```

'Released in <period>' / 'this year' compares the entity's own date against the current date via the SKILL.md Dates rule: parse with `datetime.fromisoformat(...)` and compare against a naive `datetime.now()` - 'this year' -> `.year == now.year`, never a hardcoded year.

## 4. Create the new collection, add each match once, verify, finish

Print the create and add API docs. Create the collection with the task's EXACT title, loop over `matches`, and call the add API once per matched id (SKILL.md Queue/playlist adds rule: the add API, never a play/replace/reorder API). The collection starts empty, so there are no pre-existing entries to preserve - add every match and only the matches. Re-read the new collection and assert its member ids equal the set derived from the predicate (SKILL.md Procedure step 8 exact-set gate) so a wrong genre spelling or a wrong year is caught before finishing. Call `apis.supervisor.complete_task()` with no `answer` (state-change task).