# Summing a Labeled Numeric Field Across Directory Files (read-and-sum)

Use for question tasks of the form 'what is the total <field> of my <records> for <period>?' where the records are files in a directory and each file carries the field on a labeled line (e.g. a bill file with a `Total Amount => $NN` line). The final answer is one bare number. The question names the source directory — all answer data lives there, so once it is listed do not browse other apps (email, notes) for a record that does not exist.

## 1. Anchor the period to the current date
Get the current date/time from `datetime.now()` or the phone/clock API, never from memory. Resolve the task's period against that date first: 'this year' = the current calendar year, 'this month' = the current month, 'last month' = the previous calendar month. Summing by name without anchoring mixes records from other periods into the total; a record for the current unfinished month is not yet issued and is out of scope.

## 2. Enumerate the directory and select files by type and period
List the directory (recursively if needed). Keep only files whose path names BOTH the requested record type/folder and a year-month inside the anchored period; ignore files in sibling folders of other types and files of other years. Compare the full year-month — a same-month file from a different year is out of scope.

## 3. Skip binary placeholders; read only readable text
Read each selected file's content with the file-read API. A content value that starts with `binary:` and ends with `...` is an opaque placeholder stub, not text — skip it (see SKILL.md 'Binary files' rule); do not hex/base64-decode it, do not guess its value, and never copy/decompress it (that persists model changes and yields nothing). Sum only files whose content actually carries the target field line.

## 4. Extract the aggregate field line, not a component line
Parse each readable content line by line and take the line whose label is the requested aggregate (e.g. `Total Amount`), not a charge/fee/tax/subtotal line from the same file.

## 5. Sum numerically and show the per-file breakdown
Strip any leading currency symbol for arithmetic only, convert each value with `float()`, and sum (`total = round(sum(amounts), 2)`). Print each extracted value next to its file name and the total so a dropped or double-counted file is visible.

## 6. Answer bare, then stop
Call `apis.supervisor.complete_task(answer=<bare number>)` — digits only, no currency symbol, separator, or unit (SKILL.md Output Format). Do not re-read already-summed files or chase a missing record in other apps; the total is complete as soon as step 5 printed it.