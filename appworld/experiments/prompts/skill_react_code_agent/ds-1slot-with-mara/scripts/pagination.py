"""Reusable pagination helper for AppWorld list/search APIs.

Load once per task, then call it on any paginated API:

    exec((SKILL_DIR / 'scripts/pagination.py').read_text())
    records = get_all_pages(apis.<app>.<list_api>, access_token=token, **filters)
"""


def get_all_pages(api_func, page_limit=20, **kwargs):
    """Return every record from a paginated API call as one flat list.

    api_func: the app API function to call (a show_*/search_* endpoint).
    page_limit: records per request. AppWorld list APIs cap this value
        (commonly 20); the helper starts at this low value and, if the API
        rejects it, reads the allowed cap from the error message and retries.
    kwargs: other parameters passed through (access_token, filters, sort_by).

    Stops when a page is empty or shorter than page_limit (the final page).
    """
    results = []
    page_index = 0
    limit = page_limit
    while True:
        try:
            page = api_func(page_index=page_index, page_limit=limit, **kwargs)
        except Exception as exc:
            text = str(exc)
            marker = "less than or equal to "
            if "page_limit" in text and marker in text:
                digits = ""
                for ch in text[text.find(marker) + len(marker):]:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if not digits:
                    raise ValueError(
                        "page_limit rejected but no numeric cap found in: " + text
                    ) from exc
                limit = int(digits)
                page = api_func(page_index=page_index, page_limit=limit, **kwargs)
            else:
                raise
        if not page:
            break
        results.extend(page)
        if len(page) < limit:
            break
        page_index += 1
    return results