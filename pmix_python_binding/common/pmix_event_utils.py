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


class JobCompletionRouter:
    """Buffer early job-end events and deliver each namespace exactly once.

    The supplied condition also protects the controller's running-job and slot
    bookkeeping. Never hold it across a blocking PMIx spawn call.
    """

    def __init__(self, condition, on_completion):
        self.condition = condition
        self.on_completion = on_completion
        self.registered = set()
        self.pending = {}
        self.delivered = set()

    def record(self, source, info):
        from copy import deepcopy
        namespace = (source or {}).get('nspace')
        if not namespace:
            return
        with self.condition:
            if namespace in self.delivered or namespace in self.pending:
                return
            self.pending[namespace] = deepcopy((source, info))
            self._deliver(namespace)

    def register(self, namespace):
        with self.condition:
            self.registered.add(namespace)
            self._deliver(namespace)

    def _deliver(self, namespace):
        if namespace not in self.registered or namespace not in self.pending:
            return
        source, info = self.pending.pop(namespace)
        self.delivered.add(namespace)
        self.on_completion(source, info)


def wait_for_job_completions(condition, completed_count, expected, timeout):
    """Handle prior notifications and spurious wakeups with a bounded wait."""
    with condition:
        return condition.wait_for(lambda: completed_count() >= expected,
                                  timeout=timeout)
