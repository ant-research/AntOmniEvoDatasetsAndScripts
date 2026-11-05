# Action Verification Pattern

## When to Use
When the task requires determining which actors (people, accounts) from one data source have completed an action recorded in another data source (e.g., who has paid from a note's list of shares).

## Code Pattern

```python
import re

# 1. Parse the source document to extract expected actors and amounts
# Adapt the regex to the document format (notes, messages, files)
expected = {}  # {first_name: amount}
for line in document_content.split('\n'):
    match = re.match(r'- (\w+) => \$(\d+)', line)
    if match:
        expected[match.group(1)] = float(match.group(2))

# 2. Fetch ALL relevant records using structural filters ONLY (no query=)
all_records = []
page = 0
while True:
    batch = apis.<app>.<list_api>(
        access_token=token,
        direction='received',  # or other structural filter
        page_index=page,
        page_limit=20
    )
    if not batch:
        break
    all_records.extend(batch)
    if len(batch) < 20:
        break
    page += 1

# 3. Match each expected actor to a record by identity AND amount
matched_names = set()
for name, amount in expected.items():
    for record in all_records:
        sender_name = record['sender']['name']  # adapt field names
        sender_first = sender_name.split()[0]
        if sender_first == name and abs(record['amount'] - amount) < 0.01:
            matched_names.add(name)
            break

# 4. Compute remaining (unmatched) actors
unmatched = {name: amount for name, amount in expected.items() if name not in matched_names}
```

## Key Points
- NEVER pass `query=` when fetching records for verification — use structural filters only (direction, status, etc.).
- Match by sender first name (or full name/email) AND amount, not by description keyword.
- The amount comparison uses `abs(diff) < 0.01` to handle float precision.
- Collect ALL pages before matching — missing pages means missing matches.