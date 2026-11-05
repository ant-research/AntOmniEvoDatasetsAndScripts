# Time-of-Day Shift Arithmetic

Use for tasks of the form 'move/reschedule X to N minutes/hours later or earlier' and any time a time-of-day stored as `HH:MM` (or `HH:MM:SS`) must be shifted or compared — alarms, scheduled drafts, calendar events.

## 1. Read the source time
Take the source time verbatim from the record's time field. Never rebuild it from memory or mutate the digit string directly.

## 2. Shift with datetime arithmetic
Parse the source, add/subtract the task's delta, and format back. `timedelta` wraps across midnight automatically.

```python
from datetime import datetime, timedelta
t = datetime.strptime(src_time, '%H:%M')     # '21:30'
delta = timedelta(minutes=20)                # +20 for 'later', -20 for 'earlier'
new_time = (t + delta).strftime('%H:%M')     # '21:50'; '23:50'+20 -> '00:10'
```

Use `'%H:%M:%S'` for timestamp sources.

## 3. Confirm direction and magnitude
Map the wording before computing: 'later'/'after' = +N, 'earlier'/'before' = -N. Print source, delta, and result together so a sign error or missed rollover is visible.

## 4. Apply only the shifted time
Pass only the shifted time to the update API (leave every other field unset), then re-read the record and assert the stored time equals `new_time` before completing.