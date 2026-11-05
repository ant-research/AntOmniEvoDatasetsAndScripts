# File System App Patterns

When a task mentions "file system", "~/", or directory/file paths, use the `apis.file_system` app — not Python os/pathlib modules (which are disabled in the sandbox).

## Workflow

1. Login using the `login_to_app` helper (loaded in step 2 of the procedure):
   ```python
   fs_token, profile = login_to_app('file_system')
   ```
2. Call `show_directory(access_token=token, directory_path='~/...')` to discover files. Pass `recursive=True` when you need all nested files, not just the top level.
3. Filter the returned path list programmatically to select target files — never hardcode paths after viewing a listing.
4. Call `show_file(access_token=token, file_path=...)` to read each file's content (returns dict with `content` key).
5. Parse file content using string methods or regex to extract the needed data.

## Parsing File Content

When a task requires a value from a file (e.g., a bill amount, a date, a count),
ALWAYS extract it programmatically from the file content — never hardcode a
value observed in printed output.

```python
# WRONG — hardcoding after reading the receipt output
total_amount = 128

# CORRECT — parse the amount from the file content
import re
content = apis.file_system.show_file(access_token=token, file_path=receipt_path)['content']
match = re.search(r'Total Amount.*?\$([\d.]+)', content)
assert match, 'Total Amount not found in receipt'
total_amount = float(match.group(1))
```

Common patterns for bills and receipts:
- `Total Amount => $128` → `re.search(r'Total Amount.*?\$([\d.]+)', content)`
- `Base Charges => $108` → `re.search(r'Base Charges.*?\$([\d.]+)', content)`
- Any `KEY => VALUE` format → `re.search(r'KEY.*?=>\s*(.+)', content)`

## Programmatic File Selection

Always filter `show_directory` results in code. Hardcoded file lists break when directory contents vary across tasks:

### Filtering files by attribute

```python
# WRONG — hardcoding after viewing directory output
target_files = ['/home/user/bills/cable/2023-01.txt', '/home/user/bills/cable/2023-04.txt']

# CORRECT — programmatically filter the directory listing
all_paths = apis.file_system.show_directory(access_token=token, directory_path='~/bills/', recursive=True)
current_year = datetime.now().year
target_files = [p for p in all_paths if f'/{current_year}-' in p and p.endswith('.txt')]
```

Use the current year from `datetime.now()` (or `apis.phone.get_current_date_and_time()`) to resolve relative references like "this year" or "last month" when filtering.

### Extracting subdirectory names

When a task requires iterating over subdirectories of a parent directory (e.g., compress or delete each subdirectory), extract subdirectory names programmatically from `show_directory` output — never hardcode a list of names after viewing the listing:

```python
# WRONG — hardcoding names seen in printed output
vacation_spots = ['athens', 'maui', 'stockholm', 'tokyo']

# CORRECT — extract subdirectory names from the directory listing
parent_path = '~/photographs/vacations/'
entries = apis.file_system.show_directory(access_token=fs_token, directory_path=parent_path)
subdirs = []
for entry in entries:
    # Subdirectory paths look like /home/user/parent/child/
    # Strip trailing slash, get basename; only include paths ending with '/'
    if entry.endswith('/'):
        name = entry.rstrip('/').split('/')[-1]
        subdirs.append(name)
```

## Non-Paginated APIs

File system APIs (`show_directory`, `show_file`, `file_exists`, etc.) do **NOT** accept `page_limit`/`page_index` — they are not paginated. Never wrap them in `get_all_pages()`, which adds these parameters automatically and causes a 422 error. Call them directly.

## Key APIs

Check `apis.api_docs.show_api_descriptions(app_name='file_system')` for the complete list. Commonly used:

| API | Purpose |
|-----|--------|
| `login` | Authenticate; returns access_token |
| `show_directory` | List files/subdirs; supports `recursive=True`; NOT paginated |
| `show_file` | Read file content; returns `{'content': '...'}`; NOT paginated |
| `create_file` | Create file with content; pass `overwrite=True` to replace existing file without error |
| `create_directory` | Create directory; pass `allow_if_exists=True` to accept existing directory without error; pass `recursive=True` to create parent directories |
| `delete_file` | Delete a file |
| `delete_directory` | Delete a directory and contents |
| `file_exists` | Check if a file exists |
| `directory_exists` | Check if a directory exists |
| `update_file` | Update file content |
| `move_file` / `copy_file` | Move or copy a file |
| `move_directory` / `copy_directory` | Move or copy a directory |
| `compress_directory` / `decompress_file` | Archive operations |

## When to Use

- Task mentions "file system", "~/path", "directory", or "files"
- Task says data is "in" a directory or file
- Task references bill files, documents, or saved data on disk
- Task says to export, save, or backup data from an app to files

## Cross-App Data Export Pattern

When a task requires exporting data from another app to the file system:

1. Login to both the source app and the file_system app.
2. Fetch all data from the source app using `get_all_pages` on its list/search API.
3. If the list API lacks fields needed for file content (e.g., note body, transaction details),
   call the source app's detail API for each item to retrieve the full record.
4. Create the target directory with `create_directory(directory_path=..., recursive=True, allow_if_exists=True)`.
5. For each item, construct the file path and name programmatically — never hardcode.
   When a task specifies a naming rule (e.g., replace whitespace with `_`, use a specific extension),
   apply `re.sub(r'\s+', '_', title)` for whitespace replacement (handles spaces, tabs, newlines)
   rather than `str.replace(' ', '_')` which only handles spaces.
6. Write each file with `create_file(file_path=..., content=..., overwrite=True)`.
7. Verify by listing the target directory and checking the file count matches the item count.