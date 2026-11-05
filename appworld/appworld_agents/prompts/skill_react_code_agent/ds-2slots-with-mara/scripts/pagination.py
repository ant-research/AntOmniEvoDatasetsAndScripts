"""Full-pagination helper for any AppWorld paginated list/search/feed API.

Usage: define a callable that performs ONE page of a paginated API call, then hand it to
fetch_all(...) together with the page_limit taken from that API's show_api_doc. fetch_all
loops page_index from 0 and STOPS ONLY when a returned page is shorter than page_limit,
which is the documented end-of-data signal. A final safety assert raises if the last page
is exactly full, because that page is overwhelmingly likely to hide more rows on the next
page_index — the #1 cause of a truncated list silently becoming a wrong min/max/sum answer.

The callable receives page_index= and page_limit= as keyword arguments; fold any API-specific
parameters (access_token, query, artist_id, date filters, ...) into the callable's closure.
No app or API name is hardcoded here.
"""


def fetch_all(page_call, page_limit):
    """Aggregate every row of a paginated API by re-calling page_call for each page.

    Args:
        page_call: callable(page_index=int, page_limit=int) -> list of rows for ONE page.
        page_limit: int page size copied from the API's show_api_doc (reused on every page;
                    changing it between pages shifts the offset window and skips rows).
    Returns:
        list of all rows across pages, in order.
    Raises:
        AssertionError if the terminating page is exactly page_limit long (a near-certain
        sign that more rows exist on the next page) — never a page_limit smaller than 1.
    """
    assert isinstance(page_limit, int) and page_limit >= 1, \
        "page_limit must be a positive int copied from show_api_doc"
    rows = []
    page_index = 0
    while True:
        page = page_call(page_index=page_index, page_limit=page_limit)
        page = list(page) if page else []
        rows.extend(page)
        if len(page) < page_limit:
            # A short or empty page is the API's documented end-of-data signal.
            break
        # A page that is exactly full is NOT proof of exhaustion.
        assert page, "empty page returned despite full-length guard"
        page_index += 1
    return rows


# Worked pattern (adapt the callable to the actual API and its doc-listed parameters):
#
#   print(apis.api_docs.show_api_doc(app_name='<app>', api_name='<list_api>'))
#   PAGE_LIMIT = <per-page cap copied from that doc>
#   def one_page(**paging):
#       return apis.<app>.<list_api>(page_index=paging['page_index'],
#                                    page_limit=paging['page_limit'],
#                                    access_token=token,  # only APIs whose doc lists it
#                                    query='<any fixed filter params>')
#   all_rows = fetch_all(one_page, PAGE_LIMIT)
#   print('rows:', len(all_rows))