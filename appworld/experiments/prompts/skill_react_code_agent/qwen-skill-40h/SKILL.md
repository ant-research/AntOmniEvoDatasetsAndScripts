---
name: appworld-coding
description: AppWorld coding agent skill for API interaction, pagination, error handling, and task completion
---

## Overview

This skill provides patterns for the AppWorld coding agent to reliably complete tasks through Python API interactions. It covers credential handling, pagination, search strategies, error recovery, and verification.

## Procedure

0. **Define SKILL_DIR for file loading**: Always use pathlib to load spec files. Use the skill directory path shown in the Skill Resources section of your system prompt:
    ```python
    from pathlib import Path
    SKILL_DIR = Path('<skill dir shown in the Skill Resources section of the system prompt>')
    ```

0a. **Get current date/time if needed for filtering**: If the task requires date-based filtering ("this year", "yesterday", etc.), call `apis.phone.get_current_date_and_time()` which returns `{'date': 'Thursday, May 18, 2023', 'time': '12:00 PM'}`. Extract the year using:
    ```python
    current = apis.phone.get_current_date_and_time()
    current_year = int(current['date'].split(', ')[-1])  # Extracts 2023 from "Thursday, May 18, 2023"
    ```

1. **Obtain credentials once**: Call `apis.supervisor.show_account_passwords()` ONCE at the start to get login credentials for all required apps. The API returns a LIST of credential dicts. Convert to dict for easy access, then reuse—do NOT re-fetch credentials in a loop. Never guess or hardcode passwords.
    ```python
    credentials = apis.supervisor.show_account_passwords()
    # Convert list to dict: creds = {c['account_name']: c['password'] for c in credentials}
    # Use creds['app_name'] for login; do not re-fetch in loops
    ```

1a. **Obtain user profile for login credentials**: Call `apis.supervisor.show_profile()` to get the user's email and phone number for login. The profile contains ONLY: first_name, last_name, email, phone_number, birthday, sex. It does NOT contain relationship fields like 'coworkers' or 'friends'. For relationship data, use `apis.phone.search_contacts()` or `apis.phone.search_contacts(relationship=...)` to get contact relationship data.
    ```python
    profile = apis.supervisor.show_profile()
    # profile contains: first_name, last_name, email, phone_number, birthday, sex
    # For relationship data, use apis.phone.search_contacts() — see Relationship Filtering rule
    # For login: use profile['email'] for most apps, profile['phone_number'] for phone app
    ```

2. **Login to required apps**: Use `apis.<app>.login()` with credentials from step 1. Use `profile['email']` as username for email-based apps (venmo, spotify, gmail, etc.) and `profile['phone_number']` for the phone app.

3. **Fetch all data with pagination AND access_token**: For ANY API returning a collection (list/search/feed APIs like `show_*`, `list_*`, `get_*`, `*_feed()`, `search_contacts`, `show_*_payment_requests`, `show_transactions`), iterate through all pages using `page_index` starting at 0. Continue until empty results or fewer items than page_limit. ALWAYS include `access_token` for authenticated APIs (playlists, user libraries, private data). Forgetting access_token causes 422 errors like 'This playlist is not public, and you have not passed an access_token'. Payment and transaction APIs ALWAYS require pagination—even if you see results on the first page, there may be more.
    ```python
    exec((SKILL_DIR / 'scripts/pagination.py').read_text())
    all_items = fetch_all(apis.spotify.show_song_library, access_token=spotify_token)
    all_albums = fetch_all(apis.spotify.show_album_library, access_token=spotify_token)
    all_playlists = fetch_all(apis.spotify.show_playlist_library, access_token=spotify_token)  # MUST include access_token for private playlists
    all_feed_items = fetch_all(apis.venmo.show_social_feed, access_token=token)
    all_sent_requests = fetch_all(apis.venmo.show_sent_payment_requests, access_token=token, query="Cory")
    all_coworkers = fetch_all(apis.phone.search_contacts, access_token=phone_token, relationship="coworker")
    ```

3a. **Use relationship filter script for ANY relationship-based contact search**: BEFORE searching contacts by relationship (coworker, friend, roommate, family, etc.), execute the relationship filter script. This ensures ALL contacts with the relationship are retrieved. DO NOT call search_contacts(relationship=...) directly without the script.
    ```python
    exec((SKILL_DIR / 'scripts/relationship_filter.py').read_text())
    all_coworkers = get_contacts_by_relationship(apis.phone.search_contacts, phone_token, 'coworker')
    all_friends = get_contacts_by_relationship(apis.phone.search_contacts, phone_token, 'friend')
    # NEVER use: coworkers = apis.phone.search_contacts(access_token=phone_token, relationship='coworker')
    # That pattern misses contacts on page 2+ and causes incomplete results
    ```

4. **Read search patterns BEFORE filtering by relationship or payment status**: Before any payment reconciliation (determining who paid vs who owes) or relationship-based filtering, read the search patterns reference. This ensures you use the proven patterns for these common scenarios.
    ```python
    print((SKILL_DIR / 'references/search_pattern.md').read_text())
    ```

4a. **Apply two-pass verification for relationship filtering**: When the task requires filtering by relationship (coworkers, friends, roommates, family), you MUST use the two-pass verification pattern. After initial filtering by known relationships, verify each unknown party (sender OR receiver) individually before skipping them. This is MANDATORY - the relationship API's internal graph may have incomplete coverage, causing you to miss items. Skipping the second pass causes incomplete results.
    ```python
    # First pass: filter by known relationship emails from fetch_all search_contacts(relationship=...)
    # Second pass: search individually for each unknown sender/receiver and check their relationships
    # See references/search_pattern.md for the complete two-pass code pattern
    # DO NOT proceed to actions without completing both passes
    ```

5. **Use privates APIs for property checks**: When filtering by ANY properties (boolean like liked/downloaded, or metadata like release_date), use `show_*_privates(entity_id=...)` APIs which return ground-truth properties. List APIs return filtered sets that may not contain the property you need. Do NOT compute intersections of list API results.

6. **Collect from all relevant sources**: For query tasks that span multiple data sources (e.g., "across song, album and playlist libraries"), fetch from ALL applicable libraries before analyzing. Build a set of unique entity IDs from each source, then aggregate. Example: `all_song_ids = set(song_library_ids) | set(album_library_song_ids) | set(playlist_library_song_ids)`.

7. **Systematically filter ALL collected data**: When identifying items matching criteria (date range, relationship, property values), write code to filter across the complete dataset - do NOT manually pick items from memory or partial inspection. For paginated results, ensure your filter processes entries from ALL pages, not just the first page. Example pattern:
    ```python
    # Collect all transactions from all pages first
    all_transactions = fetch_all(apis.venmo.show_social_feed, access_token=token)
    # Then filter programmatically across complete dataset
    target_transactions = [t for t in all_transactions if matches_criteria(t)]
    ```

8. **Filter by relationship BEFORE acting on people**: When the task specifies a subset of people by relationship (e.g., "coworkers", "friends", "roommates", "family"), you MUST filter candidates by relationship BEFORE taking any action on them. Pattern:
    ```python
    # Step 1: Get ALL people with the relationship using fetch_all pagination
    exec((SKILL_DIR / 'scripts/relationship_filter.py').read_text())
    coworkers = get_contacts_by_relationship(apis.phone.search_contacts, phone_token, 'coworker')
    coworker_emails = {c['email'] for c in coworkers}
    
    # Step 2: Filter your candidate list to only those in the relationship set
    from_note = {'Stephen': 33, 'Sabrina': 32, 'Jamie': 31, 'Travis': 21, 'Kristin': 38, 'Anita': 32}
    # Only request from coworkers
    target_requests = {name: amount for name, amount in from_note.items() if lookup_email(name) in coworker_emails}
    
    # Step 3: Create payment requests only for filtered set
    # DO NOT skip this filtering - the relationship API coverage may be incomplete
    ```
    **Why this matters**: In data_id 22cc237_3, the agent created 6 payment requests when only 4 coworkers should have been requested. Skipping relationship filtering caused 2 extra requests for non-coworkers.

9. **Iterative navigation until condition met**: When the task requires repeatedly applying a navigation operation until reaching a target state (e.g., "keep going to next song until...", "scroll through feed until finding...", "go to next page until..."), use this pattern:
    ```python
    # Loop with safety maximum to prevent infinite loops
    max_iterations = 50  # Adjust based on expected collection size
    for i in range(max_iterations):
        result = apis.spotify.next_song(access_token=token)  # Or other navigation API
        
        # Verify current state after each navigation step
        current = apis.spotify.show_current_song(access_token=token)
        print(f"Step {i+1}: {current['title']} by {[a['name'] for a in current['artists']]}")
        
        # Check stopping condition
        if 'Lily Moon' in [a['name'] for a in current['artists']]:
            print("Target reached!")
            break
    
    # Final verification before completing
    final_song = apis.spotify.show_current_song(access_token=token)
    assert 'Lily Moon' in [a['name'] for a in final_song['artists']], "Target not reached"
    ```
    Key points: (1) loop with safety maximum, (2) verify condition at each step, (3) break when satisfied, (4) final verification before complete_task.

9. **Execute required actions**: Perform the API calls (follow, remove, create, send, etc.).

10. **Verify actions succeeded**: Re-check state after mutations to confirm the expected result. For cleanup tasks, verify iteratively. For money transfers: verify the amount matches your expectation by checking the original transaction or payment request details before calling complete_task(). If the task mentions an "accident" payment, cross-reference with transaction history to confirm you're refunding the correct amount—not just the most recent approved request. For account deletion/termination: verify the delete_account() call returns a success confirmation before completing the task.

11. **Complete the task immediately after verification**: Once you have verified the answer is correct, call `apis.supervisor.complete_task(answer=...)` as the IMMEDIATE next step. Do NOT perform any additional API calls, file operations, or exploration after verification—these modify system state and cause failures.
    ```python
    # CORRECT: compute → verify → complete_task
    total = compute_answer()
    print(f"Answer: {total}")  # Verification
    apis.supervisor.complete_task(answer=total)  # IMMEDIATE completion
    # Do NOT make any more API calls after this point
    ```

## Rules

### Pagination

- **Always paginate list APIs**: Loop with `page_index=0, 1, 2...` until empty results. Never assume one call returns everything.
- **page_limit <= 20**: ALWAYS start with page_limit=20 for ALL list APIs. Using page_limit>20 causes immediate 422 validation errors that waste steps. The fetch_all helper uses page_limit=20 by default.
- **Use the fetch_all helper**: The pagination script provides a reusable `fetch_all()` function.

### File Loading

- **Use pathlib, never open()**: The sandboxed REPL does not support `builtins.open()`. Always load files using pathlib. SKILL_DIR should already be defined in Step 0 - reuse it:
    ```python
    # SKILL_DIR defined in Step 0 - reuse it
    exec((SKILL_DIR / 'scripts/pagination.py').read_text())
    ```

### Date/Time Handling

- **Get current date from phone API**: Use `apis.phone.get_current_date_and_time()` which returns `{'date': 'Thursday, May 18, 2023', 'time': '12:00 PM'}`. Parse using `datetime.strptime()`:
    ```python
    from datetime import datetime
    current = apis.phone.get_current_date_and_time()
    parsed_date = datetime.strptime(current['date'], "%A, %B %d, %Y")
    current_year = parsed_date.year
    ```
- **Extract dates from file metadata**: File `show_file()` returns `created_at` in ISO format `'YYYY-MM-DDTHH:MM:SS'`. Extract the date portion and year:
    ```python
    created_at = file_details['created_at']  # '2023-05-18T02:42:58'
    date_part = created_at.split('T')[0]  # '2023-05-18'
    year = int(date_part.split('-')[0])  # 2023
    ```

### Credential Handling

- **Always get credentials from supervisor**: Call `apis.supervisor.show_account_passwords()` before any login. The password is never a literal string like "appname_password".
- **Credentials API returns a list**: The result is a list of dicts, each with 'account_name' and 'password' fields. Iterate to find the app you need:
    ```python
    credentials = apis.supervisor.show_account_passwords()
    spotify_password = None
    for cred in credentials:
        if cred['account_name'] == 'spotify':
            spotify_password = cred['password']
            break
    ```
- **Determine correct username format**: Use `profile['email']` for email-based apps (venmo, spotify, gmail, amazon, file_system, simple_note, splitwise, todoist) and `profile['phone_number']` for the phone app.
- **Extract and store access tokens**: Login returns a dict with 'access_token' and 'token_type'. Always extract the token string before using:
    ```python
    login_result = apis.venmo.login(username=..., password=...)
    access_token = login_result['access_token']  # Extract the string, don't use the whole dict
    # Use access_token in ALL subsequent API calls
    ```
- **Always include access_token in authenticated API calls**: Pass `access_token=<token>` to EVERY API call that requires authentication (private resources like playlists, user data, transactions, etc.). Forgetting this causes 422 errors like 'This playlist is not public, and you have not passed an access_token'. Include it even when the API documentation doesn't explicitly list it as required.
- **Pattern for authenticated calls**: When writing API calls for private resources, use a variable pattern that embeds the token: `result = apis.spotify.show_playlist(playlist_id=pid, access_token=token)` — write the full call with token in one statement, don't build arguments separately. For loops over collections, keep the token in the loop: `for item in collection: details = apis.app.show_item(item_id=item['id'], access_token=token)`.
- **Handle authentication failures**: If login returns 401, verify you are using the correct username format (email vs phone number) and the correct password from show_account_passwords().

### Search Strategies

- **Prefer specific parameters**: Use `genre=` instead of `query=` for genre-based artist filtering. The `genre=` parameter filters precisely; `query=` searches across multiple fields.
- **Use search APIs**: Call `search_notes`, `search_users`, etc. with relevant keywords rather than iterating all items.
- **Verify search results**: Search may return partial matches—verify results match your needs.
- **Disambiguate multiple results**: When search returns multiple candidates, examine each result's metadata (dates, titles, content) to identify the one matching your task context. For time-sensitive tasks (e.g., "yesterday's dinner", "last week's meeting"), compare dates against the current date obtained from `apis.phone.get_current_date_and_time()`.
- **Extract criteria from requester messages**: When the task references what the requester "asked for" or "mentioned", search for and read the actual message/conversation to identify the specific filtering criterion (e.g., director name, category, date) before gathering data. Do not assume keyword-matched notes contain the final answer. Pattern: (1) read requester message via `show_messages`/`show_emails`, (2) extract the criterion they specified, (3) gather candidate items, (4) filter to items matching the criterion. See `references/search_pattern.md` for detailed patterns.
- **Filter structured note content**: When notes contain structured data (movies with directors, expenses with categories, etc.), parse the content and filter items based on properties matching your task criterion. See `references/search_pattern.md` for the filtering pattern.
- **Enum field case-sensitivity**: API enum fields (genre, status, type, etc.) have specific casing that may differ from natural language. Before filtering by an enum value: (1) Check available values using discovery APIs like `show_genres()`, `show_statuses()`, etc., OR (2) Use case-insensitive comparison in your filter (e.g., `item['genre'].lower() == 'r&b'`). Example: `apis.spotify.show_genres()` returns `['EDM', 'R&B', 'indie', ...]` - filtering with `'r&b'` (lowercase) returns 0 results, but `'R&B'` returns correct matches.

### Property Checks

- **Use privates APIs for ground-truth properties**: For ANY property verification (boolean like liked/downloaded, or metadata like release_date), prefer `show_*_privates(entity_id=...)` which returns authoritative ground-truth values. List APIs return filtered sets that may not contain the property you need.
- **Fetch details for missing properties**: When list APIs don't include needed properties (e.g., release_date not in show_song_library results), call `show_<entity>(<entity>_id=...)` to get full details for each item.
- **Album download criterion**: An album is downloaded only if ALL its songs have `downloaded=True`. Check each song via `show_song_privates()`.

### Check Existing Records Before Create vs Update

- **When creating or updating records** (reviews, notes, contacts, etc.), first query existing records to determine which items need new records vs which need updates. This avoids 409 Conflict errors and handles mixed scenarios correctly.
- **Pattern**: (1) collect target entity IDs, (2) query existing records for those entities, (3) separate into 'needs_new' vs 'needs_update' sets, (4) update existing records, (5) create new records.
    ```python
    # Check which songs already have reviews
    for song_id in target_song_ids:
        reviews = apis.spotify.show_song_reviews(song_id=song_id, user_email=user_email)
        if reviews:
            needs_update.append((song_id, reviews[0]))
        else:
            needs_new.append(song_id)
    # Update existing, then create new
    ```

### Cross-Library Aggregation

- **When query mentions multiple sources**: If the task asks for something "across my X, Y, and Z libraries" or similar, fetch from ALL mentioned sources before comparing. Example: "newest song across song, album and playlist libraries" requires fetching from show_song_library, show_album_library (then show_album for each), and show_playlist_library (then show_playlist for each).
- **Aggregate unique items**: Build a set of unique entity IDs from all sources before fetching details or comparing properties. This avoids duplicates and ensures fair comparison.
- **Compare on complete data**: Only after collecting ALL items from ALL sources, analyze to find max/min/count/etc.
- **Set intersection for AND criteria**: When a task requires items matching multiple criteria (e.g., "songs in album library which I have liked"), fetch each criterion's set independently, then compute intersection. Example: `target_ids = set(album_library_song_ids) & set(liked_song_ids)`. This is more efficient than checking a property on each item from one source.
- **Set difference for exclusion criteria**: When a task requires items in one set but NOT in another (e.g., "unfollow artists who have not sung any liked songs", "remove songs not downloaded", "delete contacts not in a group"), fetch both sets independently, then compute set difference. Example: `to_unfollow_ids = set(followed_artist_ids) - set(liked_artist_ids)` or `to_remove_ids = set(all_items) - set(items_to_keep)`. This ensures complete, systematic filtering without manual inspection.
- **Act on related entities**: When the task requires acting on entities related to items matching a criterion (e.g., "Follow all artists who have sung songs I have liked"), use this pattern: (1) fetch the set of items with the criterion property using a pre-filtered API like `show_liked_songs()`, (2) extract unique related entity IDs from those items (e.g., artist IDs from song metadata), (3) check current status for each related entity, (4) act on entities that need action. Example: `liked_songs = apis.spotify.show_liked_songs(access_token=token); artist_ids = {a['id'] for s in liked_songs for a in s['artists']}`.

### Per-Group Selection

- **When task requires one item from EACH group**: Tasks like "most-played song from each playlist", "most recent note from each folder", or "highest-rated restaurant from each cuisine" require a per-group max/min pattern. Unlike cross-library aggregation (which finds ONE item across ALL sources), per-group selection finds ONE item FROM EACH group/collection.
- **Pattern**: (1) fetch all groups (e.g., all playlists, all folders, all categories), (2) for each group, identify the item with the target property (max play_count, most recent date, highest rating), (3) collect all selected items for the final action.
- **Do NOT use set operations** for per-group selection—set intersection/difference compares items across sources, but per-group selection requires intra-group comparison.
- **Example**: "Create a playlist containing the most-played song from each of my playlists":
    ```python
    # Step 1: Fetch all groups
    all_playlists = fetch_all(apis.spotify.show_playlist_library, access_token=token)
    # Step 2: Get details for each group to see its items
    playlist_details = [apis.spotify.show_playlist(playlist_id=pl['playlist_id'], access_token=token) for pl in all_playlists]
    # Step 3: Fetch properties for all unique items (e.g., play_count for all songs)
    all_song_ids = {song['id'] for pl in playlist_details for song in pl['songs']}
    song_play_counts = {sid: apis.spotify.show_song(song_id=sid)['play_count'] for sid in all_song_ids}
    # Step 4: For EACH group, find the item with max property
    most_played_per_playlist = []
    for pl in playlist_details:
        max_count = -1
        best_song = None
        for song in pl['songs']:
            count = song_play_counts[song['id']]
            if count > max_count:
                max_count = count
                best_song = song
        most_played_per_playlist.append(best_song)
    # Step 5: Act on all selected items
    ```

### Relationship Filtering

**MANDATORY TWO-PASS VERIFICATION PATTERN**: When filtering by relationship (coworkers, friends, roommates, family, etc.), the relationship API may have incomplete coverage. You MUST:
1. First pass: Get contacts with `search_contacts(relationship=...)` using `fetch_all()` pagination, filter items by known relationship emails.
2. Second pass: For each remaining item from unknown senders, search for that sender individually and check their relationships.

**Why two passes are mandatory**: The search_contacts(relationship=...) API returns contacts based on the app's internal relationship graph, which may NOT match the task's ground truth. In data_id 6ea6792_3, the first pass found only 13 of 18 required payment requests. The second pass recovered the missing 5 by checking each unknown sender individually. Skipping the second pass guarantees incomplete results.

**See `references/search_pattern.md` for the complete two-pass code pattern.**

### File Export Format

- **CSV headers**: When exporting data to CSV, use exact header names from the task specification (e.g., "Title, Artists" not "Song Title, Artist Names"). Match header capitalization and spacing exactly.
- **Field separators**: Use comma as the CSV field separator. For multi-value fields (e.g., multiple artists), use pipe `|` as the internal separator within the field value.
- **Deduplication by composite key**: When exporting from multiple sources where duplicates may exist, deduplicate by the combination of fields that define uniqueness per the task (e.g., (title, artists) tuple for songs). Use a dict with composite key to ensure each unique combination appears exactly once.

### File Path Handling

- **Preserve task-specified path notation**: When the task specifies a file path with tilde notation (e.g., `"~/backups/file.csv"`), use the path exactly as written. Do NOT expand `~` to the full home directory path (e.g., `/home/admin/`). The file_system app APIs expect the literal path string from the task.
- **File manipulation workflow**: For tasks involving renaming/moving files based on metadata:
    1. List directory contents with `show_directory(directory_path=...)`. `show_file(file_path=...)` returns metadata including `created_at` in ISO format `'YYYY-MM-DDTHH:MM:SS'`.
    2. Extract date components from `created_at` by splitting on 'T' then '-': `year = int(details['created_at'].split('T')[0].split('-')[0])`.
    3. Rename/move files with `move_file(source_file_path=old_path, destination_file_path=new_path, access_token=token)`.
    4. Create directories with `create_directory(directory_path=..., access_token=token, allow_if_exists=True)`.
    5. Verify changes by re-checking directory contents.

### Verification

- **Verify after mutations**: After any remove/add/follow operation, re-query to confirm the change succeeded.
- **Iterative cleanup with cascading effects**: For cleanup involving multiple entity types (e.g., songs AND albums), removing one type may change the criteria for another. Example: removing songs changes which albums have 'all songs downloaded'. Always complete cleanup of the dependent entity type FIRST, then re-evaluate the dependent-on entity type. Loop: (1) remove songs not meeting criteria, (2) re-check album download status for remaining albums, (3) remove albums not meeting criteria, (4) repeat song check if album removal affects song library.

### Task Completion

- **Always call complete_task**: The final step must be `apis.supervisor.complete_task(answer=...)`.
- **Minimal answers**: For query tasks, return only the requested value—entity names, numbers, or IDs—not full sentences. Example: for "What is the title of the song?", return `answer="Song Title"` not `answer="The title is Song Title"`. For action-only tasks, omit answer or use `answer=None`.

### Text Message Content

- **Bare lists only**: When sending a list of items via text message (e.g., "Reply with movie titles"), format the message body as ONLY the comma-separated values with no introductory or explanatory text. Example: `message="Title1, Title2, Title3"` NOT `message="Here are the titles: Title1, Title2, Title3"`.
- **Avoid substring collisions**: Introductory phrases like "Here are...", "These are...", "My recommendations include..." may contain substrings that match excluded item names. Always use the bare list format to prevent accidental matches.

## Error Patterns to Avoid

- **Missing pagination**: Fetching only the first page causes incomplete data and wrong decisions. ALWAYS use fetch_all() for show_received_payment_requests(), show_sent_payment_requests(), show_transactions(), and show_social_feed() - these APIs commonly have data spanning multiple pages. Failing to paginate payment/transaction APIs causes incomplete payment reconciliation (missing who already paid or who still owes).
- **Task interpretation flip**: Do NOT second-guess the task's stated direction. If the task says "How much money have I received FROM X", compute money received FROM X. Do NOT flip to "sent TO X" interpretation. The task phrasing is intentional—"received to my coworkers" means "received from coworkers" (you received money, coworkers sent it). Common flip errors: "received from" → "sent to", "paid to" → "received from". Trust the task's stated direction.
- **Wrong refund amount**: When refunding an "accident" payment, verify the amount matches the original transaction. Don't just refund the most recent approved request—cross-reference with transaction history and task context to confirm you're refunding the correct amount.
- **Incomplete filtering across pages**: Fetching all pages but manually identifying items instead of programmatic filtering causes missed items. Always write filter code that processes the complete collected dataset.
- **Continued exploration after answer computed**: After computing and verifying the answer, calling additional APIs (searching directories, decompressing files, etc.) modifies system state and causes 'no model changes' failures. Complete the task IMMEDIATELY after verification.
- **Accepting all without relationship filtering**: When the task specifies a relationship filter (e.g., "from coworkers", "to friends", "family members"), filtering by relationship is mandatory. Acting on ALL items without filtering by relationship causes incorrect actions on unrelated items.
- **Skipping second-pass verification for relationships**: When filtering by relationship, the relationship API may have incomplete coverage. After filtering by known relationships, you MUST verify each unknown sender individually before skipping them. Skipping the second pass causes incomplete results (e.g., data_id 6ea6792_3 missed 5 of 18 payment requests).
- **Wrong username format for login**: Phone app requires phone number as username; all other apps use email. Use `profile['phone_number']` for phone app, `profile['email']` for others. If login fails with 401, verify the username format.
- **Missing access_token**: Passing the full login result dict instead of extracted access_token, or forgetting to include access_token in API calls, causes 422/401 errors. Always extract `login_result['access_token']` and pass it to authenticated APIs.
- **page_limit > 20**: Causes immediate 422 errors. ALWAYS use page_limit=20 or lower. Once you load the pagination helper with `exec((SKILL_DIR / 'scripts/pagination.py').read_text())`, use `fetch_all()` for ALL subsequent list APIs—the helper automatically uses page_limit=20. Do NOT mix manual API calls with page_limit>20 after loading the helper.
- **Ignoring 422 errors**: Read the error message, identify the constraint violation, adjust parameters, and retry.
- **Guessing credentials**: Never hardcode passwords. Always call `show_account_passwords()` first.
- **Intersecting list APIs**: Computing intersection of `show_liked_songs()` IDs and `show_downloaded_songs()` IDs gives wrong results. Use privates APIs to check both properties on the same entity.
- **Query-param-only filtering**: Don't trust API genre/query filters alone—filter in code after collecting all results.
- **No verification**: Never call `complete_task()` without verifying actions succeeded.
- **Skipping complete_task**: Forgetting this leaves the task incomplete.

## Output Format

Final step: `apis.supervisor.complete_task(answer=<value>)`

- **Counting tasks**: `answer=5` (not "There are 5 songs")
- **Entity tasks**: `answer="Artist Name"` (not "The artist is...")
- **List tasks**: `answer=["item1", "item2"]` (not "The playlist contains...")
- **Action-only tasks**: `apis.supervisor.complete_task()` or `answer=None`

## References

- `references/pagination.md` — BEFORE any list API call: `print((SKILL_DIR / 'references/pagination.md').read_text())`
- `references/search_pattern.md` — BEFORE searching or filtering by relationship: `print((SKILL_DIR / 'references/search_pattern.md').read_text())`
- `references/complete_task.md` — BEFORE completing: `print((SKILL_DIR / 'references/complete_task.md').read_text())`
- `references/credentials.md` — BEFORE credential lookup: `print((SKILL_DIR / 'references/credentials.md').read_text())`

## Scripts

- `scripts/pagination.py` — BEFORE any list API call: `exec((SKILL_DIR / 'scripts/pagination.py').read_text())`
- `scripts/relationship_filter.py` — BEFORE ANY search_contacts(relationship=...) call: `exec((SKILL_DIR / 'scripts/relationship_filter.py').read_text())`, then use `get_contacts_by_relationship()` helper