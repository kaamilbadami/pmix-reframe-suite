import os
import sys
import threading
import time

import pmix


SPAWN_COMPLETION_TIMEOUT_SECONDS = 60
PROOF_VISIBILITY_TIMEOUT_SECONDS = 30
PROOF_POLL_INTERVAL_SECONDS = 0.1


class SpawnCompletionTracker:
    """Track completion of one exact spawned namespace."""

    def __init__(self):
        self._completion = threading.Event()
        self._lock = threading.Lock()
        self._events = {}
        self.namespace = None
        self.event = None

    def record(self, status, source, info=None, results=None):
        """Retain a completion event, including one received early."""
        source_copy = dict(source or {})
        source_namespace = source_copy.get("nspace")

        if source_namespace is None:
            return

        event = {
            "status": status,
            "source": source_copy,
            "info": list(info or []),
            "results": list(results or [])
        }

        with self._lock:
            self._events[source_namespace] = event

            if source_namespace == self.namespace:
                self.event = event
                self._completion.set()

    def set_namespace(self, namespace):
        """Select the exact namespace and apply any retained event."""
        with self._lock:
            self.namespace = namespace
            self.event = self._events.get(namespace)

            if self.event is not None:
                self._completion.set()

    def wait(self, timeout_seconds):
        """Wait for completion of the selected namespace."""
        return self._completion.wait(timeout_seconds)


def get_pmix_info_value(info, key):
    """Return an event-info value while normalizing PMIx byte keys."""
    if isinstance(key, bytes):
        key = key.decode("ascii")

    return next(
        (item.get("value") for item in (info or [])
         if item.get("key") == key),
        None
    )


def require_successful_job_termination(event):
    """Fail closed unless JOB_END reports successful job termination."""
    termination_status = get_pmix_info_value(
        event.get("info"),
        pmix.PMIX_JOB_TERM_STATUS
    )

    if termination_status is None:
        reason = "spawned namespace completion status was missing"
    elif termination_status != pmix.PMIX_SUCCESS:
        reason = (
            "spawned namespace terminated unsuccessfully: "
            f"PMIX_JOB_TERM_STATUS={termination_status}"
        )
    else:
        return termination_status

    print(
        "spawn completion failure:",
        f"reason={reason}",
        f"event={event}",
        flush=True
    )
    raise SystemExit(reason)


def wait_for_spawn_completion_and_proofs(
    tracker, proof_files,
    completion_timeout_seconds=SPAWN_COMPLETION_TIMEOUT_SECONDS,
    visibility_timeout_seconds=PROOF_VISIBILITY_TIMEOUT_SECONDS
):
    """Require successful exact-job completion before judging proof files."""
    if not tracker.wait(completion_timeout_seconds):
        raise SystemExit(
            "spawned namespace completion was not observed: "
            f"namespace={tracker.namespace}"
        )
    print("spawn completion observed:", tracker.namespace, flush=True)
    require_successful_job_termination(tracker.event)
    deadline = time.monotonic() + visibility_timeout_seconds
    while True:
        missing = [name for name in proof_files if not os.path.exists(name)]
        if not missing:
            print("process proofs complete:", len(proof_files), flush=True)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit(
                "proofs remained incomplete after completion: "
                f"namespace={tracker.namespace} missing={missing}"
            )
        time.sleep(min(PROOF_POLL_INTERVAL_SECONDS, remaining))


def main():
    # Read the process count passed by the shell script.
    if len(sys.argv) != 2:
        raise SystemExit("usage: spawn_scaling_test.py NUMBER_OF_PROCESSES")

    try:
        num_processes = int(sys.argv[1])
    except ValueError:
        raise SystemExit("process count must be an integer")

    if num_processes < 1:
        raise SystemExit("process count must be at least 1")


    # Create one proof-file name for each requested process.
    proof_files = [
        f"process_{number}_worked"
        for number in range(1, num_processes + 1)
    ]


    # Read the address of the running PRRTE DVM.
    with open("dvm.uri") as file:
        dvm_uri = file.readline().strip()


    # Create the PMIx tool used to connect, spawn processes, and finalize.
    tool = pmix.PMIxTool()


    # Connect the PMIx tool to PRRTE.
    init_result = tool.init([
        {
            "key": "pmix.srvr.uri",
            "value": dvm_uri,
            "val_type": pmix.PMIX_STRING
        }
    ])

    print("init:", init_result)

    if init_result[0] != 0:
        raise SystemExit("init failed")


    # Create one PMIx app entry for each requested process.
    apps = [
        {
            "cmd": "/usr/bin/touch",
            "argv": ["touch", proof_file],
            "maxprocs": 1
        }
        for proof_file in proof_files
    ]



    tracker = SpawnCompletionTracker()

    def completion_handler(event_handler, status, source, info, results):
        tracker.record(status, source, info, results)
        return pmix.PMIX_EVENT_ACTION_COMPLETE, None

    try:
        handler_result = tool.register_event_handler(
            [pmix.PMIX_EVENT_JOB_END], [], completion_handler
        )
        print("completion handler:", handler_result, flush=True)
        if handler_result[0] != pmix.PMIX_SUCCESS:
            raise SystemExit("spawn completion handler registration failed")
        spawn_result = tool.spawn([{
            "key": pmix.PMIX_NOTIFY_COMPLETION,
            "value": True,
            "val_type": pmix.PMIX_BOOL
        }], apps)
        print("spawn:", spawn_result, flush=True)
        if spawn_result[0] != pmix.PMIX_SUCCESS:
            raise SystemExit("spawn failed")
        if not spawn_result[1]:
            raise SystemExit("spawn returned no namespace")
        tracker.set_namespace(spawn_result[1])
        wait_for_spawn_completion_and_proofs(tracker, proof_files)
        for process_number, proof_file in enumerate(proof_files, start=1):
            print(f"process {process_number} proof:", os.path.exists(proof_file))
    finally:
        finalize_result = tool.finalize()
        print("finalize:", finalize_result, flush=True)

    if finalize_result != pmix.PMIX_SUCCESS:
        raise SystemExit("finalize failed")


if __name__ == "__main__":
    main()
