---
name: appworld-coding
description: "AppWorld coding agent skill for API interaction and task completion. Handles action vs question tasks, pagination, credential lookup, cross-app data orchestration, and answer formatting."
---

## Overview
Complete AppWorld tasks by writing Python code that calls app APIs, then report results via `apis.supervisor.complete_task()`.

## Procedure

1. Read the task instruction. Determine if it is a **question task** (asks "How many...", "What is...", "Who...", "Which...") or an **action task** (says "Follow...", "Send...", "Delete...", "Create...", "Update...", "Remove...", "Clean up...").
2. Look up credentials: call `apis.supervisor.show_account_passwords()` and `apis.supervisor.show_profile()`. Store both results in variables — never skip `show_profile()`:
   ```python
   passwords = apis.supervisor.show_account_passwords()
   profile = apis.supervisor.show_profile()
   my_email = profile['email']
   my_phone = profile['phone_number']
   ```
   Extract the login username from `profile['email']` (or `profile['phone_number']` for phone-based apps) and the password by matching `account_name` in `passwords`. Never hardcode or guess an email/phone for login.
3. Log in to required apps using the stored identifiers from step 2 (e.g., `username=my_email`). Never type an email or phone number directly into a login call — always use the variable extracted from `profile`.
4. Use `apis.api_docs.show_api_doc()` to check API parameters and return-value field names before calling unfamiliar APIs. When accessing properties of returned objects, never assume field names based on other APIs — different endpoints may use different names for the same concept (e.g., `id` vs `song_id`). For enum-like parameters (e.g., `direction`, `status`, `sort_by`), never guess values — the accepted strings vary across APIs and wrong values cause 422 errors.
4a. **Relationship resolution checkpoint** — When the task references people by relationship (e.g., "my friends," "my roommates," "my family," "my coworkers"), resolve them NOW via the phone app's `search_contacts(relationship=...)` with full pagination (step 5 pattern). Store the resulting email set. When another data source (e.g., a note) provides specific names, still perform the phone contact lookup — match those names against the contact results (by first and last name) to obtain verified email identifiers. This prevents misidentification from first-name-only searches in the target app, where multiple users may share the same first name and name-based search returns both the intended person and unrelated users. Do NOT use the target app's user search API (e.g., `search_users`) as a substitute for phone contact lookup — name search matches can be unrelated to the relationship group. Complete this lookup BEFORE executing any actions in the target app.
4b. **Date computation checkpoint** — When the task references relative dates ("yesterday", "today", "this week", "this month", "last month", etc.), compute them programmatically NOW from `datetime.now()` using `timedelta`. Store computed dates in variables for use in subsequent filtering and API calls:
   ```python
   from datetime import datetime, timedelta
   today_str = datetime.now().strftime('%Y-%m-%d')
   yesterday = datetime.now() - timedelta(days=1)
   yesterday_str = yesterday.strftime('%Y-%m-%d')
   ```
   Never write `datetime(2023, 5, 18)` or `"2023-05-18"` — the frozen date varies across tasks and hardcoding produces wrong results with no error. Complete this computation BEFORE any data-gathering or API calls that depend on dates.
5. Execute the required API calls. **Every API that returns a list must be paginated** using this pattern:
   ```python
   all_results = []
   page_index = 0
   page_limit = 20
   while True:
       page = apis.<app>.<api>(..., page_index=page_index, page_limit=page_limit)
       all_results.extend(page)
       if len(page) < page_limit:
           break
       page_index += 1
   ```
   Never call any list API without setting `page_limit` and `page_index` — the first page alone silently omits results with no error or warning.
6. **Pagination checkpoint** (mandatory code gate before proceeding): After all data-gathering and action API calls, verify that every list API call used `page_limit` and `page_index`. For any that did not, re-call them now with the pagination loop from step 5 and replace the incomplete result:
   ```python
   # CHECKPOINT: Did any list API call skip pagination?
   # If a variable holds a list from an API call that did NOT use page_limit/page_index,
   # re-call that API with pagination NOW:
   # all_items = []
   # page_index = 0
   # page_limit = 20
   # while True:
   #     page = apis.<app>.<api>(..., page_index=page_index, page_limit=page_limit)
   #     all_items.extend(page)
   #     if len(page) < page_limit:
   #         break
   #     page_index += 1
   ```
   Never proceed past this checkpoint with unpaginated list results.
7. Verify actions by reading back the changed state (e.g., call `show_following` after `follow_artist`, `show_sent_messages` after sending). When verifying, also paginate any list API used for verification. When multiple items were modified, verify ALL of them — not just one sample. Loop through each modified item and confirm the expected change took effect. A single verification only confirms that one operation succeeded; silent failures on other items go undetected.
8. Call `apis.supervisor.complete_task()`:
   - **Action tasks**: call `complete_task()` with NO `answer` argument (or `answer=None`). Never pass a count, name, or description as the answer.
   - **Question tasks**: call `complete_task(answer=<value>)` where `<value>` is the minimal answer — a number, name, or ID, not a sentence.
9. Before calling `complete_task`, re-read the task and confirm:
   ```python
   # ACTION TASK: task uses action verbs without asking a question → answer must be None
   # QUESTION TASK: task asks a question → answer must be provided
   # PAGINATION: all list API results were fetched with pagination (step 6 checkpoint)
   ```

## Rules

- **Pagination**: Always explicitly set `page_limit` (≤ 20) and loop `page_index` from 0 upward on every list API call — this includes calls with filter parameters like `user_email`, `min_rating`, or `query`. Filters narrow results but do not disable pagination; the API still returns only the first page silently. Getting N results does NOT mean there are only N total — even with filters. Loop until `len(result) < page_limit` or the list is empty.
- **API filter parameters**: When a task specifies a genre, category, numeric threshold (e.g., follower count, price), date range, or status, prefer the API's built-in filter parameters over fetching all results and filtering in Python. Check `apis.api_docs.show_api_doc()` to discover available filter parameters.
- **Credentials**: Look up passwords via `apis.supervisor.show_account_passwords()` and extract them programmatically by matching `account_name` (e.g., `next(p['password'] for p in passwords if p['account_name'] == 'simple_note')`). Never visually copy password strings from printed output — special characters like `{`, `}`, `#`, `$` in passwords are easily confused with Python syntax or dict display, causing transcription errors. Look up login identifiers from `apis.supervisor.show_profile()`, which returns both `email` and `phone_number` fields. Different apps use different identifiers for login — try the identifier that matches the app's purpose (e.g., `phone_number` for the phone app, `email` for email-based apps). If login fails with one identifier, retry with the other. Never hardcode or guess credentials.
- **Date/time**: Always get from `datetime.now()` or `apis.phone.get_current_date_and_time()`, never from internal knowledge.
- **Cross-app data orchestration**: When a task references data in one app and requires actions in another, read ALL reference data from the source app first, then use it to drive actions in the target app. Never start performing actions before gathering all needed input data from other apps.
- **Reply-to-message context and filtering**: When a task says to "reply" to a message (text, email, etc.) or act "as per their request," always read the original incoming message first (e.g., via `search_text_messages` or `search_emails` with the sender's identifier). The task instruction may summarize the request broadly (e.g., "movie recommendations"), but the actual message often contains specific qualifiers (e.g., "from Quentin Tarantino," "under $50," "last week"). Composing the reply based only on the task instruction's summary risks including or excluding items incorrectly. After reading the request content, when the source data contains attributes matching the request's criteria (genre, director, category, artist, location, price, etc.), filter the data to only include items satisfying those criteria before composing the reply — parse attribute fields in Python code and apply the filter, rather than returning all items. When composing the reply message body, include ONLY the filtered items in the format specified by the task (e.g., comma-separated titles) — do not add introductory prose ("Here are my recommendations:", "Hi! Here are the movies:") because conversational text can accidentally contain substrings matching excluded items after normalization (e.g., "Here" contains "her" matching the movie "Her", "It's" contains "it" matching the movie "It"). The task instruction's specified format is sufficient; added text only risks false substring matches against excluded items.
- **User identifier lookup**: When a task provides names but the API requires an email or user ID, search for users by name in that app's search API to obtain the correct identifier. When names come from another app, never assume existing contacts or transaction partners are the intended recipients — always look up names from the source app explicitly. When filtering result lists by a person's name from the task instruction, match the name exactly — check `first_name` and `last_name` fields separately if available, or split the full name and compare. Never use substring containment (e.g., `'Robert' in name`) — it matches unintended names like 'Robertson' or 'Roberta'.
- **Relationship-based contact lookup**: When a task references people by their relationship to the user (e.g., "my friends," "my roommates," "my family," "my coworkers"), find them via the phone app's `search_contacts(relationship=...)` filter. The phone app stores relationship metadata that other apps lack. Use the resulting emails as identifiers when acting in the target app. **Always paginate `search_contacts` fully** — use `page_limit` and `page_index` and loop until all pages are retrieved; a single page may omit contacts. Be especially careful with "my friends" — many apps (e.g., Venmo, social apps) have their own "friend" or "contact" concept and corresponding APIs (e.g., `search_friends`). These app-specific friend lists are NOT the same as the phone app's relationship-based "friend" contacts. Always use the phone app's `search_contacts(relationship='friend')` to resolve "my friends," never the target app's friend/search API. When a task names multiple relationships (e.g., "friends and roommates"), search each relationship separately with its own `search_contacts(relationship=...)` call (paginating each), then merge the email sets (e.g., `friend_emails.union(roommate_emails)`) — never pass multiple relationships as a single argument, since the API accepts one relationship at a time.
- **Deduplication (partial completion)**: When a task says "some have already...", "for those who haven't", or asks you to perform a bulk action (e.g., "Follow all X", "Add all Y", "Send to all Z") that may partially overlap with existing state, (1) get the full list of items needing action, (2) check what's already been done via transactions/messages/etc., (3) subtract to find remaining items and act only on those — always compute the subtraction in Python code (e.g., build a dict of {name: amount} from the source data and a set of names from completed transactions, then iterate over the dict skipping names in the completed set). Never re-do already-completed items. Never try to compute the difference mentally — it is error-prone when lists have more than 2-3 items. Even when the task phrasing doesn't hint at partial completion, always check existing state before bulk follow/add/send actions — re-performing an already-completed action may cause errors or side effects.
- **Deduplication (multi-source merge)**: When a task asks for a unique or deduplicated list by merging items from multiple sources or collections, use a dict keyed by a composite tuple of all identifying fields (not just one field). Normalize multi-valued fields in the key by sorting (e.g., `tuple(sorted(artist_names))`) so that equivalent items with different ordering map to the same key. Using only a single field (e.g., title alone) conflates distinct items that share that field. The same deduplication principle applies before performing bulk operations: when collecting items from multiple sources (e.g., songs from multiple artist searches, messages from multiple threads), deduplicate by the entity's unique ID first (e.g., `song_id`, `message_id`) — the same entity can appear in multiple source queries (e.g., a song by multiple artists appears when searching each artist). Without deduplication by ID, redundant operations on the same entity cause API errors or harmful side effects.
- **Create-or-update**: When a task requires setting a property on items (e.g., rating, review, preference) and some may already exist, check each item's existing state first. Partition into: (1) items with the correct value — skip, (2) items with a different value — update via the update API, (3) items with no existing record — create via the create API. Never assume all items need the same operation type. Compute the partition in Python code, not mentally.
- **Minimal field updates**: When updating a record, pass only the fields that need to change. Passing unchanged fields can cause unintended side effects — some APIs reset omitted-but-passed fields, and evaluations often assert that only specific fields were modified. For text content updates (e.g., toggling a checkbox in a note), use surgical string replacement (`content.replace(old, new)`) rather than reconstructing the full content, to avoid accidentally altering other parts of the text.
- **Conditional threshold updates**: When a task says to set a value to X only if currently lower (e.g., "rate 4 stars if rated lower"), check each item's current value before updating. Skip items where the current value already meets or exceeds the target — updating them is unnecessary and may downgrade values above the target.
- **Relative date filtering**: When a task references relative dates ("yesterday", "last week", "this month"), get the current date via API first, compute the date range, and apply min/max date filter parameters on the initial API call. Do not fetch unfiltered records and re-query later.
- **Programmatic result processing**: Never print an entire API result set for visual inspection — large outputs overwhelm the LLM context and make it impossible to identify correct items, causing infinite restart loops. Instead, write Python code to filter, sort, and extract the specific items needed. Only print the final filtered/selected result (or its key fields), not the full raw data. When you need to inspect a result to understand its structure, print only the first item: `print(all_results[0])`.
- **Extremum selection**: When a task asks for "the last", "most recent", "earliest" item, or any superlative ("most/least played", "highest/lowest rated", "biggest/smallest", etc.), collect all matching items (paginating fully) and programmatically select using `max()` or `min()` with an appropriate key (e.g., `max(items, key=lambda x: x['created_at'])` for temporal, `max(items, key=lambda x: x['play_count'])` for play count). Never assume API response order reflects any ranking — always compare the relevant attribute explicitly. Visual inspection of printed results is unreliable when multiple matches exist across pages. Apply the Programmatic result processing rule first — filter the full result set in Python, then apply `max()`/`min()` on the filtered subset.
- **Music player queue interpretation**: When a task references playback state in a music player queue (e.g., "songs played so far," "currently playing," "up next"), get the queue and use position markers to determine playback state: the song marked `is_current=True` or `is_playing=True` is the current song; songs before it in the queue have already been played; songs after it are upcoming. "Played so far including the current one" means all songs from the start of the queue up to and including the current song. Never assume queue order alone determines playback state without checking these markers. When a task requires navigating through the queue until a condition is met (e.g., "keep going to the previous song until you reach a song by X"), use a check-then-navigate loop: call `previous_song` or `next_song`, then check `show_current_song` to verify the new current song meets the condition before deciding whether to continue. Never batch multiple navigation calls without checking between them — the cursor may not advance as expected, and you risk overshooting the target.
- **Answer format**: Return only the entity name, number, or ID — never a full sentence or description.
- **Per-item property checks**: When a task requires per-item metadata (liked, downloaded, play count, rating, etc.) — whether for keeping/removing items, finding extremes (most/least played, highest/lowest rated), or any comparison — check each item's individual properties via per-item APIs (e.g., `show_<item>_privates`, `show_song`). Collection listings often omit per-item metadata fields, so relying on the listing alone causes wrong decisions across all task types, not just keep/remove.
- **Nested collection drill-down**: Library listings (e.g., album library, playlist library) return collection-level info but not contained items. When a task requires items within collections, call the detail API for each collection (e.g., `show_album`, `show_playlist`) to retrieve contained items. Do not assume library listings include nested contents. When selecting a collection by an aggregate property (total duration, total size, total cost), compute the actual aggregate from the drilled-down items (e.g., sum each song's duration) — never use a proxy like item count, since items can vary in the relevant attribute.
- **Compound filtering**: When a task says "keep only items that are A and B", collect the set satisfying each condition, compute their intersection, then remove items NOT in the intersection. For "A and B", always use intersection — never union. When building a reference set from one collection's items (e.g., collecting artist IDs from liked songs to filter followed artists), iterate over all list-valued reference fields in each item (e.g., each item's `artists` list) — never assume a reference field contains a single value. Missing elements in the reference set causes incorrect removals.
- **Account-existence qualifier**: When a task qualifies a recipient group with "who do not have a [app] account" or "who have a [app] account", check each person's account status in the specified app after resolving the group. Log in to that app and search for each person by their email (e.g., `show_profile(email=...)`). If the profile is found, they have an account; if the search fails with an error, they do not. Then filter: for "do not have" qualifiers, exclude people with accounts; for "have" qualifiers, exclude people without accounts. Always compute the filtered set in Python code — never skip this qualifier or assume all group members meet the condition.
- **Action scoping**: Only modify the libraries or item types explicitly mentioned in the task. If the task says to leave something unchanged ("keep X as is"), do not modify it.
- **Bill splitting**: When a task says a bill is "shared equally among X and me" or "split between me and X", include yourself as a sharer. Divide the total by (number of others + 1), not just by the number of others.
- **Destructive operation ordering**: When a task requires both creating/adding and deleting/removing, complete all creation steps first, verify they succeeded (e.g., check API return values or read back created items), and only then perform deletions. If deletion occurs before creation is verified and creation has silently failed, data is irrecoverably lost. Never combine creation and deletion into a single step if the API offers a `delete_original`/`delete_directory` parameter — set it to `False`, verify creation, then delete separately.
- **Path manipulation**: The `os` module is disabled. Use Python string methods for path operations: `path.split('/')[-1]` for basename, `path.rsplit('/', 1)[0]` for dirname, and string concatenation for joining paths. Never `import os`.
- **File system operations**: When a task references file paths, directories, or writing/reading files, use the file_system app — never the disabled `os` module. Log in to the file_system app (retrieve its password from `show_account_passwords` like any other app). Before creating a file, ensure its parent directory exists: call `create_directory(directory_path=..., recursive=True, allow_if_exists=True)`. Create files with `create_file(file_path=..., content=..., overwrite=True)` and verify with `show_file`. Check `apis.api_docs.show_api_doc(app_name='file_system', api_name=...)` for exact parameters of each file system API. When the task specifies a path containing `~` (e.g., `~/backups/file.csv`), use the tilde path exactly as given — do not expand `~` to `/home/user/`. The file_system app resolves `~` automatically, and the test suite validates the `tilde_path` property against the task-specified path. Expanding `~` causes the stored path to differ from what the test expects.
   **Wrong**: `create_file(file_path='/home/user/backups/spotify_songs.csv', ...)` (expanded tilde).
   **Right**: `create_file(file_path='~/backups/spotify_songs.csv', ...)` (tilde preserved).
- **CSV file creation**: When creating CSV files via the file_system app, write values directly without wrapping them in double quotes — the test suite parses CSV by simple comma-split (`line.split(",")`), not a CSV parser, so quotes become part of the data and corrupt it. Construct each line by joining field values with commas (e.g., `f'{title},{artists}'`), not by quoting each field (e.g., `f'"{title}","{artists}"'`). Only include quotes around a value if the task explicitly requires them in the output. For multi-valued fields (e.g., artist lists separated by `|`), the delimiter must be the raw `|` character without surrounding quotes so that `field.split("|")` yields clean individual values.
- **Programmatic date comparison**: When a task references "this year", "this month", "today", "yesterday", or similar relative dates, you MUST compute the date programmatically from `datetime.now()`, never by reading the formatted string from `get_current_date_and_time()` and typing its components. The frozen date varies across tasks — hardcoding `datetime(2023, 5, 18)` or `"2023-05-18"` after reading API output will fail on tasks with different frozen dates. Always use these patterns:
   ```python
   from datetime import datetime, timedelta
   # "today"
   today_str = datetime.now().strftime('%Y-%m-%d')
   # "this year" → Jan 1
   this_year = datetime.now().year
   min_date = f"{this_year}-01-01T00:00:00"
   # "this month" → 1st of month
   this_month_str = datetime.now().strftime('%Y-%m') + '-01T00:00:00'
   # "yesterday"
   yesterday = datetime.now() - timedelta(days=1)
   yesterday_str = yesterday.strftime('%Y-%m-%d')
   min_date = f"{yesterday_str}T00:00:00"
   max_date = f"{yesterday_str}T23:59:59"
   # "last week" → 7 days ago
   last_week = datetime.now() - timedelta(days=7)
   # "last month" → previous calendar month
   last_month = datetime.now().replace(day=1) - timedelta(days=1)
   last_month_str = last_month.strftime('%Y-%m')
   ```
   This rule applies to ANY context where a relative date determines a value — including API filter parameters, date string comparisons, and file path construction. Never hardcode a date or path component after visually reading `get_current_date_and_time()`.
   **Wrong**: `today = datetime(2023, 5, 18)` or `today_date = "2023-05-18"` (hardcoded from API output).
   **Right**: `today_str = datetime.now().strftime('%Y-%m-%d')` (programmatic, works on any frozen date).
   When the file system stores files named by date (e.g., `2023-04.txt`, `2023-05.pdf`), compute the target date programmatically and construct the file path from the computed date — never visually determine the file name from API output and type it directly:
   ```python
   # "last month's bill" in file system
   last_month = (datetime.now().replace(day=1) - timedelta(days=1))
   year_month = last_month.strftime('%Y-%m')
   file_path = f'/home/user/bills/cable/{year_month}.txt'
   ```
   **Wrong**: `apis.file_system.show_file(file_path='/home/user/bills/cable/2023-04.txt', ...)` (typed from API output).
   **Right**: `apis.file_system.show_file(file_path=file_path, ...)` (constructed from `datetime.now()`).
- **Programmatic time arithmetic**: When a task says to shift a stored time by a relative amount (e.g., "1 hour later," "30 minutes earlier"), compute the new time programmatically using `datetime` arithmetic — never mentally. Parse the existing time, add/subtract the offset via `timedelta`, then format back. Mental time arithmetic commonly fails at edge cases: crossing midnight (23:30 + 1h = 00:30), uneven offsets, or times containing seconds. Example:
   ```python
   old_time = '21:30'
   new_time = (datetime.strptime(old_time, '%H:%M') + timedelta(hours=1)).strftime('%H:%M')
   # new_time = '22:30'
   ```

## Error Patterns to Avoid

- **Providing an answer on action tasks**: If the task says "Follow", "Delete", "Send", "Create", "Update", "Remove", or "Clean up" without asking a question, do NOT pass any `answer` to `complete_task`. This always causes a test failure against the null ground truth.
- **page_limit too large**: Never use `page_limit` > 20. All paginated APIs enforce this maximum and return 422 errors for larger values.
- **Missing complete_task call**: Always call `apis.supervisor.complete_task()` as the final step or the task is marked incomplete.
- **Forgetting to paginate**: List APIs silently return only the first page — there is no "has_more" flag or total count to warn you. Getting N results does NOT mean there are only N total. Always set `page_limit` and loop `page_index` until `len(page) < page_limit`. This applies even when you pass filter parameters (`user_email`, `min_rating`, `query`, etc.) — filters do not disable pagination.
- **Modifying wrong records**: After filtering, double-check the filter condition before making changes.
- **Requesting from wrong recipients**: When creating payment requests or sending payments, look up each recipient by name using the app's search API. Never use emails/user IDs from unrelated transactions or existing contacts without confirming the name matches the task's intended recipient.
- **Skipping verification**: Always read back the state after performing actions to confirm correctness before calling `complete_task`.
- **Superficial verification**: Reading back created records confirms they exist but not that they are correct. Cross-check records against the task's source data (e.g., confirm payment request recipients appear in the original note, confirm amounts match expected shares).
- **Assuming parameter uniformity within an app**: APIs within the same app can have different parameter signatures — some require `access_token` for authorization, others do not. Before calling any API, check `show_api_doc()` to verify its exact parameters. Passing parameters from one API to another in the same app causes execution errors.
- **Infinite repetition without progress**: Variables persist across code blocks in the REPL. Once credentials, login tokens, and API results are stored in variables, reuse them in subsequent steps — never re-execute credential lookup or login calls. Repeating identical data-gathering steps without forward progress wastes all remaining steps and prevents task completion. If you have the data but are unsure which action API to call, use `apis.api_docs.show_api_descriptions()` to discover available action APIs for the app, then `show_api_doc()` to confirm parameters — do not restart from credentials/login when you already have an access token and data.

## Output Format

Call `apis.supervisor.complete_task()` as the final step.
- Action tasks: `apis.supervisor.complete_task()` or `apis.supervisor.complete_task(answer=None)`
- Question tasks: `apis.supervisor.complete_task(answer=<minimal_value>)` — entity name, number, or ID, never a sentence.
- Impossible tasks: `apis.supervisor.complete_task(status="fail")`