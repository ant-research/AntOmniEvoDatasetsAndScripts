# Pagination Patterns for AppWorld Collection APIs

## Key Constraint
All paginated APIs in AppWorld enforce `page_limit <= 20`. Using a value greater than 20 causes a 422 validation error. Always use `page_limit=20` to minimize the number of API calls.

## Standard Pagination Pattern

```python
results = []
page = 0
while True:
    batch = apis.<app>.<collection_api>(access_token=token, page_index=page, page_limit=20)
    if not batch:
        break
    results.extend(batch)
    if len(batch) < 20:
        break
    page += 1
```

## Search/Show API with Filters

Search and show APIs that accept `page_index`/`page_limit` require the same pagination loop as list APIs. Include filter parameters alongside pagination parameters — the loop is identical:

```python
results = []
page = 0
while True:
    batch = apis.<app>.<search_api>(
        access_token=token,
        <filter_param>=<filter_value>,  # e.g., phone_number='1234567890'
        page_index=page,
        page_limit=20
    )
    if not batch:
        break
    results.extend(batch)
    if len(batch) < 20:
        break
    page += 1
```

## Rules
- Always use `page_limit=20` to minimize the number of API calls.
- Always write the full pagination loop even when you expect few results — you cannot know the total count before fetching. Fetching only page_index=0 without a loop silently misses data when results span multiple pages.
- Always loop until `not batch` or `len(batch) < 20` to handle partial last pages.
- Collect all results before processing — never act on partial data.
- After any modification (add/remove items), re-query to verify the change if verification is needed before calling `complete_task`.

## Common Mistake: Hardcoded page_index in Loop

WRONG — `page_index` is hardcoded to 0 inside the loop, causing an infinite loop fetching page 0 repeatedly:
```python
results = []
batch = apis.<app>.<api>(access_token=token, page_index=0, page_limit=20)
results.extend(batch)
while len(batch) == 20:
    batch = apis.<app>.<api>(access_token=token, page_index=0, page_limit=20)  # BUG: always page 0
    results.extend(batch)
```

RIGHT — use an incrementing `page` variable:
```python
results = []
page = 0
while True:
    batch = apis.<app>.<api>(access_token=token, page_index=page, page_limit=20)
    if not batch:
        break
    results.extend(batch)
    if len(batch) < 20:
        break
    page += 1
```