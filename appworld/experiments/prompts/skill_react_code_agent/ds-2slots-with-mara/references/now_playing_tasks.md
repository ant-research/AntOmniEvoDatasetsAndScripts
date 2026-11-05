# Now-Playing / Currently-Playing-Song Tasks (answer about, or act on, the current song)

Use for tasks whose target entity is the SONG CURRENTLY PLAYING (or paused): read-only questions about it or its artist ('Who is the artist of the currently playing song?', 'How many people follow the artist of the currently playing song?'), or state changes scoped to that single song. For 'every song played so far in my queue' PREFIX tasks, route to `references/music_player_queue.md` instead. For 'add every song by <artist>' subset tasks, route to `references/music_library_tasks.md`.

## 1. Fetch the now-playing detail API, never parse the queue
The source of truth for 'the currently playing song' is the app's now-playing/current-song detail API (a `show_current_song` / `show_now_playing`-style call taking `access_token`), not a brute-force read of the whole queue. Print its doc first, then call it and store the result:

```python
print(apis.api_docs.show_api_doc(app_name='<music_app>', api_name='<now_playing_api>'))
current = apis.<music_app>.<now_playing_api>(access_token=<token>)
print(current)
```

## 2. Answer from the current song's own fields, and hop to nested entities
Read the requested value off the returned song object. When the question is about the song's ARTIST ('who is the artist of the currently playing song', 'how many people follow the artist'), take the artist id from the song's nested `artists` list — never re-search the artist by the name printed in the song, which can match a decoy or miss the credited id. A question phrased 'the artist' (singular) has one credited artist; when several are credited and the question names one, match that name:

```python
artist_id = current['artists'][0]['<artist_id_key>']
```

For a 'how many people follow ...' (or any other artist attribute) question, hop to the per-entity artist detail API with that id and read the mapped field:

```python
print(apis.api_docs.show_api_doc(app_name='<music_app>', api_name='<artist_detail_api>'))
artist = apis.<music_app>.<artist_detail_api>(<artist_id_param>=artist_id)
print(artist)
```

Map the question's verb to the field NAME by printing the detail doc first and reading its response schema: 'follow'/'followers'/'how many people follow' -> `follower_count`. If the schema names the field differently (e.g. `followers` or `follower_quantity`), use the schema's name exactly — never assume `follower_count`. The detail API returns the count directly; do not re-count or paginate anything on top of it.

## 3. Read-only discipline, then answer bare
A question about the current song is read-only: call ONLY read APIs (now-playing, show/detail, doc viewers) — any write API persists a record and fails the evaluator's `assert no model changes` check. Then call `apis.supervisor.complete_task(answer=<bare value>)`: the artist's name for 'who is', the bare number for a count, never a sentence or a unit.