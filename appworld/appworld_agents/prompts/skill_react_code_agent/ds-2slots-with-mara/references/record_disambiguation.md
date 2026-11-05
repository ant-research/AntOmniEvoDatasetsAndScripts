# Selecting the Correct Source Record

Use when the task references a record (a note, email, order, or message) but list/search returns several candidates that share the same topic or a similar title.

## 1. List every candidate
Fetch the full candidate list with pagination and print each candidate's id, title, created_at, and content (or subject/body).

## 2. Extract the task's selection evidence
Pin down the concrete values that identify the right record: the people or recipients involved, the amounts, the item counts, and any relative-date anchor (yesterday, last week, today). Compute relative dates from `datetime.now()` or the phone/clock API, never from memory.

## 3. Pull corroborating evidence from the linked app
Query the second app the task references (e.g. the payment app for 'already sent their share', the email app for 'already replied') restricted to the relevant date window, and collect its concrete values (amounts, emails, names, dates).

## 4. Match, do not guess
Select the candidate whose values actually appear in the corroborating data (exact value matches and/or the date anchor), not the candidate nearest in title or recency — unless the task itself specifies a temporal selector ('the last', 'most recent', 'newest'), in which case sort the candidates by their date/created field and apply the selector explicitly. A name can appear in several candidates — treat each occurrence as distinct and keep only the candidate whose surrounding values match the evidence. If no candidate uniquely matches, stop and re-check retrieval completeness before acting.

## 5. Act only from the chosen record
Use the chosen record's entries and values verbatim, and ignore the other candidates entirely. Do not merge or average values across candidates.