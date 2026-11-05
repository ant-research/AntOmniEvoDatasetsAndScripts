# Pagination Pattern for AppWorld APIs

## Prerequisite: Define SKILL_DIR

Before using this pattern, define SKILL_DIR using the skill directory path shown in the Skill Resources section of your system prompt:

```python
from pathlib import Path
SKILL_DIR = Path('<skill dir shown in the Skill Resources section of the system prompt>')
```

## How to Load the Helper

Load the pagination helper using exec (NOT import):

```python
exec((SKILL_DIR / 'scripts/pagination.py').read_text())

# Now fetch_all() is available
all_songs = fetch_all(apis.spotify.show_song_library, access_token=token, page_limit=20)
```

Do NOT use `from scripts.pagination import fetch_all` - this will fail. Always use exec() with the full path.

## Common Mistakes to Avoid

**Do NOT wrap the API function in lambda or a wrapper function** - fetch_all handles page_index internally:

```python
# WRONG - these patterns cause 'missing positional arguments' errors:
all_alarms = fetch_all(lambda page_index, page_limit: apis.phone.show_alarms(...))
def get_alarms(page_index, page_limit):
    return apis.phone.show_alarms(...)
all_alarms = fetch_all(get_alarms)

# CORRECT - pass the API function directly:
all_alarms = fetch_all(apis.phone.show_alarms, access_token=token)
```

**Do NOT try to import the script**:
```python
# WRONG - this will fail:
from scripts.pagination import fetch_all

# CORRECT - always use exec():
exec((SKILL_DIR / 'scripts/pagination.py').read_text())
```

## When to Use

Use this pattern BEFORE calling ANY list API (methods starting with `show_`, `list_`, or `get_` that return collections).

## Why It Matters

AppWorld APIs return paginated results. If you don't paginate, you only see the first page (typically 5-50 items) and miss the rest. This causes:

- Incomplete analysis
- Wrong decisions about which items to keep/remove
- Task failures

## Pattern

```python
def fetch_all(api_func, **kwargs):
    """Fetch all results from a paginated API."""
    all_results = []
    page_index = 0
    while True:
        page_kwargs = kwargs.copy()
        page_kwargs['page_index'] = page_index
        page_results = api_func(**page_kwargs)
        if not page_results or len(page_results) == 0:
            break
        all_results.extend(page_results)
        page_index += 1
    return all_results

# Usage example:
all_songs = fetch_all(apis.spotify.show_song_library, access_token=token)
all_liked = fetch_all(apis.spotify.show_liked_songs, access_token=token)
```

## Alternative Inline Pattern

If you prefer not to define a function:

```python
all_results = []
page_index = 0
while True:
    page = apis.spotify.show_song_library(access_token=token, page_index=page_index)
    if not page:
        break
    all_results.extend(page)
    page_index += 1
```

## Page Limit Constraint

Most APIs have a maximum `page_limit` of 20. If you receive a 422 validation error mentioning page_limit:

1. Read the error message to identify the constraint
2. Reduce page_limit to 20 or lower
3. Retry with the corrected parameter

```python
# Start with page_limit=20
all_songs = fetch_all(apis.spotify.show_song_library, access_token=token, page_limit=20)
```

## Access Token for Private Resources

Some list APIs require `access_token` for private resources (e.g., playlists you own but aren't public). If you receive a 422 error saying "This playlist is not public, and you have not passed an access_token":

1. Add `access_token=<your_token>` parameter to the API call
2. Retry the call

```python
# For private playlists, always pass access_token
playlist_details = apis.spotify.show_playlist(playlist_id=pl_id, access_token=token)
```

## APIs That Require Pagination

- `show_song_library()` — songs in user's library
- `show_album_library()` — albums in user's library
- `show_playlist_library()` — playlists in user's library (use pagination; aggregate song_ids from returned playlists to analyze songs within playlists)
- `show_liked_songs()` — songs user has liked
- `show_downloaded_songs()` — songs user has downloaded
- `show_liked_albums()` — albums user has liked
- `search_artists()` — search for artists
- `search_tracks()` — search for tracks
- `show_social_feed()` — social feed transactions
- `show_sent_payment_requests()` — sent payment requests (requires pagination with page_index)
- `show_received_payment_requests()` — received payment requests (requires pagination with page_index)
- `show_text_message_window()`, `search_text_messages()` — text message history and search (requires pagination with page_index; messages are returned in pages)
- `show_voice_message_window()`, `search_voice_messages()` — voice message history and search (requires pagination with page_index)
- **Any other `show_*`, `list_*`, `get_*`, `search_*`, or `*_feed()` API that returns a collection** — assume pagination is required until proven otherwise. Message APIs often have conversations spanning multiple pages.

## Getting Songs in an Album

To check whether all songs in an album are downloaded, first get the album details which includes the list of songs:

```python
album = apis.spotify.show_album(album_id=album_id)
# album['songs'] is a list of song objects with 'id' field
for song in album['songs']:
    song_privates = apis.spotify.show_song_privates(song_id=song['id'], access_token=token)
    if song_privates['downloaded']:
        # song is downloaded
```

Then use show_song_privates() on each song to check its download status. An album is downloaded only if ALL its songs have downloaded=True.

## Querying Across Multiple Libraries

When a task asks for items "across" multiple library types (e.g., "oldest song across my song, album and playlist libraries"):

1. Fetch all library types with pagination
2. Collect all unique entity IDs from each source:
   - Direct song IDs from `show_song_library()`
   - Song IDs from each album's `song_ids` field
   - Song IDs from each playlist's `song_ids` field
3. Use `set()` to deduplicate across sources
4. Fetch details for each unique ID and analyze

```python
# Example: collect all song IDs across libraries
all_song_ids = set()
for song in song_library:
    all_song_ids.add(song['song_id'])
for album in album_library:
    for song_id in album['song_ids']:
        all_song_ids.add(song_id)
for playlist in playlist_library:
    for song_id in playlist['song_ids']:
        all_song_ids.add(song_id)
```

## Finding Max/Min Items Within Playlists

When a task asks for the most/least X item among songs in your playlists (e.g., "most-liked song in my Spotify playlists"):

1. Fetch all playlists with pagination using `show_playlist_library(access_token=...)`
2. Collect ALL unique song_ids from ALL playlists into a set
3. Fetch details for each unique song_id using `show_song(song_id=...)`
4. Analyze the complete dataset to find max/min/count (e.g., max like_count)
5. Return the answer immediately after verification

```python
# Step 1: Get all playlists (paginate if needed)
playlists = fetch_all(apis.spotify.show_playlist_library, access_token=token)

# Step 2: Collect unique song_ids from all playlists
all_song_ids = set()
for playlist in playlists:
    all_song_ids.update(playlist['song_ids'])

# Step 3: Fetch details for each song
song_details = {}
for song_id in all_song_ids:
    song = apis.spotify.show_song(song_id=song_id)
    song_details[song_id] = {
        'title': song['title'],
        'like_count': song['like_count'],
        'artists': [a['name'] for a in song['artists']]
    }

# Step 4: Find the song with maximum like_count
max_likes = max(d['like_count'] for d in song_details.values())
most_liked = [sid for sid, d in song_details.items() if d['like_count'] == max_likes]

# Step 5: Complete task immediately
apis.supervisor.complete_task(answer=song_details[most_liked[0]]['title'])
```

## Set Intersection for Multi-Criteria Filtering

**WHEN** a task requires items matching multiple AND criteria (e.g., "songs in album library which I have liked", "downloaded songs that are also in playlists"):

1. **Fetch each criterion's set independently** using pagination
2. **Compute intersection** using Python set operations to find items matching ALL criteria
3. **Process the intersection** - this is your target set

```python
# Example: "songs in album library which I have liked"
# Step 1: Fetch both sets independently
album_songs = set()
for album in fetch_all(apis.spotify.show_album_library, access_token=token):
    album_details = apis.spotify.show_album(album_id=album['album_id'])
    for song in album_details.get('songs', []):
        album_songs.add(song['id'])

liked_songs = set(song['song_id'] for song in fetch_all(apis.spotify.show_liked_songs, access_token=token))

# Step 2: Compute intersection - songs matching BOTH criteria
target_songs = album_songs & liked_songs  # or album_songs.intersection(liked_songs)

# Step 3: Process target set
for song_id in target_songs:
    # Apply action (e.g., rate, review, etc.)
```

**Why this pattern**: Computing intersection of independently fetched sets is more efficient and reliable than checking a property (e.g., 'liked') on each item from one source using privates APIs. Use set intersection when both criterion sets can be fetched directly.

## Verification

After fetching all pages, verify completeness:

- If the last page has fewer items than page_limit, you likely got everything
- If the last page is full (e.g., exactly 20 items), there might be more—try one more page to confirm
- **Feed APIs**: Social feeds, activity feeds, and timeline APIs often paginate silently. Always iterate until you receive an empty page or fewer items than page_limit.

## Processing Collected Data

**CRITICAL**: After fetching all pages, process the COMPLETE dataset - do NOT manually identify matching items from memory or partial inspection:

```python
# WRONG: Manually listing items visible on first page
matching = [item1, item2, item3]  # Misses items on other pages!

# CORRECT: Programmatic filter across all collected data
matching = [item for item in all_items if item meets criteria]
```