def resolve_relationships(relationship_terms, phone_token):
    """Resolve relationship terms to contact identifiers using the phone app.

    Use this whenever a task mentions people by relationship (friend, coworker,
    family, roommate, manager, etc.). NEVER use the target app's own friend/
    social API (e.g., venmo.search_friends) — those return platform connections
    unrelated to the contact-relationship meaning. The phone app's contact list
    is the authoritative social graph.

    Usage:
        exec((SKILL_DIR / 'scripts/helpers.py').read_text())
        phone_token, profile = login_to_app('phone')
        contact_ids, contacts = resolve_relationships(['friend'], phone_token)
        # Then filter target app records by contact_ids (set of emails)

    Args:
        relationship_terms: List of relationship terms mentioned in the task
            (e.g., ['friend'], ['coworker', 'manager']).
        phone_token: Access token for the phone app.

    Returns:
        tuple: (contact_ids, contacts) where:
            - contact_ids is a set of email strings for filtering in other apps
            - contacts is a list of all matching contact records
    """
    all_contacts = []
    for term in relationship_terms:
        page_index = 0
        while True:
            page = apis.phone.search_contacts(
                access_token=phone_token,
                relationship=term,
                page_limit=20,
                page_index=page_index
            )
            all_contacts.extend(page)
            if len(page) < 20:
                break
            page_index += 1
    contact_ids = {c['email'] for c in all_contacts}
    return contact_ids, all_contacts


def login_to_app(app_name, username_field=None):
    """Login to an app using supervisor credentials and profile.

    Usage:
        exec((SKILL_DIR / 'scripts/helpers.py').read_text())
        venmo_token, profile = login_to_app('venmo')
        phone_token, profile = login_to_app('phone')

    Args:
        app_name: Name of the app to login to (e.g., 'venmo', 'phone').
        username_field: Profile field to use as username. Defaults to
                        'phone_number' for the phone app, 'email' for all others.

    Returns:
        tuple: (access_token, profile_dict) where profile_dict contains
               first_name, last_name, email, phone_number, etc. for use
               in subsequent logic — never hardcode these values.
    """
    if username_field is None:
        username_field = 'phone_number' if app_name == 'phone' else 'email'
    passwords = apis.supervisor.show_account_passwords()
    profile = apis.supervisor.show_profile()
    app_passwords = [p['password'] for p in passwords if p['account_name'] == app_name]
    if not app_passwords:
        raise ValueError(
            f"No password found for app '{app_name}'. "
            f"Available apps: {[p['account_name'] for p in passwords]}"
        )
    app_password = app_passwords[0]
    username = profile[username_field]
    login_api = getattr(apis, app_name).login
    result = login_api(username=username, password=app_password)
    return result['access_token'], profile