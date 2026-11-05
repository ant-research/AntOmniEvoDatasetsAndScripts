# Editing an Item's Status Inside a Document (check-off / in-place text edit)

Use for tasks of the form 'mark <item> in my <note/file/document> as done', 'check off / uncheck <item>', or any task that changes one item's status marker or substring inside a stored document's text. Use when the app has NO per-item status/toggle API — the only mutation is updating the whole document's content (e.g. a note whose body is one content string). The target item and the status-marker convention both live inside the document's text, not in separate per-item records.

## 1. Find the document by title, then read its full content
Search the app's list API with the document name as the query (server-side filter), select the record whose title/name matches the named document, then fetch the FULL content with the show/read API. The target item and the marker convention are visible only in the full content, not in the list row.

## 2. Infer the done/not-done markers from the document's own text
Never assume a marker format. Read it verbatim from the document: the title may state the convention (e.g. '([x] = done, [ ] = not done)'), already-done sibling lines show the 'done' form, and the target line plus other open items show the 'not done' form. Markers vary — `[x]`/`[ ]`, `☑`/`☐`, `DONE:`/`TODO:`, a leading token, or a strikethrough — so take them from the source, never from memory.

## 3. Flip exactly the target line, preserve every other line
Build the new content by replacing only the target item's marker (its full line) with the done form. Keep the header line and every other line byte-identical — do not rebuild the document, re-order lines, or normalize whitespace, because the evaluator compares the header line and body lines against ground truth.

```python
new_content = content.replace(not_done_marker + ' ' + target_item, done_marker + ' ' + target_item)
assert new_content != content, 'target line not found - re-read the full content'
```

## 4. Update only the content field and verify
Call the update/rewrite API with the document id and `content=new_content`, omitting every other field (title/tags/pinned/...) per SKILL.md 'Field-scoped updates'. Re-read the document and assert: the target item now carries the done marker, its old not-done form is gone, the header line is unchanged, every other line is unchanged, and no record was added or removed.

## 5. Finish
Call `apis.supervisor.complete_task()` with no `answer` (state-change task).