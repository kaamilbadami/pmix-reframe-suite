def get_pmix_info_value(info, key):
    """Return an event-info value while normalizing PMIx byte keys."""
    if isinstance(key, bytes):
        key = key.decode('ascii')

    return next(
        (item.get('value') for item in (info or [])
         if item.get('key') == key),
        None
    )


def format_pmix_job_term_status(term_status):
    """Describe an application job termination status without PMIx decoding."""
    return (
        'PMIX_JOB_TERM_STATUS={} (application termination status)'
        .format(term_status)
    )
