# Question-Type Computation Patterns

Patterns for computing answers to common AppWorld question types.
Read this when the task asks for a least/most/extreme value or a top-N list.

## Table of Contents

- [Case-Insensitive String Matching](#case-insensitive-string-matching)
- [Least/Most Questions](#leastmost-questions)
- [Top-N List](#top-n-list)

## Case-Insensitive String Matching

When filtering records by a string attribute (genre, category, type, etc.),
always compare case-insensitively. API responses may store values in any case:

```python
# WRONG — misses 'EDM', 'Edm', etc.
if song['genre'] == 'edm':

# CORRECT — matches regardless of casing
if song['genre'].lower() == 'edm':
```

Apply `.lower()` (or `.casefold()` for Unicode) to both the API value and the
target string. This applies to any string-based filter: genre, category, type,
status, or label.

## Least/Most Questions

When the task asks for the "least" or "most" of something
(e.g., "least recommended artist", "most common category"),
count entity occurrences across all paginated results and find the min/max.

### Steps

1. Fetch all items via `get_all_pages` (see `references/pagination.md`).
2. For each item, iterate ALL instances of the target entity
   (e.g., all artists in a song, not just the first).
3. Count occurrences using `collections.Counter`.
4. Find min or max count:
   `min(counter.items(), key=lambda x: x[1])` or
   `max(counter.items(), key=lambda x: x[1])`.
5. Return the entity name as the answer.

### Key Detail: Multi-Entity Items

Some items contain multiple instances of the target entity
(e.g., a song with multiple artists). Always iterate all of them:

```python
from collections import Counter

counter = Counter()
for song in all_songs:
    for artist in song['artists']:
        counter[artist['name']] += 1

least = min(counter.items(), key=lambda x: x[1])
most = max(counter.items(), key=lambda x: x[1])
```

Do not count only the first or primary entity — that produces wrong answers
when the target entity appears in a non-primary position.

### Numeric Attribute Comparison

When the task asks for the "least" or "most" of a numeric attribute
(e.g., "least-played song", "most expensive product", "longest movie"),
compare the attribute directly — do not count occurrences.

1. Fetch all items via `get_all_pages`.
2. If the list API does not include the needed attribute (e.g., `play_count`,
   `price`, `duration`), call the detail API (e.g., `show_song`, `show_product`)
   for each item to retrieve it.
3. Use `min()` or `max()` with a `key` lambda on the attribute:
   ```python
   least_played = min(song_details, key=lambda x: x['play_count'])
   most_expensive = max(products, key=lambda x: x['price'])
   ```
4. Return the entity name/ID as the answer.

Do NOT use `Counter` for this pattern — Counter counts how often an entity
appears, which is unrelated to comparing numeric attribute values.

### Tie Handling

If multiple entities share the same min/max count, check the task wording.
If it implies a unique answer, re-examine the data or criteria.
Otherwise, return all tied entities as a sorted list.

## Top-N List

When the task asks for the "top N" items (e.g., "top 6 most played",
"3 least expensive"), sort all candidates and take the first N:

1. Fetch all items via `get_all_pages` and fetch details for any missing attributes.
2. Sort by the target attribute — descending for "most" / "top", ascending for "least":
   ```python
   # Top N most played
   sorted_songs = sorted(all_songs, key=lambda x: x['play_count'], reverse=True)
   top_n = sorted_songs[:6]

   # N least expensive
   sorted_products = sorted(all_products, key=lambda x: x['price'])
   bottom_n = sorted_products[:3]
   ```
3. Return entity names as a comma-separated list (no spaces after commas):
   `','.join(song['title'] for song in top_n)`

Do NOT use `max()` or `min()` repeatedly to find top-N — that returns the
same item each time. Sort the full list and slice instead.