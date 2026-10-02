import glob
import os
import shlex
import sys
import threading
import time
from collections import Counter

import pmix


SPAWN_COMPLETION_TIMEOUT_SECONDS = 60
PROOF_VISIBILITY_TIMEOUT_SECONDS = 30
PROOF_POLL_INTERVAL_SECONDS = 0.1
PROOF_PROGRESS_INTERVAL_SECONDS = 1


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


def read_proof_snapshot(pattern):
    """Return visible proof files, their hosts, and any read errors."""
    proof_files = sorted(glob.glob(pattern))
    file_hosts = []
    read_errors = []

    for proof_file in proof_files:
        try:
            with open(proof_file) as file:
                hostname = file.readline().strip()
        except OSError as error:
            read_errors.append((proof_file, str(error)))
            continue

        file_hosts.append((proof_file, hostname))

    return proof_files, file_hosts, read_errors


def format_host_counts(expected_hosts, file_hosts):
    """Format expected and unexpected observed host counts."""
    host_counts = Counter(hostname for _, hostname in file_hosts)
    ordered_hosts = list(expected_hosts)
    ordered_hosts.extend(sorted(set(host_counts) - set(expected_hosts)))

    if not ordered_hosts:
        return "(none)"

    return ",".join(
        f"{hostname or '<empty>'}={host_counts[hostname]}"
        for hostname in ordered_hosts
    )


def wait_for_spawn_completion_and_proofs(
    completion_tracker,
    pattern,
    expected_count,
    expected_hosts,
    completion_timeout_seconds=SPAWN_COMPLETION_TIMEOUT_SECONDS,
    visibility_timeout_seconds=PROOF_VISIBILITY_TIMEOUT_SECONDS
):
    """Wait for exact job completion and all shared-storage proofs."""
    wait_started = time.monotonic()
    completion_deadline = wait_started + completion_timeout_seconds
    visibility_deadline = None
    next_progress = wait_started

    while True:
        proof_files, file_hosts, read_errors = read_proof_snapshot(pattern)
        now = time.monotonic()
        elapsed_seconds = now - wait_started
        completion_observed = completion_tracker.wait(0)

        if completion_observed and visibility_deadline is None:
            visibility_deadline = now + visibility_timeout_seconds
            print(
                "spawn completion observed:",
                f"namespace={completion_tracker.namespace}",
                f"event={completion_tracker.event}",
                f"elapsed={elapsed_seconds:.1f}s",
                flush=True
            )
            require_successful_job_termination(completion_tracker.event)

        if (
            completion_observed
            and len(proof_files) == expected_count
            and not read_errors
        ):
            print(
                "hostname proofs complete:",
                f"observed={len(proof_files)}/{expected_count}",
                f"elapsed={elapsed_seconds:.1f}s",
                f"counts={format_host_counts(expected_hosts, file_hosts)}",
                flush=True
            )
            return proof_files

        if now >= next_progress:
            print(
                "waiting for spawn completion and hostname proofs:",
                f"expected={expected_count}",
                f"observed={len(proof_files)}",
                f"elapsed={elapsed_seconds:.1f}s",
                f"completion_observed={completion_observed}",
                f"counts={format_host_counts(expected_hosts, file_hosts)}",
                flush=True
            )
            next_progress = now + PROOF_PROGRESS_INTERVAL_SECONDS

        timeout_reason = None

        if not completion_observed and now >= completion_deadline:
            timeout_reason = "spawned namespace completion was not observed"
        elif (
            completion_observed
            and now >= visibility_deadline
        ):
            timeout_reason = "hostname proofs remained incomplete after completion"

        if timeout_reason is not None:
            print(
                "hostname proof timeout:",
                f"reason={timeout_reason}",
                f"expected={expected_count}",
                f"observed={len(proof_files)}",
                f"elapsed={elapsed_seconds:.1f}s",
                f"selected_hosts={','.join(expected_hosts)}",
                f"spawned_namespace={completion_tracker.namespace}",
                f"completion_event={completion_tracker.event}",
                f"counts={format_host_counts(expected_hosts, file_hosts)}",
                flush=True
            )

            for proof_file, hostname in file_hosts:
                print(
                    "hostname proof seen:",
                    f"file={os.path.basename(proof_file)}",
                    f"host={hostname or '<empty>'}",
                    flush=True
                )

            for proof_file, error in read_errors:
                print(
                    "hostname proof unreadable:",
                    f"file={os.path.basename(proof_file)}",
                    f"error={error}",
                    flush=True
                )

            raise SystemExit(timeout_reason)

        time.sleep(PROOF_POLL_INTERVAL_SECONDS)


# Expected command:
# python spawn_scaling_multinode_test.py PROCESSES HOSTS SLOTS_PER_NODE
if len(sys.argv) != 4:
    raise SystemExit(
        "usage: spawn_scaling_multinode_test.py "
        "NUMBER_OF_PROCESSES EXPECTED_HOSTS SLOTS_PER_NODE"
    )


# Read the values passed by the shell script.
num_processes = int(sys.argv[1])
expected_hosts = sys.argv[2].split(",")
slots_per_node = int(sys.argv[3])


# Every spawned process creates a unique hostname proof file.
proof_directory = os.path.abspath(".")
proof_pattern = os.path.join(proof_directory, "process_*_host")

for old_proof_file in glob.glob(proof_pattern + "*"):
    os.remove(old_proof_file)


# Read the address of the running PRRTE DVM.
with open("dvm.uri") as file:
    dvm_uri = file.readline().strip()


# Create the PMIx tool.
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


# Create one application for the entire spawned job.
# The hostname and PID make every proof filename unique. Publish each proof
# atomically so the controller cannot observe a partially written file.
proof_directory_shell = shlex.quote(proof_directory)

apps = [
    {
        "cmd": "/bin/bash",
        "argv": [
            "bash",
            "-c",
            (
                'host=$(hostname -s); '
                f'out={proof_directory_shell}/process_${{host}}_$$_host; '
                'tmp="${out}.tmp"; '
                'printf "%s\\n" "$host" > "$tmp"; '
                'mv "$tmp" "$out"'
            )
        ],
        "maxprocs": num_processes
    }
]


completion_tracker = SpawnCompletionTracker()


def spawn_completion_handler(
    event_handler,
    status,
    source,
    info,
    results
):
    """Record job-end notification for exact-namespace correlation."""
    completion_tracker.record(status, source, info, results)

    return pmix.PMIX_EVENT_ACTION_COMPLETE, None


try:
    event_handler_result = tool.register_event_handler(
        [pmix.PMIX_EVENT_JOB_END],
        [],
        spawn_completion_handler
    )
    print("completion handler:", event_handler_result, flush=True)

    if event_handler_result[0] != 0:
        raise SystemExit("spawn completion handler registration failed")

    # Ask PMIx and PRRTE to launch every process and report job completion.
    spawn_result = tool.spawn([
        {
            "key": pmix.PMIX_NOTIFY_COMPLETION,
            "value": True,
            "val_type": pmix.PMIX_BOOL
        }
    ], apps)
    print("spawn:", spawn_result, flush=True)

    if spawn_result[0] != 0:
        raise SystemExit("spawn failed")

    completion_tracker.set_namespace(spawn_result[1])

    proof_files = wait_for_spawn_completion_and_proofs(
        completion_tracker,
        proof_pattern,
        num_processes,
        expected_hosts
    )

    # Read the hostname written by every process.
    observed_hosts = []

    for proof_file in proof_files:
        with open(proof_file) as file:
            hostname = file.readline().strip()

        observed_hosts.append(hostname)
        print(f"{os.path.basename(proof_file)} host:", hostname)

    # Count how many processes ran on each hostname.
    host_counts = Counter(observed_hosts)

    print("expected hosts:", ",".join(expected_hosts))

    for hostname in expected_hosts:
        print(f"host {hostname} process count:", host_counts[hostname])
finally:
    # Disconnect the PMIx tool from PRRTE on success and every failure path.
    finalize_result = tool.finalize()
    print("finalize:", finalize_result, flush=True)


# Verify that every process created a hostname file.
if len(observed_hosts) != num_processes:
    raise SystemExit("not every process created a hostname file")


# Verify that no process ran on an unexpected host.
if set(observed_hosts) != set(expected_hosts):
    raise SystemExit("processes did not run on the expected hosts")


# Verify that every selected node ran the expected number of processes.
for hostname in expected_hosts:
    if host_counts[hostname] != slots_per_node:
        raise SystemExit(
            f"{hostname} ran {host_counts[hostname]} processes; "
            f"expected {slots_per_node}"
        )


if finalize_result != 0:
    raise SystemExit("finalize failed")


print("PLACEMENT VERIFIED")
