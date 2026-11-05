"""Pagination helper for AppWorld list APIs.

Usage:
    exec((SKILL_DIR / 'scripts/pagination.py').read_text())
    all_songs = paginated_call(apis.spotify.show_song_library, access_token=token)

IMPORTANT: Only use paginated_call for APIs that accept page_index/page_limit parameters.
If an API does not accept these (e.g., file_system.show_directory), call it directly instead.
"""


def paginated_call(api_func, page_limit=20, **kwargs):
    """Call a paginated API across all pages and return all results.

    Args:
        api_func: The API function to call (e.g., apis.spotify.show_song_library).
        page_limit: Number of results per page. Max 20 for AppWorld APIs.
        **kwargs: Additional keyword arguments passed to the API function.

    Returns:
        List of all results across all pages.
    """
    if page_limit > 20:
        page_limit = 20
    all_results = []
    page_index = 0
    while True:
        results = api_func(page_index=page_index, page_limit=page_limit, **kwargs)
        if not results:
            break
        all_results.extend(results)
        if len(results) < page_limit:
            break
        page_index += 1
    return all_results


print("Pagination helper loaded. Use: paginated_call(api_func, **kwargs)")
print("Example: songs = paginated_call(apis.spotify.show_song_library, access_token=token)")