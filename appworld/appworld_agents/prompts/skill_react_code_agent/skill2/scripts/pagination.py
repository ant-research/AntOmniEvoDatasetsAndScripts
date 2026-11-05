def get_all_pages(api_func, **kwargs):
    """Fetch all pages from a paginated AppWorld API endpoint.

    Usage:
        from pathlib import Path
        exec((SKILL_DIR / 'scripts/pagination.py').read_text())
        all_songs = get_all_pages(apis.spotify.show_song_library, access_token=token)
        all_playlists = get_all_pages(apis.spotify.show_playlist_library, access_token=token)
        contacts = get_all_pages(apis.phone.search_contacts, access_token=token, query="roommate")

    Args:
        api_func: The paginated API function to call (e.g., apis.spotify.show_song_library).
        **kwargs: Keyword arguments passed to api_func (e.g., access_token, query).

    Returns:
        list: All results across all pages.
    """
    all_results = []
    page_index = 0
    while True:
        page = api_func(page_limit=20, page_index=page_index, **kwargs)
        all_results.extend(page)
        if len(page) < 20:
            break
        page_index += 1
    return all_results


def find_entity(api_func, name_field, name_value, **kwargs):
    """Search for a specific named entity across all pages and return the first match.

    Use this instead of printing search results and hardcoding IDs.
    Search APIs often return many unrelated results — always match by name
    programmatically rather than assuming the first result is correct.

    Usage:
        artist = find_entity(apis.spotify.search_artists, 'name', 'Lily Moon', query='Lily Moon')
        artist_id = artist['artist_id']  # programmatically extracted, never hardcoded

    Args:
        api_func: The paginated search API function (e.g., apis.spotify.search_artists).
        name_field: The field name to match on (e.g., 'name', 'title', 'username').
        name_value: The value to match (e.g., 'Lily Moon').
        **kwargs: Additional keyword arguments passed to api_func (e.g., query, access_token).

    Returns:
        dict: The first matching entity record.

    Raises:
        ValueError: If no entity matches the given name_field=name_value.
    """
    results = get_all_pages(api_func, **kwargs)
    matches = [r for r in results if r.get(name_field) == name_value]
    if not matches:
        raise ValueError(
            f"No entity found with {name_field}='{name_value}'. "
            f"Available: {[r.get(name_field) for r in results[:20]]}"
        )
    return matches[0]


def compute_new_items(existing_api_func, key_field, target_ids, **kwargs):
    """Fetch existing items via paginated API and return only IDs not already present.

    Use this before bulk mutations (download, follow, add) to avoid
    'already exists' errors. This is the programmatic implementation
    of the Pre-mutation state check rule.

    Usage:
        # Before downloading songs, find which ones aren't already downloaded
        songs_to_download = compute_new_items(
            apis.spotify.show_downloaded_songs,
            'song_id',
            target_song_ids,
            access_token=token
        )
        for song_id in songs_to_download:
            apis.spotify.download_song(song_id=song_id, access_token=token)

    Args:
        existing_api_func: Paginated read API returning current state.
        key_field: Field name in existing items to match against (e.g., 'song_id').
        target_ids: Iterable of IDs you plan to add/create/download.
        **kwargs: Keyword arguments for existing_api_func (e.g., access_token).

    Returns:
        list: IDs from target_ids not found in existing items.
    """
    existing_items = get_all_pages(existing_api_func, **kwargs)
    existing_ids = {item[key_field] for item in existing_items}
    return [item_id for item_id in target_ids if item_id not in existing_ids]