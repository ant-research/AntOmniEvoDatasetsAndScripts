# Common AppWorld Task Workflows

## Search Entity → Fetch Related Items → Filter → Act

This is the most common AppWorld task pattern: find an entity (artist, seller, contact) by name, then fetch its related items (songs, products, messages) with pagination, filter by a criterion, and act on the matches.

### Step-by-step Example

```python
# 1. Search for the entity by name
all_entities = []
page = 0
while True:
    batch = apis.<app>.search_<entities>(query="<entity name>", page_index=page, page_limit=20)
    if not batch:
        break
    all_entities.extend(batch)
    if len(batch) < 20:
        break
    page += 1

# 2. Extract the entity ID programmatically — never hardcode from printout
entity_id = next(e['<id_field>'] for e in all_entities if e['name'] == '<entity name>')

# 3. Fetch related items using the extracted ID with a pagination loop
all_items = []
page = 0
while True:
    batch = apis.<app>.search_<items>(<id_field>=entity_id, page_index=page, page_limit=20)
    if not batch:
        break
    all_items.extend(batch)
    if len(batch) < 20:
        break
    page += 1

# 4. Filter the COMBINED results (all pages already in all_items)
filtered = [item for item in all_items if item['<criterion_field>'] > <threshold>]

# 5. Act on each filtered item
for item in filtered:
    apis.<app>.<action_api>(<item_id_field>=item['<item_id_field>'], access_token=token)
```

### Key Principles
- **Programmatic ID extraction**: Use `next(e['id'] for e in results if e['name'] == target)` — never hardcode IDs read from printed output. The entity might not be on page 0, and IDs change across instances.
- **While-loop pagination in a single variable**: Collect all pages into one list (`all_items`) before filtering. Manual pagination into separate variables (`page1`, `page2`) tempts filtering on partial data.
- **Filter on the combined list**: After the while-loop, `all_items` contains every page. Filter this combined list, not a single page's variable.
- **Call show_api_doc first**: Before writing any API call, verify its parameter names and response fields via `show_api_doc` — parameter names often differ from expectations.