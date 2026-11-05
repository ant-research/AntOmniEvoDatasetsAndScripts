# Credential Lookup Pattern

## When to Use

Always retrieve credentials BEFORE attempting to log into any app.

## Why It Matters

App passwords are not predictable—they are retrieved from the supervisor API. Guessing or hardcoding passwords causes 401 authentication failures and wastes steps.

## Pattern

```python
# Step 1: Get all credentials from supervisor
credentials = apis.supervisor.show_account_passwords()
# Returns a LIST of dicts: [{'account_name': 'spotify', 'password': '...'}, ...]

# Step 2: Extract the password for the specific app by iterating
creds = {c['account_name']: c['password'] for c in credentials}
# OR use a loop:
# spotify_password = None
# for c in credentials:
#     if c['account_name'] == 'spotify':
#         spotify_password = c['password']
#         break

# Step 3: Login with the correct credentials
result = apis.<app>.login(username=<username>, password=creds['<app>'])
access_token = result['access_token']
```

## Example

```python
# Get credentials (returns list)
credentials = apis.supervisor.show_account_passwords()
# Convert to dict for easy access
creds = {c['account_name']: c['password'] for c in credentials}

# Login to Spotify
spotify_token = apis.spotify.login(
    username=profile['email'],
    password=creds['spotify']
)['access_token']

# Login to Venmo
venmo_token = apis.venmo.login(
    username=profile['email'],
    password=creds['venmo']
)['access_token']
```

## Important Rules

1. **Never guess passwords** — The password is never a literal string like "appname_password" or "password123"
2. **Always call show_account_passwords() first** — Before any login attempt
3. **Store access tokens** — After login, save the returned access token for subsequent API calls
4. **Handle authentication errors** — If you get 401, re-check that you're using the correct credentials from the supervisor

## Username Format by App Type

Different apps require different username formats for login:

- **Email-based apps** (venmo, spotify, gmail, amazon, file_system, simple_note, splitwise, todoist): Use the supervisor's email from `show_profile()['email']` as username.
- **Phone app**: Uses the supervisor's phone number from `show_profile()['phone_number']` as username (digits only, no formatting).

```python
profile = apis.supervisor.show_profile()

# For email-based apps
venmo_token = apis.venmo.login(username=profile['email'], password=credentials['venmo'])

# For phone app - use phone number as username
phone_token = apis.phone.login(username=profile['phone_number'], password=credentials['phone'])
```

## User Profile

For tasks requiring user profile information (beyond login credentials):

```python
profile = apis.supervisor.show_profile()
# Use profile fields: first_name, last_name, email, phone_number, birthday, sex
```

## Current Date/Time Format

When you need the current date for time-based filtering (e.g., "this year", "yesterday", "last month"):

```python
current_datetime = apis.phone.get_current_date_and_time()
# Returns: {'date': 'Thursday, May 18, 2023', 'time': '12:00 PM'}

# Extract year from the date string:
current_year = int(current_datetime['date'].split(', ')[-1])  # 2023

# For month/day comparisons, parse the full date:
from datetime import datetime
current_date = datetime.strptime(current_datetime['date'], '%A, %B %d, %Y')
```