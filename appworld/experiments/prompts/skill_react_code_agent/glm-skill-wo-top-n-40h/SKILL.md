---
name: appworld-coding
description: AppWorld coding agent skill for API interaction, task completion, pagination, and error handling across all apps.
---

## Overview
Complete day-to-day tasks in the AppWorld environment by writing Python code that interacts with app APIs, following a structured procedure with verification gates.

## Procedure

1. Get credentials and profile in one code block — both calls are mandatory even if you think you know the username:
   ```python
   passwords = apis.supervisor.show_account_passwords()
   profile = apis.supervisor.show_profile()
   print(passwords)
   print(profile)
   ```
   Then login to required apps. **Use the right username per app**: for the phone app, use `profile['phone_number']`; for other apps, use `profile['email']`. If a login returns 401 (Invalid credentials), try the alternative field (email ↔ phone_number) — never guess or hardcode credentials.
2. Discover APIs: for every app you will interact with, call `apis.api_docs.show_api_descriptions(app_name=...)` first, then `apis.api_docs.show_api_doc(app_name=..., api_name=...)` for each API you plan to use — **you MUST do this before any app API calls, including the very first one**. **Batch all `show_api_doc` calls for the same app into a single code block** — each call is independent, so reading 5–10 docs in one step saves 4–9 steps versus one doc per step. Verify every kwarg you plan to pass matches the spec exactly — never guess parameter names or assume they follow a convention. Skipping this causes 422 errors from wrong parameters and KeyErrors from assuming wrong field names. Track read docs in a set — gate with code, not prose:
   ```python
   _api_docs_read = set()  # Track as '(app_name, api_name)' tuples
   # Batch all doc reads per app in one code block:
   apis.api_docs.show_api_doc(app_name='spotify', api_name='show_song_library')
   _api_docs_read.add(('spotify', 'show_song_library'))
   apis.api_docs.show_api_doc(app_name='spotify', api_name='show_album_library')
   _api_docs_read.add(('spotify', 'show_album_library'))
   # ... all other spotify APIs in the same block
   print(f'Docs read so far: {_api_docs_read}')
   ```
   Gate: before calling any app API, assert `(app_name, api_name) in _api_docs_read` — if not, read it now. Store the API's response field names from the doc and reference them instead of guessing (e.g., the doc shows `path` not `file_path`). **Also record whether each API accepts `page_index`/`page_limit`** so you know whether to use `paginated_call` (paginated) or call directly (non-paginated).
3. Define SKILL_DIR (copy the path from the Skill Resources section in this prompt), then load pagination helper and read the pagination reference in a single code block — both lines are required, do not skip either. **This step is NOT optional even if the task seems simple or the dataset small — skipping it causes silent data loss on every list API call.**
   ```python
   from pathlib import Path
   SKILL_DIR = Path('<path from Skill Resources>')
   exec((SKILL_DIR / 'scripts/pagination.py').read_text())
   print((SKILL_DIR / 'references/pagination.md').read_text())
   assert 'paginated_call' in dir(), 'Pagination helper not loaded — do not proceed without it'
   assert '_api_docs_read' in dir() and len(_api_docs_read) > 0, 'Step 2 not completed — read API docs for every app/API before proceeding'
   ```
   AFTER loading: use `paginated_call` for every paginated list API call. A list API is paginated if and only if its API doc includes `page_index`/`page_limit` parameters — you learn this by reading the API doc in step 2. If the API doc shows these params, use `paginated_call`; if absent, call the API directly (it is not paginated — passing it through `paginated_call` injects invalid kwargs causing a 422). **If you find yourself calling any `search_*`, `show_*`, or list API without having completed this step, stop immediately and complete it first.**
4. When the task references a prior message, request, or communication (e.g., "X has asked for…", "reply to their email/text"), retrieve and read that communication FIRST. **Use topic keywords from the task as the search query** — e.g., if the task says "movie recommendations", search with `query='movie'` not `query='Laura'`. Topic keywords return fewer, more relevant results than a person's name. Use the relevant app's search/show API (e.g., `search_text_messages`, `show_text_message_window`, `show_inbox_threads`). The prior message contains the specific request details — never assume scope from the task description alone. **Crucial: after retrieving messages, immediately filter for the specific message you need and store it in a named variable — do NOT print all results. Filter using a list comprehension that matches BOTH the referenced sender's name/phone AND the task's topic, then print only the matching message's key fields (sender, message text, id). Example: `target_msg = [m for m in messages if m['sender']['name'] == sender_name and topic_keyword in m['message']][0]; print(target_msg['message'])`. Never print more than 5 messages from any search result.**
5. **After retrieving a referenced communication (step 4), parse its request into a concrete action plan** — identify: (a) WHICH other app(s) have the requested data, (b) WHICH API to call on each app, (c) WHAT filter or criterion the message specifies. Then immediately execute those retrievals in the SAME step. Example workflow: message says "Do you have recommendations for movies by director X?" → retrieve notes from the relevant app via `paginated_call` → filter for director X → store results in a named variable for the reply. Do NOT return to step 1 or step 4 after extraction — proceed directly to composing and sending the reply.
6. **Do not repeat steps**: once credentials are obtained and logins are complete (step 1), store tokens in durable named variables (e.g., `phone_token`, `spotify_token`) and reference them in later steps — never re-login unless a token expires. If a variable from a prior step is already defined (check with `if 'var_name' in dir()`), reuse it — do not re-derive it. After each step completes successfully, move to the NEXT procedure step; never restart from step 1. **Before executing any code block that calls an app API, check prerequisites at the top of the block** — if `_api_docs_read` is not defined or `paginated_call` is not in `dir()`, add the missing setup calls at the top of the block before proceeding. Collect full snapshot BEFORE any mutation: read all reference data needed for decisions using `paginated_call` for paginated list APIs. Never interleave reads and writes on the same entity — mutations change what subsequent reads return.
7. **Compute relative dates programmatically** — never hardcode a date or year read from output. Always use `datetime.now()` to derive dates — do NOT use `apis.phone.get_current_date_and_time()` because its format requires non-standard parsing:
   ```python
   from datetime import datetime, timedelta
   now = datetime.now()
   today = now.replace(hour=0, minute=0, second=0, microsecond=0)
   yesterday = today - timedelta(days=1)
   last_month = today.replace(day=1) - timedelta(days=1)
   current_year = now.year
   current_month = now.month
   ```
   Reference these variables (`yesterday`, `today`, `last_month`, `current_year`, `current_month`) in filtering logic AND in constructing file paths/identifiers — e.g., `f'/home/user/bills/cable/{last_month.strftime("%Y-%m")}.txt'`. When a task says 'this year', use `current_year`; 'this month', compare against `current_month`. Never transcribe a date literal, year, or month from API output.
8. For question tasks, derive the answer from the fully collected data (count, rank, filter) — see Question Resolution.
9. Make all decisions from the snapshot, then execute all modifications in one pass — all mutations (creates, moves, deletes, updates) across all entity types go into a single code block, not one block per entity type. Splitting mutations across steps wastes the step budget and risks incomplete execution if steps run out.
10. Verify: re-query the final state and confirm every change matches the task criteria. Confirm every paginated list API call used `paginated_call` — if any were called directly, re-fetch with `paginated_call` before proceeding. Non-paginated list APIs are fine called directly.
11. Call `apis.supervisor.complete_task()` as the final step — see Output Format.

## Rules

- **Paginate all paginated list APIs via `paginated_call` only**: never call a paginated list API directly — always use `paginated_call(api_func, **kwargs)` after loading `scripts/pagination.py`. A list API is paginated if and only if its API doc includes `page_index`/`page_limit` parameters — only those must go through `paginated_call`. Some list-returning APIs (e.g., `file_system.show_directory`, `file_system.show_file`) do NOT accept pagination parameters; calling them through `paginated_call` injects invalid kwargs and causes a 422 error. Before using `paginated_call`, check the API doc for `page_index`/`page_limit` — if absent, call the API directly (it is not paginated). Manual pagination is error-prone; `paginated_call` handles everything automatically.
- **Use server-side filters**: when an API supports filter parameters (e.g., `min_follower_count`, `genre`, `query`, `sort_by`), pass them as kwargs to `paginated_call` instead of fetching all results and filtering in Python. This includes `query` on transaction/payment/message list APIs — pass the description or keyword to get only relevant records.
- **Snapshot before mutate**: collect ALL data before any state-changing call using `paginated_call`. After a mutation, previously fetched data is stale — re-fetch if you need current state.
- **Batch nested entity retrieval**: when a task requires fetching detail records for each item in a paginated list (e.g., songs from each album or playlist), collect the parent list with `paginated_call`, then loop through all parents in a single code block calling the detail API for each. Do not print intermediate results — only print the final count or summary. This avoids consuming one step per parent entity.
- **No guessing**: look up credentials, IDs, and dates through APIs or `datetime.now()`, never from internal knowledge. Compute relative dates programmatically as shown in Procedure step 7 — never hardcode a date derived from visually reading output. Store looked-up values in variables and reference them programmatically — never manually transcribe API output into subsequent code blocks. This applies to ALL values from prior API output: (1) credentials — reference the `passwords` dict from step 1 (e.g., `passwords[2]['password']`) instead of retyping password strings; (2) entity IDs — after fetching a list, find the target with a comprehension (e.g., `target = [a for a in alarms if 'sleep' in a['label'].lower()][0]`) and reference `target['alarm_id']` instead of hardcoding the ID; (3) numeric values — extract amounts via parsing (dict access, regex) rather than hardcoding what you visually read; (4) dates and times — use `datetime.now()` and computed variables, or parse from prior output programmatically (e.g., `datetime.strptime(alarm['time'], '%H:%M') + timedelta(hours=1)`).
- **Disambiguate by cross-referencing**: when multiple entities (notes, contacts, transactions) match a query, use additional constraints — date (`"yesterday"` = `datetime.now() - timedelta(days=1)`), description keywords, or corroborating data from other apps — to pick the right one. Prefer the candidate whose related records confirm the match.
- **Cross-source intersection for multi-criteria selection**: when a task requires items satisfying conditions from multiple sources (e.g., 'songs in playlists AND liked'), collect IDs from each source into sets, then compute the intersection: `result_ids = source_a_ids.intersection(source_b_ids)`. This is faster and less error-prone than nested loops. Apply the same pattern for any task that says 'X that are also Y' or 'X which have Y'.
- **Sort explicitly for superlatives**: when a task uses 'last', 'first', 'most recent', 'earliest', 'newest', 'oldest', or any ordinal selector, explicitly sort the candidate list by the relevant timestamp or numeric field (e.g., `sorted(items, key=lambda x: x['created_at'], reverse=True)[0]`) — never assume the API returns results in chronological order.
- **Deduplicate across sources**: when combining entities from multiple API sources (e.g., song library + album library + playlist library), deduplicate by entity ID before making decisions. The same entity can appear in multiple sources; without dedup, duplicates bias counts and rankings. **Beware: the ID field name can differ across sources** — e.g., songs from `show_song_library` use `song_id`, but songs nested inside `show_album`/`show_playlist` responses use `id`. Normalize to a single field name before deduplicating: `song.get('song_id') or song['id']`.
- **Use entity-level fields, not parent fields**: when a task asks about a property of individual entities (e.g., "oldest released song"), always look up per-entity data rather than using a parent object's field (e.g., album `release_date`). Parent and child fields can differ.
- **Resolve people by relationships via the phone app**: when a task references personal relationships (siblings, parents, friends, etc.), use `phone.search_contacts(relationship=...)` via `paginated_call` to identify those people, then use their identifying fields (email, phone_number) as keys to match records in other apps (venmo, gmail, splitwise, etc.). The phone app is the canonical source for relationship data.
- **File system**: references to 'file system' mean the `file_system` app, not the OS.
- **CSV formatting**: when creating CSV files via the file_system app, include a header row. Use comma as the delimiter. **Do NOT wrap fields in quotes unless a field actually contains a comma or a quote** — wrapping clean fields in quotes causes the quotes to become part of the parsed value. When quoting is necessary (field contains a comma, quote, or newline), wrap the field in double quotes and escape internal quotes by doubling them (`""`). **Never add a space after the comma delimiter** — the space becomes part of the next field's value on parse. **Never strip or remove commas (or any characters) from field values to avoid CSV escaping** — this silently corrupts the data. If a field contains a comma, quote the field; do not remove the comma. Build each line programmatically — define a helper that quotes only when needed: `def csv_field(v): return f'"{v.replace(chr(34), chr(34)*2)}"' if (',' in v or '"' in v or '\n' in v) else v`. Then construct lines like `f'{csv_field(title)},{csv_field(artists)}'`. Example correct: `A Symphony Written in Tears,Aria Sterling` or multi-artist: `Eternal Solitude,Hazel Winter|Ava Morgan` or comma in title: `"Hello, Goodbye",The Beatles`. Example incorrect: `"A Symphony Written in Tears", "Aria Sterling"` (quotes and space corrupt) or `Hello Goodbye,The Beatles` (comma stripped corrupts value).

## Error Patterns to Avoid

- Passing a non-null `answer` to `complete_task` for action tasks — see Output Format.
- Using `page_limit > 20` — all AppWorld paginated APIs reject this with a 422 error.
- Calling a paginated list API directly (without `paginated_call`) — the default page size is 5 and you will silently miss results, producing wrong decisions. **There is NO visible error when this happens — the API returns a partial page and looks complete.** Every paginated API (one with `page_index`/`page_limit` parameters in its doc) must go through `paginated_call`. Non-paginated list APIs (no `page_index`/`page_limit` in their doc) must be called directly — `paginated_call` injects invalid kwargs. **Before calling ANY API that returns a list or collection, check its doc for `page_index`/`page_limit` — even APIs whose names don't look like list APIs (e.g., `show_song_reviews`, `show_expenses`) can be paginated.** If you called any list API without `paginated_call`, assume the results are incomplete and re-fetch with `paginated_call` before using them.
- Calling a list API before loading the pagination helper — the first unpaginated call returns only a partial page and is wasted work. Load the helper first (Procedure step 3). If you find yourself calling any `search_*`, `show_*`, or list API without `paginated_call` defined, stop and load it immediately.
- Interleaving reads and writes — mutations invalidate prior reads. Snapshot first, mutate second.
- Skipping retrieval of referenced prior communications — when the task says "X has asked for…" or "reply to their message", the specific request lives in that prior message. Failing to retrieve it causes wrong scope (e.g., sending all items instead of the filtered subset the person actually requested).
- Guessing API response field names — when skipping Procedure step 2 (API doc discovery), you risk KeyErrors from assuming fields that don't exist (e.g., assuming 'status' when the API uses approved_at/denied_at). Always read the API doc before accessing response fields; if a KeyError occurs, read the doc immediately and fix the code.
- Verbose `answer` strings — `answer` must be a minimal value (name, number, list), never a full sentence or summary.
- Deriving answers from incomplete data — after paginating, compute the answer from the FULL collected list, not from a single page.
- Incomplete data for decision-making — when determining who has/hasn't completed an action (e.g., who paid, who responded), fetch ALL relevant records with `paginated_call` and match by exact identifiers, not by keyword search on a single unpaginated page.
- Accidental substring inclusion in reply messages — when composing a message that includes a filtered subset of items from a list (e.g., only items matching a criterion), the conversational text wrapping the item list must NOT contain any word that is a case-insensitive substring of an excluded item. Example: "Here are my recommendations" fails because "Her" is a substring of "Here" and "Her" may be an excluded item name. Compose concise messages that start directly with the item list or use wording that avoids common entity-name words (e.g., "Recommendations: item1, item2" instead of "Here are my recommendations: item1, item2").

## Question Resolution

For question tasks ("most", "least", "how many", "which", "what"), compute the answer from fully collected data:
- **Count/rank**: use `collections.Counter` on the target field across all results to find the most/least frequent item. Do not assume a ranking or score field exists; derive the ordering from the data. When the target field is a list (e.g., `artists`, `genres`, `tags`), flatten it by iterating through each element — count each element individually, not the list as one occurrence:
  ```python
  counter = Counter()
  for item in all_results:
      for val in item['list_field']:
          counter[val] += 1
  ```
- **Filter**: use list comprehensions on the full result set to narrow by criteria.
- **Aggregate**: sum, count, or rank after `paginated_call` returns the complete list — never compute from a single page.

## Output Format

Call `apis.supervisor.complete_task()` as the final step:

- **Question task** (asks "How many…?", "What is…?", "Who…?", "Which…?"): pass the minimal answer as `answer` — entity names, numbers, or lists only, no full sentences.
- **Action task** (imperative: "Follow…", "Create…", "Delete…", "Send…", "Add…", "Remove…", "Update…"): call `complete_task()` without `answer` or with `answer=None`. Never pass a descriptive string.
- **Impossible task**: call `complete_task(status='fail')`.

Decision test: if the task instruction starts with an imperative verb and does not contain a question word, it is an action task — omit `answer`.

## References

- `scripts/pagination.py` — BEFORE any list API call: `exec((SKILL_DIR / 'scripts/pagination.py').read_text())`
- `references/pagination.md` — BEFORE first list API call: `print((SKILL_DIR / 'references/pagination.md').read_text())`