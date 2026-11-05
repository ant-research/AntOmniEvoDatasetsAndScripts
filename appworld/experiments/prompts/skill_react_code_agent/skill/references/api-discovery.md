# API Discovery Workflow

## When to Use
Before writing ANY task-logic code that calls app APIs, execute the API discovery code block below to check all needed API signatures. This replaces guesswork about parameter names and response fields with verified signatures.

## Code Pattern

```python
# Step 1: Identify which apps and APIs you need from the task instruction.
# Step 2: For EACH API, call show_api_doc and store the result.

# Example: task needs spotify show_liked_songs, show_playlist_library, show_playlist, download_song
api_docs_cache = {}

for api_name in ['show_liked_songs', 'show_playlist_library', 'show_playlist', 'download_song']:
    doc = apis.api_docs.show_api_doc(app_name='spotify', api_name=api_name)
    api_docs_cache[api_name] = doc
    print(f"--- {api_name} ---")
    print(doc)
    print()

# Step 3: Read the output. For each API note:
#   - Required parameters (especially access_token, page_index, page_limit)
#   - Response field names (e.g., 'id' vs 'song_id', 'name' vs 'title')
#   - Whether it accepts page_index/page_limit (if yes, must paginate)
```

## Rules
- Execute this code block as a SEPARATE code block BEFORE any task-logic code.
- Include EVERY API you plan to call — omitting one means guessing its signature.
- If a KeyError occurs later on a response field you did not verify, STOP and call show_api_doc for that API before retrying — never guess an alternate field name.
- If you discover an API accepts page_index/page_limit, you MUST paginate per references/pagination.md.
- Use the printed doc output to write subsequent API calls with correct parameter names and response field access.
- Use bracket access (response['field']) for fields confirmed by the doc, not .get() with defaults.