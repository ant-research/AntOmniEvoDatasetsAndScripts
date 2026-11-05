# Relationship filter helper for AppWorld contact searches.
#
# Usage: exec((SKILL_DIR / 'scripts/relationship_filter.py').read_text())
#        then call get_contacts_by_relationship(api, token, relationship)
#
# This script ensures ALL contacts with a given relationship are retrieved
# using fetch_all() pagination. Direct search_contacts calls without
# pagination miss contacts on page 2+.


def get_contacts_by_relationship(api_func, access_token, relationship):
    """
    Fetch ALL contacts with a specific relationship using pagination.
    
    Args:
        api_func: The search_contacts API function
        access_token: Phone API access token
        relationship: Relationship type (e.g., 'coworker', 'friend', 'roommate')
    
    Returns:
        List of ALL contacts with the given relationship across all pages
    
    Example:
        # First, exec this script:
        exec((SKILL_DIR / 'scripts/relationship_filter.py').read_text())
        
        # Then use the helper:
        all_coworkers = get_contacts_by_relationship(
            apis.phone.search_contacts,
            phone_token,
            'coworker'
        )
        coworker_emails = {c['email'] for c in all_coworkers}
    """
    # Import fetch_all from pagination script if not available
    # The pagination script should be exec'd first per SKILL.md Procedure step 3
    # If fetch_all is not defined, define it inline
    if 'fetch_all' not in globals():
        def fetch_all(api_func, **kwargs):
            all_results = []
            page_index = 0
            while True:
                page_kwargs = kwargs.copy()
                page_kwargs['page_index'] = page_index
                try:
                    page_results = api_func(**page_kwargs)
                except Exception:
                    if page_index == 0:
                        return list(api_func(**kwargs))
                    break
                if not page_results or len(page_results) == 0:
                    break
                all_results.extend(page_results)
                page_index += 1
            return all_results
    
    return fetch_all(api_func, access_token=access_token, relationship=relationship)