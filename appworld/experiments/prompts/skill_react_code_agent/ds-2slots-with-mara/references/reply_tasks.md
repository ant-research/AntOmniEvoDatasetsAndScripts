# Reply & Message Tasks

Use for tasks of the form 'X asked for Y via a message (text/voice/email); reply with information from an app, as per their request.' Also use when the answer must be composed from free-form note/file/message content whose selection criteria live inside that content.

## 1. Identify the exact recipient
Search the contact/message app by the person's name. Name searches can return fuzzy or unrelated matches, so select only the record whose first/last name exactly equals the person in the request, then take their phone_number/email from that record — never retype it from the task wording.

## 2. Recover the actual request from the incoming message
Fetch the message thread with that recipient and read the incoming message(s) verbatim. The specific selection criterion (a director, genre, keyword, author, date, item) lives in the incoming message, not in the task's one-line summary. Build the reply around that specific ask.

## 3. Read the full source content
Fetch the source record (note, file, or email) and read its complete content string. Do not truncate or hand-pick lines.

## 4. Parse items into title + attributes
Before extracting, print `repr()` of the relevant lines so prefixes and formats are visible. Split content into entries and parse each entry's title and attribute lines programmatically:

```python
items = []
for block in content.split('\n\n'):
    lines = [ln.strip() for ln in block.split('\n') if ln.strip()]
    if not lines:
        continue
    title = lines[0].lstrip('#').strip()
    attrs = {}
    for ln in lines[1:]:
        if ln.startswith('-') and ':' in ln:
            key, val = ln.split(':', 1)
            attrs[key.lstrip('-').strip().lower()] = val.strip()
    if attrs:  # skip headings and non-item blocks
        items.append({'title': title, **attrs})
```

Match substrings (`keyword in line`) rather than exact equality; when a value sits on the line before or after its attribute line (e.g. a title above a ` - director:` line), take the adjacent line as the value.

## 5. Filter by the requested attribute
Keep only the items whose attribute values contain the requested term (case-insensitive). Exclude non-matching items — evaluators also assert that non-matching titles do NOT appear in the reply.

```python
term = '<constraint from the incoming message>'.lower()
matching = [it for it in items
            if term in ' | '.join(str(v).lower() for v in it.values())]
```

## 6. Compose, send, and verify
Join the matched titles exactly as they appear in the source (preserve casing) as a comma-separated string: `answer = ', '.join(it['title'] for it in matching)`. Send that string to the recipient's exact phone_number/email, then re-read the sent message to confirm both the content and the recipient before calling `complete_task`.