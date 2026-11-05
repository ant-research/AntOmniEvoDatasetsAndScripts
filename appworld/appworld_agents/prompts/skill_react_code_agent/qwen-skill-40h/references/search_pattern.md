# Search Pattern for AppWorld APIs

## When to Use

Use search APIs to efficiently locate specific items rather than iterating through all results.

## Why It Matters

Search APIs are optimized for finding items matching specific criteria. They are faster and more accurate than manual iteration.

## Critical Rule: Use Exact Descriptions for Transaction Searches

**WHEN searching Venmo transactions by description**: Use the EXACT description string from your source of truth (notes, receipts, etc.).

**WRONG**: `query="dinner"` - returns unrelated transactions like "Seafood Dinner", "Brunch Dinner", etc.

**CORRECT**: `query="Work Dinner"` - returns only transactions with that exact description.

```python
# Extract the exact description from your note/source first
# Note content: "# What I paid for whom\n- Kayla => $20\n..."
# Expected payment description: "Work Dinner" (from task instruction)

# Use the EXACT description to find who paid
transactions = apis.venmo.show_transactions(access_token=token, query="Work Dinner")
# This returns ONLY transactions with description "Work Dinner"

# Compare against your expectation list to find who hasn't paid
# Expected: Kayla $20, Sandra $19, Stephen $20, Jamie $15, Anita $23, Travis $39, Sabrina $39
# Found paid: Kayla, Anita, Sabrina (from transactions)
# Still owe: Sandra, Stephen, Travis
```

## General Pattern

```python
# Search with relevant keywords
results = apis.<app>.search_<item>(access_token=token, query="<relevant_keyword>")

# If multiple results, identify the correct one by:
# 1. Most recent (check created_at/updated_at)
# 2. Best match to your criteria
# 3. Additional verification by fetching full details
```

## Examples

### Find Notes by Topic

```python
# Find notes about dinner
notes = apis.simple_note.search_notes(access_token=token, query="dinner")
# Then pick the most relevant by title/content
```

### Find Transactions by Description

```python
# CRITICAL: Use exact description from task/note, NOT generic keywords
# Task says: "Make payment requests for others with a description note 'Work Dinner'"
# So search for transactions with query="Work Dinner" to find who already paid
transactions = apis.venmo.show_transactions(access_token=token, query="Work Dinner")
# Returns only transactions matching the exact description
```

### Payment Reconciliation Pattern

**CRITICAL: Always paginate payment/transaction APIs** - Use `fetch_all()` for `show_received_payment_requests()`, `show_transactions()`, and `show_social_feed()`. These APIs return paginated results. Skipping pagination causes incomplete reconciliation.

**WHEN** you need to create payment requests for people who haven't paid:

1. **Extract expected shares from source** (note, receipt, task description)
2. **Find paid transactions** using `fetch_all()` with exact description from the same source
3. **Compute set difference**: expected_people - paid_people = still_owe
4. **Create requests only for those who still owe**, using amounts from the source

```python
# Step 1: Extract expected shares from note
# note_content: "# What I paid for whom\n- Stephen => $33\n- Sabrina => $32\n- Jamie => $31\n- Allison => $26\n- Stacy => $28\n- Travis => $21\n- Kristin => $38\n- Anita => $32\n- Kayla => $16"
expected_shares = {
    "Stephen": 33, "Sabrina": 32, "Jamie": 31,
    "Allison": 26, "Stacy": 28, "Travis": 21,
    "Kristin": 38, "Anita": 32, "Kayla": 16
}

# Step 2: CRITICAL - Use fetch_all() to get ALL paid transactions
exec((SKILL_DIR / 'scripts/pagination.py').read_text())
paid_transactions = fetch_all(apis.venmo.show_transactions, access_token=token, query="Dinner with Colleagues")
# Or use exact matching by checking sender email
paid_people = {t['sender']['name'] for t in paid_transactions if t['receiver']['email'] == my_email}
# paid_people = {"Allison", "Kayla", "Stacy", "Travis"}

# Step 3: Compute who still owes
still_owe = {name: amount for name, amount in expected_shares.items() if name not in paid_people}
# still_owe = {"Stephen": 33, "Sabrina": 32, "Jamie": 31, "Kristin": 38, "Anita": 32}

# Step 4: Look up user emails and create payment requests for those who still owe
for name, amount in still_owe.items():
    users = apis.venmo.search_users(access_token=token, query=name)
    # Find exact match by full name
    user = next(u for u in users if f"{u['first_name']} {u['last_name']}" == name)
    apis.venmo.create_payment_request(
        access_token=token,
        user_email=user['email'],
        amount=amount,
        note="Dinner with Colleagues"
    )
```

**Critical Rules:**
- Only create payment requests for people in `still_owe`, not those who already paid
- Use amounts from the source note, not from transaction amounts
- Filter transactions by exact description to avoid mixing unrelated payments
- ALWAYS use `fetch_all()` for payment/transaction APIs - failing to paginate causes incomplete payment reconciliation

### Find Users by Name

```python
# Find users whose name matches
users = apis.venmo.search_users(access_token=token, query="Sandra")
# CRITICAL: Verify exact match by checking first_name AND last_name
# Search may return multiple users with similar names
user = next(u for u in users if u['first_name'] == 'Sandra' and u['last_name'] == 'Martinez')
# Extract exact email from the verified match for payment requests
```

**Always verify user identity:**
- Check both first_name and last_name match your target exactly
- Don't assume the first result is correct
- If multiple matches exist, use additional context (email pattern, task description) to identify the right person

### Disambiguate by Owner/Additional Metadata

**WHEN** your search returns multiple items with the same name (e.g., multiple playlists, notes, or folders with identical titles):**

1. **Identify the disambiguation criterion**: The task often implies which item you need (e.g., "my playlist" = owned by current user, "Laura's note" = owned by Laura).
2. **Get the user's profile** to know their email/identifier.
3. **Filter search results by owner or other metadata** to find the correct item.

```python
# Example: "Play the most listened to song from my Woodstock playlist"
# Search returns multiple playlists with same name from different owners
playlists = apis.spotify.search_playlists(query="Woodstock", access_token=token)

# Get current user's profile to identify 'my' playlists
profile = apis.supervisor.show_profile()
my_email = profile['email']

# Filter by owner email to find the user's own playlist
my_playlist = next(p for p in playlists if p['owner']['email'] == my_email)

# If owner field not available, fetch details for each candidate and check
for playlist in playlists:
    details = apis.spotify.show_playlist(playlist_id=playlist['playlist_id'], access_token=token)
    if details['owner']['email'] == my_email:
        my_playlist = details
        break
```

**Common disambiguation criteria:**
- Owner email (most common for playlists, notes, shared documents)
- Created/modified date ("the most recent", "yesterday's")
- Privacy setting ("my private playlist" vs public)
- Item count or other metadata mentioned in the task

**Why this matters**: APIs like search_playlists, search_notes return items from all users that match your query. Without filtering by owner or other metadata, you may act on the wrong item.

### Search Artists by Genre

```python
# Prefer genre= over query= for precise filtering
artists = apis.spotify.search_artists(access_token=token, genre="classical", page_limit=20)

# query= searches across multiple fields and may return non-matching results
# genre= filters precisely by the genre field
```

## Read Requester Messages for Filtering Criteria

**WHEN** the task says "as per their request", "based on what they asked", "as I mentioned to you", or similar:

1. **Find the requester's message**: Use `search_messages`, `show_messages`, or `show_emails` to locate the conversation with the person who made the request.
2. **Extract the specific criterion**: Read the message content to identify what they asked for (e.g., 'movies by Christopher Nolan', 'restaurants we discussed', 'songs from last week').
3. **Apply the criterion as a filter**: After gathering candidate items, filter them using the criterion from the message.

**Why this matters**: The requester's message contains the filtering criterion. Skipping this step and sending all matching items will fail tasks where only a subset is correct.

```python
# Example: Task says "Reply with movie recommendations as per Laura's request"
# Step 1: Find and read Laura's message
messages = apis.phone.show_messages(access_token=phone_token)
for msg in messages:
    if laura_phone in msg.get('phone_number', ''):
        # Extract criterion from message content
        # e.g., "Can you send me your Christopher Nolan movies?"
        requested_director = extract_name_from_message(msg['content'])

# Step 2: Gather movies from note, Step 3: Filter to requested director
```

## Finding People by Relationship Type

**WHEN** you need to find people with a specific relationship (roommates, family members, colleagues, etc.):

**Preferred: Use relationship parameter directly** - If the API supports filtering by relationship as a parameter, use it for efficiency. **CRITICAL: Always wrap search_contacts with fetch_all() pagination** - the relationship API returns paginated results:

```python
# Example: Find all roommates using direct parameter (preferred)
# CRITICAL: Use fetch_all() - search_contacts is paginated!
roommates = fetch_all(apis.phone.search_contacts, access_token=phone_token, relationship="roommate")

# Find all coworkers - pagination required
all_coworkers = fetch_all(apis.phone.search_contacts, access_token=phone_token, relationship="coworker")

# Extract emails for filtering
coworker_emails = {c['email'] for c in all_coworkers}
roommate_emails = {r['email'] for r in roommates}
target_emails = roommate_emails | coworker_emails
```

**Warning**: Calling `apis.phone.search_contacts(relationship=...)` without `fetch_all()` returns only the first page (typically 5-20 contacts). If there are more contacts with that relationship on subsequent pages, they will be missed. **Always use fetch_all()** for relationship-filtered contact searches.

**Alternative: Manual filtering** - If the API does not support relationship as a parameter, fetch all contacts and filter manually:

```python
contacts = fetch_all(apis.phone.search_contacts, access_token=phone_token)
roommates = [c for c in contacts if 'roommate' in c.get('relationships', [])]
```

**Common relationship types:** 'roommate', 'sibling', 'brother', 'sister', 'parent', 'spouse', 'colleague', 'coworker', 'friend'

**Note:** The relationships field is a list - check using `in` not equality. When using direct parameter, use singular form (e.g., "roommate" not "roommates").

**CRITICAL: The relationship API may have incomplete coverage.** The phone app's internal relationship graph may NOT match the task's ground truth. Even if you fetch all contacts with a relationship filter, some people who should match the relationship may be missing from the filtered list.

**MANDATORY TWO-PASS VERIFICATION PATTERN:** When filtering ANY items by relationship (whether senders of received requests, receivers of sent requests, or any other entity), you MUST use both passes. This is NOT optional - skipping the second pass has caused failures in multiple data instances.

### Pattern for filtering items by relationship

**Step 1 (First pass)**: Get ALL contacts with the target relationship using `fetch_all()` pagination, then filter your items by the known relationship emails.

**Step 2 (Second pass)**: For ANY items whose sender/receiver email was NOT in the first-pass relationship list, you MUST search for that person individually and check their relationships. Do NOT skip items just because they weren't in the first-pass filtered list.

```python
# Example: Send reminders to roommates for pending payment requests
# Step 1: Get all roommates with pagination
roommates = fetch_all(apis.phone.search_contacts, access_token=phone_token, relationship="roommate")
roommate_emails = {r['email'] for r in roommates}

# Filter pending requests to those sent to known roommates
roommate_requests = [req for req in pending_requests if req['receiver']['email'] in roommate_emails]

# Step 2: CRITICAL - verify each remaining request's receiver individually
# Some roommates may not appear in the relationship-filtered list due to incomplete coverage
unknown_receivers = [req for req in pending_requests if req['receiver']['email'] not in roommate_emails]
for req in unknown_receivers:
    receiver_email = req['receiver']['email']
    # Search for this person and check their relationships
    contacts = apis.phone.search_contacts(access_token=phone_token, query=receiver_email)
    for contact in contacts:
        if contact['email'] == receiver_email:
            if 'roommate' in contact.get('relationships', []):
                roommate_requests.append(req)  # This is a roommate after all!
                break
```

**Why two passes are mandatory**:
- The `search_contacts(relationship=...)` API returns contacts based on the app's internal relationship graph, which may NOT match the task's ground truth.
- In data_id 6ea6792_3, the first pass found only 13 of 18 required payment requests. The second pass recovered the missing 5 by checking each unknown sender individually.
- In data_id d0b1f43_2, calling `search_contacts(relationship="coworker")` without `fetch_all()` returned only 5 coworkers, missing additional coworkers on page 2, causing wrong answer ($744 vs $786).
- **Skipping the second pass guarantees incomplete results** when the relationship graph has gaps.

### Relationship Filtering for Payment Requests

**WHEN** creating payment requests for people who owe you money (from a note, receipt, or conversation) **AND** the task specifies a relationship filter (e.g., "coworkers", "friends", "roommates"):

1. **First, get ALL people with the required relationship** using the relationship filter script:
    ```python
    exec((SKILL_DIR / 'scripts/relationship_filter.py').read_text())
    coworkers = get_contacts_by_relationship(apis.phone.search_contacts, phone_token, 'coworker')
    coworker_emails = {c['email'] for c in coworkers}
    ```

2. **Then, filter your candidate list to only those with the relationship**:
    ```python
    # Extract names/amounts from your source (note, receipt, etc.)
    from_note = {'Stephen': 33, 'Sabrina': 32, 'Jamie': 31, 'Travis': 21, 'Kristin': 38, 'Anita': 32}
    
    # Look up email for each person
    def lookup_email(name):
        contacts = apis.phone.search_contacts(access_token=phone_token, query=name)
        for c in contacts:
            if c['first_name'] == name:
                return c['email']
        return None
    
    # Filter to only coworkers
    target_requests = {}
    for name, amount in from_note.items():
        email = lookup_email(name)
        if email and email in coworker_emails:
            target_requests[name] = amount
    
    # Now create payment requests only for target_requests
    ```

3. **Apply two-pass verification if relationship coverage is incomplete**: If the initial relationship query returns fewer people than expected, verify each unknown person individually:
    ```python
    # For each person not in coworker_emails, search individually
    unknown_people = [name for name in from_note if lookup_email(name) not in coworker_emails]
    for name in unknown_people:
        contacts = apis.phone.search_contacts(access_token=phone_token, query=name)
        for c in contacts:
            if c['email'] == lookup_email(name):
                if 'coworker' in c.get('relationships', []):
                    target_requests[name] = from_note[name]  # This person IS a coworker
                break
    ```

**Why this matters**: In data_id 22cc237_3, the agent created payment requests for 6 people from a note without checking coworker status. Only 4 were actual coworkers. Skipping relationship filtering caused 2 extra (incorrect) payment requests.

## Equal Expense Split Pattern

**WHEN** a task involves splitting a bill or expense equally among a group ("shared equally among my roommates and me", "split between us", etc.):

1. **Identify group size (N)** - count all people sharing the expense (including the requester)
2. **Calculate per-person share** - share = total_amount / N
3. **Request from (N-1) people** - the requester already paid, so create requests only for others
4. **Use exact description** - include the description specified in the task

```python
# Example: Electricity bill split among roommates and me
total_bill = 119  # From bill file
group_size = 4  # Jose + 3 roommates
share_per_person = total_bill / group_size  # $29.75

# Create requests for roommates only (not Jose who paid)
for roommate in roommates:  # 3 people
    apis.venmo.create_payment_request(
        access_token=venmo_token,
        user_email=roommate['email'],
        amount=share_per_person,
        note="For electricity bill."  # Exact description from task
    )
```

**Key rules:**
- Group size includes the requester ("my roommates and me" = 4 if 3 roommates)
- Only request from others, not the requester who already paid
- Use the exact description/note from the task instruction

## Identifying Accident/Refund Payments

**WHEN** the task says a payment was "an accident", "sent by mistake", or you need to "send money back" for a previous payment:

**CRITICAL: Do NOT assume "most recent approved" is the accident.** Both chain candidates (877161ece270 and 00208df3c689) picked the most recent approved request and got wrong amounts. The accident is identified by matching task context, not recency.

1. **Find the original transaction**: Search transaction history for payments involving the same people around the relevant time period.
2. **Identify the accident by task context**: Look for the transaction matching the task context:
   - Transaction/payment description matches task context (e.g., task mentions "Camera Gear" → look for that description)
   - Amount matches any mentioned values in the task or note
   - Timing is consistent with task description
3. **Cross-reference with transaction history**: After identifying the suspected accident payment request, verify by checking `show_transactions` for a corresponding transaction with matching amount and counterparty.
4. **Send the same amount back**: The refund amount should match the verified accident payment amount.

```python
# Example: "The last Venmo payment request I sent to Cory was an accident"
# Step 1: Get ALL sent payment requests (paginated)
all_sent = fetch_all(apis.venmo.show_sent_payment_requests, access_token=token, query="Cory")

# Step 2: Filter to approved requests
approved = [req for req in all_sent if req.get('approved_at')]

# Step 3: Identify by task context (NOT by max(approved_at))
# Match on description, amount, or timing from the task
accident_candidates = []
for req in approved:
    # Check if description matches task context
    # Check if amount matches any value mentioned in task
    # Check if timing is consistent
    accident_candidates.append(req)

# Step 4: Cross-reference with transaction history to verify
transactions = fetch_all(apis.venmo.show_transactions, access_token=token)
for candidate in accident_candidates:
    matching_tx = [t for t in transactions 
                   if t['amount'] == candidate['amount'] 
                   and t['receiver']['email'] == candidate['receiver']['email']]
    if matching_tx:
        refund_amount = candidate['amount']  # Verified amount
        break

# Step 5: Send refund for verified amount
```

**Critical Rules:**
- **Don't pick by recency** - the accident may not be the most recent approved request
- **Match on task context** - description, amount, timing from the task
- **Cross-reference with transactions** - verify the identified payment has a corresponding transaction before refunding

## Handling Existing Entity Conflicts (409 Errors)

**WHEN** you receive a 409 Conflict error stating an entity already exists (e.g., "SongReview with this song_id and user_id already exists"):

1. **Check the 'reviewed' (or equivalent) property** using the privates API to identify which items already have the entity:
   ```python
   privates = apis.spotify.show_song_privates(song_id=song_id, access_token=token)
   if privates['reviewed']:
       # Entity already exists - need to update
   else:
       # Entity doesn't exist - need to create
   ```

2. **For existing entities**, fetch the entity ID and use the update API:
   ```python
   reviews = apis.spotify.show_song_reviews(song_id=song_id, user_email=user_email)
   review_id = reviews[0]['review_id']  # Assuming one review per user
   apis.spotify.update_song_review(review_id=review_id, rating=1, access_token=token)
   ```

3. **For new entities**, use the create API:
   ```python
   apis.spotify.review_song(song_id=song_id, rating=1, access_token=token)
   ```

**Common 409 scenarios:**
- Reviews: Check `reviewed` property, then `update_song_review()` vs `review_song()`
- Follows: Check `following` property, then adjust accordingly
- Likes/downloads: Similar pattern with their respective privates APIs

**Do NOT** blindly retry the create operation - it will fail again. The 409 conflict indicates the entity exists and requires an update operation instead.

## Filtering Structured Note Content

**WHEN a note contains structured data with filterable properties** (e.g., movies with directors, expenses with categories, contacts with relationship types), parse the content and filter items based on the relevant property.

**Pattern:**

```python
# Note content has structured entries:
# Movie Title
#  - director: Christopher Nolan
#  - genre: Sci-Fi, Drama

# Step 1: Parse the structured content
import re

note_content = note['content']
movies = []
current_movie = None

for line in note_content.split('\n'):
    # Top-level entries (not indented) are movie titles
    if line and not line.startswith(' ') and not line.startswith('#'):
        current_movie = {'title': line.strip()}
        movies.append(current_movie)
    # Indented lines are properties of the current movie
    elif current_movie and line.startswith(' - '):
        key_value = line.strip()[3:]  # Remove ' - ' prefix
        if ':' in key_value:
            key, value = key_value.split(':', 1)
            current_movie[key.strip()] = value.strip()

# Step 2: Filter by director criterion
target_director = 'Christopher Nolan'  # Extract from task context or note structure
filtered_movies = [m for m in movies if m.get('director') == target_director]

# Step 3: Extract filtered titles
result_titles = [m['title'] for m in filtered_movies]
```

**Critical Rules:**
- Parse structure based on indentation/keywords, not just extract all text
- Filter AFTER parsing, based on the property that matches your task criterion
- Do NOT send all items from a note when the task implies a subset (e.g., "Christopher asked for recommendations" + movies have director field → filter by director name match)

## Accidental Payment Refund Pattern

**WHEN** the task says a payment was "an accident", "sent by mistake", or you need to "send money back" for a previous payment:

**CRITICAL: Do NOT assume "most recent approved" is the accident.** The accident payment is identified by matching task context (description, amount, timing), NOT by recency. Both chain candidates (877161ece270 and 00208df3c689) picked the most recent approved request and got wrong amounts ($61 and $13 vs expected $74 and $48).

1. **Paginate through ALL sent payment requests** using fetch_all()
2. **Filter to approved requests** (approved_at is not null) to the target person
3. **Match the accident by task context** - don't assume "most recent approved" is the accident. Look for:
   - Payment request description that matches task context (e.g., task mentions "Camera Gear" → look for that description)
   - Amount that matches any value mentioned in the task or note
   - Timing consistent with task description (e.g., "yesterday's payment" → filter by date)
4. **Verify by cross-referencing transaction history** - after identifying the suspected accident payment request, check `show_transactions` to confirm a corresponding transaction exists with matching amount and counterparty.
5. **Send money back** for the verified accident amount using create_transaction()

```python
# Step 1: Get all sent payment requests with pagination
all_sent_requests = fetch_all(apis.venmo.show_sent_payment_requests, access_token=token)

# Step 2: Filter to approved requests to the specific person
target_email = "person@example.com"
approved_to_target = [r for r in all_sent_requests 
                      if r['approved_at'] is not None 
                      and r['receiver']['email'] == target_email]

# Step 3: Identify the accident by matching task context, NOT by recency
# Extract context clues: description from task, amount from task, relevant dates
# Example: task says "Camera Gear accident" → filter by description match
# Example: task mentions "$74" → filter by amount
# DO NOT just pick max(approved_at) - the accident may not be the most recent
accident_candidates = []
for req in approved_to_target:
    # Check description match against task context
    # Check amount match against any mentioned values
    # Check timing consistency
    accident_candidates.append(req)

# Step 4: Cross-reference with transaction history to verify
all_txs = fetch_all(apis.venmo.show_transactions, access_token=token)
for candidate in accident_candidates:
    matching_tx = [t for t in all_txs 
                   if t['amount'] == candidate['amount'] 
                   and t['receiver']['email'] == target_email]
    if matching_tx:
        refund_amount = candidate['amount']  # Verified amount
        break

# Step 5: Send money back for the verified accident amount
apis.venmo.create_transaction(
    receiver_email=target_email,
    amount=refund_amount,
    access_token=token,
    description="Refund for accidental payment"
)
```

**Critical Rules:**
- **Always paginate** show_sent_payment_requests - default results may not include all requests
- **Filter by approved_at is not null** - unapproved requests don't count
- **Match task context, not recency** - the accident payment is identified by description/amount/timing from the task, not by being "most recent"
- **Cross-reference with transactions** - verify the identified payment has a corresponding transaction before refunding
- **Use exact amount** from the verified accident payment, not from a heuristic

## Frequency Analysis Pattern

**WHEN** you need to find the most/least common item by counting occurrences (e.g., "least recommended artist", "most common genre", "most active user"):

1. **Collect all items with pagination** from the relevant API
2. **Count occurrences** using `collections.Counter` or a dict
3. **Find the target** (min/max count, handling ties if needed)
4. **Return minimal answer** (entity name/number only)

```python
from collections import Counter

# Step 2: Count occurrences
# For items with multiple values (e.g., songs with multiple artists),
# iterate through each value
artist_counts = Counter()
for song in all_recommendations:
    for artist in song['artists']:  # Handle multiple artists per song
        artist_counts[artist['name']] += 1

# Step 3: Find minimum/maximum
min_count = min(artist_counts.values())
# Get ALL items with the min count (handles ties)
least_common = [item for item, count in artist_counts.items() if count == min_count]

# If you need exactly one: pick the first or use additional criteria
# answer = least_common[0]
```

**Handling ties**: When multiple items share the same min/max count, decide based on task requirements:
- Return all tied items: `answer=least_common` (as a list)
- Return one: Use secondary sort (alphabetical, earliest date, etc.)

**Common use cases:**
- "Which artist appears least/most in recommendations?"
- "What's the most common genre in my library?"
- "Which user sent the fewest payments?"

## Numeric Property Queries (Most/Highest/Oldest/Newest)

**WHEN** a task asks for items based on a numeric property ("most listened song", "highest rated album", "oldest note", "newest transaction"):

1. **Fetch ALL candidate items** with pagination if needed
2. **Get details for each item** to access the numeric property (play_count, rating, created_at, etc.)
3. **Use Python's max()/min() or sorted()** to find the target item
4. **Act on the result**

```python
# Example: "Play the most listened to song from the Velvet Underground album"

# Step 1: Find the album
albums = apis.spotify.search_albums(query="Velvet Underground", page_limit=20)
album = next(a for a in albums if a['title'] == "Velvet Underground")

# Step 2: Get album details to list all songs
album_details = apis.spotify.show_album(album_id=album['album_id'])
song_ids = [s['id'] for s in album_details['songs']]

# Step 3: Fetch details for each song to get play_count
songs_with_counts = []
for song_id in song_ids:
    song = apis.spotify.show_song(song_id=song_id)
    songs_with_counts.append((song['play_count'], song))

# Step 4: Find song with highest play_count
max_play_count, most_listened_song = max(songs_with_counts, key=lambda x: x[0])

# Step 5: Play the song
apis.spotify.play_music(access_token=token, song_id=most_listened_song['song_id'])
```

**For date-based queries** ("oldest", "newest", "most recent"): Compare `created_at` or `updated_at` fields parsed as datetime strings. Python string comparison works for ISO format dates, or parse with `datetime.fromisoformat()`.

```python
# Example: "Find the oldest note"
notes = fetch_all(apis.simple_note.show_notes, access_token=token)
oldest_note = min(notes, key=lambda n: n['created_at'])
```

## Notes

- Search is case-insensitive for most APIs
- Use specific keywords that appear in the target item
- **For payment/transaction matching: ALWAYS use the exact description string from your source**
- Search may return partial matches—verify results match your needs
- For paginated search results, iterate through all pages using the pagination pattern
- When searching for users before creating payments, always verify the exact email format from the search results

## API Enum Field Case-Sensitivity

**WHEN filtering by enum-like API fields** (genre, status, type, category, etc.):

**CRITICAL**: API enum values have specific casing that may differ from natural language. Filtering with incorrect casing returns 0 results.

**Pattern 1: Discovery first** - Use discovery APIs to get valid values:
```python
# Check available genres before filtering
genres = apis.spotify.show_genres()
print(f"Available genres: {genres}")  # ['EDM', 'R&B', 'indie', 'hip-hop', ...]

# Use the EXACT casing from the API
rnb_songs = [s for s in all_songs if s['genre'] == 'R&B']  # NOT 'r&b'
```

**Pattern 2: Case-insensitive comparison** - When discovery API unavailable:
```python
# Case-insensitive filter
rnb_songs = [s for s in all_songs if s['genre'].lower() == 'r&b']
```

**Common enum fields requiring exact casing**:
- `genre`: 'R&B' not 'r&b', 'EDM' not 'edm'
- `status`: 'pending', 'approved', 'declined' (check API docs)
- `type`: Varies by API - always verify

**Why this matters**: In data_id 50e1ac9_1, filtering with `'r&b'` returned 0 R&B songs. After checking `show_genres()` and using `'R&B'`, found 18 correct matches.

## Extracting Amounts from Phone/Text Conversations

**WHEN** the task says "as per my phone/text conversation" to determine a monetary amount (e.g., "Send the owed money as per my phone text conversation"):

1. **Paginate through ALL messages** — Use `fetch_all()` with `search_text_messages()` or `show_text_message_window()` to get the complete conversation history. Message APIs are paginated.
2. **Identify context-specific amounts** — Look for messages mentioning the specific transaction type from the task (e.g., "grocery", "dinner", "movie tickets"). Do NOT pick amounts from unrelated contexts.
3. **Distinguish multiple amounts** — Conversations may mention multiple payments. Filter by keywords matching the task:
   - "from the last time", "owed from before", "previous" — these refer to OLD debts, not the current task
   - "recently", "this time", task-specific keywords ("grocery", "dinner") — these refer to the CURRENT task amount
4. **Extract the correct amount** — Use the amount tied to the task's transaction type, not other mentioned amounts.

```python
# WRONG: Picking any amount mentioned in conversation
text_messages = apis.phone.search_text_messages(access_token=phone_token, phone_number=kristin_phone)
# Message says: "I owe you $25 from the last time" — this is a PREVIOUS debt!
amount = 25  # Wrong!

# CORRECT: Paginate and find the task-specific amount
all_messages = fetch_all(apis.phone.search_text_messages, access_token=phone_token, phone_number=kristin_phone)
# Filter for messages containing task keywords ("grocery", "groceries")
grocery_messages = [m for m in all_messages if 'grocery' in m['message'].lower()]
# Extract amount from the relevant message context
for msg in grocery_messages:
    # Parse amount from message mentioning grocery
    amount = extract_amount_from_message(msg['content'])
```

**Critical Rules:**
- **Always paginate message search APIs** — conversations may span multiple pages
- **Match task context** — amounts mentioned for different purposes ("from the last time" vs "for groceries") are different debts
- **Verify before acting** — if unsure, cross-reference with other sources (notes, transaction history)

## Finding Specific Messages by Content Keyword

**WHEN** a task references information "as per my text conversation" or "from my messages", do NOT assume the first dollar amount or item you see is the answer. Text conversations often contain multiple topics and references:

1. **Fetch ALL messages with pagination** using show_text_message_window() or the relevant message API.
2. **Search for keyword matches** in message content related to the task (e.g., 'grocery', 'dinner', 'owe', 'paid').
3. **Verify context** - ensure the matched message is about the task's subject, not a different transaction.
4. **Extract the value** only from the verified message.

```python
# Example: Find grocery bill amount from text conversation
all_messages = fetch_all(apis.phone.show_text_message_window, access_token=phone_token, phone_number=contact_phone)

# Search for messages mentioning groceries
grocery_messages = [m for m in all_messages if 'grocery' in m.get('message', '').lower()]

# If no direct match, look for payment/owe context
if not grocery_messages:
    import re
    for m in all_messages:
        if re.search(r'\$[\d.]+', m.get('message', '')):
            # Check context - is this about groceries or something else?
            msg_text = m.get('message', '').lower()
            if any(kw in msg_text for kw in ['grocery', 'groceries', 'food', 'shopping']):
                grocery_messages.append(m)
                break

# Extract amount from verified message
if grocery_messages:
    amount_match = re.search(r'\$([\d.]+)', grocery_messages[0]['message'])
    if amount_match:
        grocery_amount = float(amount_match.group(1))
```