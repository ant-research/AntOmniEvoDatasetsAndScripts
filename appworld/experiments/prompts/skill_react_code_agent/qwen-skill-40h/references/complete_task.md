# Task Completion Pattern

## When to Use

Always call `apis.supervisor.complete_task()` as the final step of every task.

## Why It Matters

The evaluator checks that complete_task() was called to mark the task as done. Skipping this leaves the task incomplete regardless of other actions taken.

## Pattern

### When an Answer is Needed

For query tasks where the task asks a question ("How many...", "What is...", "List all..."):

```python
apis.supervisor.complete_task(answer=<value>)
```

The answer must be **minimal**—just the entity name, number, or list. NOT full sentences.

**Examples:**

```python
# Counting task
answer=5  # NOT "There are 5 songs in the playlist"

# Entity task  
answer="Taylor Swift"  # NOT "The artist is Taylor Swift"

# List task
answer=["song1", "song2", "song3"]  # NOT "The playlist contains song1, song2, and song3"
```

### When No Answer is Needed

For action-only tasks (making payments, sending messages, updating records, setting up something):

```python
apis.supervisor.complete_task()
# or
apis.supervisor.complete_task(answer=None)
```

**Examples of action-only tasks:**

- "Send a payment request to X for $Y"
- "Create a new playlist with these songs"
- "Update my profile picture"
- "Make payment requests for others with description Work Dinner"

## Important Rules

1. **Never skip complete_task()** — The evaluator will mark the task as incomplete
2. **Never include explanatory text** in the answer
3. **Call complete_task() IMMEDIATELY after verifying the answer** — Once you have computed and verified the answer, call complete_task() as the next step. Do NOT make additional API calls, file operations, or exploration after verification—these modify system state and cause 'no model changes' failures.
4. **Verify before completing** — Check that actions succeeded (e.g., `show_artist_following()` returns `{'following': True}`)

## Verification Before Completion

Before calling complete_task(), verify the final state:

```python
# Example: verify follow operation succeeded
following = apis.spotify.show_artist_following(artist_id=artist_id, access_token=token)
assert following['following'] == True

# Now complete the task IMMEDIATELY
apis.supervisor.complete_task()
# Do NOT make any more API calls after this point
```

## Critical: Stop After Answer Computation

For query tasks (questions that require computing an answer):

```python
# WRONG: Continue exploring after computing answer
total = sum(bill_amounts)
print(f"Total: ${total}")
# Don't do this: searching for more files, creating directories, etc.
more_files = apis.file_system.show_directory(...)  # BAD - unnecessary exploration
decompress_result = apis.file_system.decompress_file(...)  # BAD - modifies state

# CORRECT: Verify and complete immediately
total = sum(bill_amounts)
print(f"Total: ${total}")  # Verification
apis.supervisor.complete_task(answer=total)  # COMPLETE NOW
```

**Why this matters**: Even "read-only" exploration can modify system state (e.g., creating temporary directories, failed API calls that leave traces). The evaluator's 'no model changes' test will fail if you make any modifications after the answer is ready.