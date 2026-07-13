def get_pmix_info_value(info, key):
    """Return an event-info value while normalizing PMIx byte keys."""
    if isinstance(key, bytes):
        key = key.decode('ascii')

    return next(
        (item.get('value') for item in (info or [])
         if item.get('key') == key),
        None
    )
