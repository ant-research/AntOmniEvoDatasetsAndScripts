"""Pagination helper for AppWorld list APIs.

Usage: exec((SKILL_DIR / 'scripts/pagination.py').read_text())
       then call fetch_all(api_func, **kwargs)

This script provides a reusable fetch_all() function that handles
pagination for any AppWorld list API.
"""


def fetch_all(api_func, **kwargs):
    """
    Fetch all results from a paginated AppWorld API.
    
    Args:
        api_func: The API function to call (e.g., apis.spotify.show_song_library)
        **kwargs: Arguments to pass to the API (excluding page_index)
    
    Returns:
        List of all results from all pages
    
    Example:
        # First, exec this script:
        exec((SKILL_DIR / 'scripts/pagination.py').read_text())
        
        # Then use fetch_all:
        all_songs = fetch_all(apis.spotify.show_song_library, access_token=token)
        all_liked = fetch_all(apis.spotify.show_liked_songs, access_token=token, page_limit=20)
    """
    all_results = []
    page_index = 0
    
    while True:
        page_kwargs = kwargs.copy()
        page_kwargs['page_index'] = page_index
        
        try:
            page_results = api_func(**page_kwargs)
        except Exception as e:
            # If pagination fails on first page, try without page_index
            # (some APIs don't support pagination)
            if page_index == 0:
                return list(api_func(**kwargs))
            break
        
        if not page_results or len(page_results) == 0:
            break
        
        all_results.extend(page_results)
        page_index += 1
    
    return all_results