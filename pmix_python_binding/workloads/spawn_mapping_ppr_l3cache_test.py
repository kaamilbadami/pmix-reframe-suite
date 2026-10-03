import glob
import os
import shlex
import sys
import threading
import time
from collections import Counter

import pmix


TOPOLOGY_PROOF_TIMEOUT_SECONDS = 60
TOPOLOGY_COMPLETION_TIMEOUT_SECONDS = 60
PROCESS_COMPLETION_TIMEOUT_SECONDS = 60
PROCESS_PROOF_TIMEOUT_SECONDS = 30
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


class TopologyCompletionTracker:
    """Track completion of one exact topology-probe namespace."""

    def __init__(self):
        self._completion = threading.Event()
        self._lock = threading.Lock()
        self._sources = {}
        self.namespace = None
        self.source = None

    def record(self, source):
        """Retain a completion event, including one received early."""
        source_copy = dict(source)
        source_namespace = source_copy.get("nspace")

        if source_namespace is None:
            return

        with self._lock:
            self._sources[source_namespace] = source_copy

            if source_namespace == self.namespace:
                self.source = source_copy
                self._completion.set()

    def set_namespace(self, namespace):
        """Select the exact namespace and apply any retained event."""
        with self._lock:
            self.namespace = namespace
            self.source = self._sources.get(namespace)

            if self.source is not None:
                self._completion.set()

    def wait(self, timeout_seconds):
        """Wait for completion of the selected namespace."""
        return self._completion.wait(timeout_seconds)


def spawn_after_topology_completion(
    tool,
    process_job_info,
    process_apps,
    completion_tracker,
    timeout_seconds
):
    """Launch the mapped job only after the topology job completes."""
    if not completion_tracker.wait(timeout_seconds):
        raise SystemExit(
            "timed out waiting for topology namespace completion: "
            f"namespace={completion_tracker.namespace}, "
            f"timeout={timeout_seconds}s"
        )

    print(
        "topology namespace completion observed:",
        f"namespace={completion_tracker.namespace}",
        f"source={completion_tracker.source}",
        flush=True
    )

    print("mapped spawn call starting", flush=True)
    return tool.spawn(process_job_info, process_apps)


# Expected command:
# python spawn_mapping_ppr_l3cache_test.py HOSTS PROCESSES_PER_L3CACHE
if len(sys.argv) != 3:
    raise SystemExit(
        "usage: spawn_mapping_ppr_l3cache_test.py "
        "EXPECTED_HOSTS PROCESSES_PER_L3CACHE"
    )


expected_hosts = sys.argv[1].split(",")
processes_per_l3cache = int(sys.argv[2])


if any(not hostname for hostname in expected_hosts):
    raise SystemExit("expected host list contains an empty hostname")


if processes_per_l3cache < 1:
    raise SystemExit("processes per L3 cache must be at least 1")


proof_directory = os.path.abspath(".")
proof_directory_shell = shlex.quote(proof_directory)

topology_pattern = os.path.join(
    proof_directory,
    "topology_*_l3"
)

process_pattern = os.path.join(
    proof_directory,
    "process_*_l3"
)

started_pattern = os.path.join(
    proof_directory,
    "started_*_l3"
)


for old_file in glob.glob(topology_pattern + "*"):
    os.remove(old_file)

for old_file in glob.glob(process_pattern + "*"):
    os.remove(old_file)

for old_file in glob.glob(started_pattern + "*"):
    os.remove(old_file)


with open("dvm.uri") as file:
    dvm_uri = file.readline().strip()


tool = pmix.PMIxTool()


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


topology_completion = TopologyCompletionTracker()
process_completion = SpawnCompletionTracker()


def topology_completion_handler(
    event_handler,
    status,
    source,
    info,
    results
):
    """Retain both jobs' events for exact-namespace correlation."""
    topology_completion.record(source)
    process_completion.record(status, source, info, results)

    return pmix.PMIX_EVENT_ACTION_COMPLETE, None


def wait_for_files(
    pattern,
    expected_count,
    description,
    timeout_seconds
):
    """Wait up to timeout_seconds for the requested proof files."""
    proof_files = []
    wait_started = time.monotonic()
    attempts = int(timeout_seconds / PROOF_POLL_INTERVAL_SECONDS)

    for attempt in range(attempts + 1):
        proof_files = sorted(glob.glob(pattern))

        if len(proof_files) == expected_count:
            print(
                f"{description} files complete: "
                f"{len(proof_files)}/{expected_count}",
                flush=True
            )
            return proof_files

        if attempt == attempts:
            break

        if attempt % 10 == 0:
            print(
                f"waiting for {description} files: "
                f"{len(proof_files)}/{expected_count}",
                flush=True
            )

        time.sleep(PROOF_POLL_INTERVAL_SECONDS)

    elapsed_seconds = time.monotonic() - wait_started

    print(
        f"timed out waiting for {description} files: "
        f"timeout={timeout_seconds}s, elapsed={elapsed_seconds:.1f}s, "
        f"found={len(proof_files)}/{expected_count}",
        flush=True
    )

    if pattern == topology_pattern:
        for artifact_pattern in (
            "l3_topology_*.started",
            "l3_topology_*.status",
            "l3_topology_*.stderr",
            "l3_topology_*.tmp",
            "topology_*_l3.tmp"
        ):
            artifact_present = bool(glob.glob(os.path.join(
                proof_directory,
                artifact_pattern
            )))
            print(
                f"timeout artifact {artifact_pattern}: "
                f"present={artifact_present}",
                flush=True
            )

    if pattern == process_pattern:
        started_files = sorted(glob.glob(started_pattern))

        print(
            f"process start markers: "
            f"{len(started_files)}/{expected_count}",
            flush=True
        )

        for started_file in started_files:
            print(
                f"started: {os.path.basename(started_file)}",
                flush=True
            )

    raise SystemExit(
        f"found {len(proof_files)} {description} files; "
        f"expected {expected_count}"
    )


def wait_for_mapped_completion_and_proofs(
    tracker, pattern, expected_count,
    completion_timeout_seconds=PROCESS_COMPLETION_TIMEOUT_SECONDS,
    visibility_timeout_seconds=PROCESS_PROOF_TIMEOUT_SECONDS
):
    """Require successful mapped-job completion before judging proofs."""
    if not tracker.wait(completion_timeout_seconds):
        raise SystemExit(
            "mapped namespace completion was not observed: "
            f"namespace={tracker.namespace}"
        )
    print("mapped namespace completion observed:", tracker.namespace, flush=True)
    require_successful_job_termination(tracker.event)
    return wait_for_files(
        pattern, expected_count, "process proof", visibility_timeout_seconds
    )


def parse_cpu_list(cpu_list_text):
    """Expand a Linux CPU-list string such as 0-3,8 into a set."""
    cpu_numbers = set()

    for section in cpu_list_text.split(","):
        section = section.strip()

        if not section:
            continue

        try:
            if "-" in section:
                first_text, last_text = section.split("-", 1)
                first_cpu = int(first_text)
                last_cpu = int(last_text)

                if first_cpu > last_cpu:
                    raise ValueError

                cpu_numbers.update(
                    range(first_cpu, last_cpu + 1)
                )
            else:
                cpu_numbers.add(int(section))

        except ValueError as error:
            raise SystemExit(
                f"invalid Linux CPU list: {cpu_list_text!r}"
            ) from error

    return cpu_numbers


finalize_result = None

try:
    event_handler_result = tool.register_event_handler(
        [pmix.PMIX_EVENT_JOB_END], [], topology_completion_handler
    )
    if event_handler_result[0] != pmix.PMIX_SUCCESS:
        raise SystemExit("topology completion handler registration failed")

    # First launch one process per node to discover all L3-cache domains.
    # Diagnostics are best-effort: the EXIT trap preserves the topology
    # command's status even if it cannot write the status artifact.
    topology_command = (
        'pid=$$; '
        'rank=${PMIX_RANK:-unknown}; '
        f'diag={proof_directory_shell}/l3_topology_${{rank}}_${{pid}}; '
        'started="${diag}.started"; '
        'context="${diag}.context"; '
        'child_stderr="${diag}.stderr"; '
        'status_file="${diag}.status"; '
        'printf "shell_pid=%s\n" "$pid" > "$started"; '
        'set +e; '
        'set -o pipefail; '
        'start_epoch=$(date +%s); '
        'start_timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ); '
        'out=""; '
        'tmp=""; '
        'write_status() { '
        '    exit_status=$?; '
        '    set +e; '
        '    end_epoch=$(date +%s); '
        '    end_timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ); '
        '    elapsed_seconds=$((end_epoch - start_epoch)); '
        '    temporary_exists=no; '
        '    final_exists=no; '
        '    [ -n "$tmp" ] && [ -e "$tmp" ] '
        '        && temporary_exists=yes; '
        '    [ -n "$out" ] && [ -e "$out" ] '
        '        && final_exists=yes; '
        '    status_tmp="${status_file}.tmp"; '
        '    { '
        '        printf "exit_status=%s\n" "$exit_status"; '
        '        printf "start_timestamp=%s\n" "$start_timestamp"; '
        '        printf "end_timestamp=%s\n" "$end_timestamp"; '
        '        printf "elapsed_seconds=%s\n" "$elapsed_seconds"; '
        '        printf "temporary_path=%s\n" "$tmp"; '
        '        printf "temporary_exists=%s\n" "$temporary_exists"; '
        '        printf "final_path=%s\n" "$out"; '
        '        printf "final_exists=%s\n" "$final_exists"; '
        '    } > "$status_tmp"; '
        '    mv "$status_tmp" "$status_file"; '
        '    trap - EXIT; '
        '    exit "$exit_status"; '
        '}; '
        'trap write_status EXIT; '
        'exec 2> "$child_stderr"; '
        'host=$(hostname -s); '
        f'out={proof_directory_shell}/topology_${{host}}_$$_l3; '
        'tmp="${out}.tmp"; '
        '{ '
        '    printf "timestamp=%s\n" "$start_timestamp"; '
        '    printf "pwd=%s\n" "$(pwd -P)"; '
        '    printf "hostname=%s\n" "$host"; '
        '    printf "PATH=%s\n" "${PATH-}"; '
        '    printf "shell_pid=%s\n" "$pid"; '
        '    printf "shell=%s\n" "${SHELL-}"; '
        '    printf "bash_version=%s\n" "${BASH_VERSION-}"; '
        '    printf "BASH_ENV=%s\n" "${BASH_ENV-}"; '
        '    printf "PMIX_NAMESPACE=%s\n" "${PMIX_NAMESPACE-}"; '
        '    printf "PMIX_RANK=%s\n" "${PMIX_RANK-}"; '
        '    for variable_name in '
        '        CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL '
        '        CONDA_EXE _CE_CONDA _CE_M; '
        '    do '
        '        if [ "${!variable_name+x}" = x ]; '
        '        then '
        '            printf "%s=%s\n" '
        '                "$variable_name" "${!variable_name}"; '
        '        fi; '
        '    done; '
        '} > "$context"; '
        '{ '
        'scan_read_status=0; '
        'for level_file in '
        '/sys/devices/system/cpu/cpu[0-9]*/cache/index*/level; '
        'do '
        '    [ -r "$level_file" ] || continue; '
        '    if ! level=$(cat "$level_file"); '
        '    then '
        '        scan_read_status=1; '
        '        continue; '
        '    fi; '
        '    [ "$level" = "3" ] || continue; '
        '    cache_dir=${level_file%/level}; '
        '    if ! cache_id=$(cat "$cache_dir/id"); '
        '    then '
        '        scan_read_status=1; '
        '        continue; '
        '    fi; '
        '    if ! shared=$(cat "$cache_dir/shared_cpu_list"); '
        '    then '
        '        scan_read_status=1; '
        '        continue; '
        '    fi; '
        '    [ -n "$shared" ] || continue; '
        '    printf "%s|%s|%s\\n" '
        '"$host" "$cache_id" "$shared"; '
        'done; '
        '[ "$scan_read_status" -eq 0 ]; '
        '} | sort -u > "$tmp"; '
        'scan_status=$?; '
        'if [ "$scan_status" -eq 0 ]; '
        'then '
        '    mv "$tmp" "$out"; '
        '    scan_status=$?; '
        'fi; '
        'exit "$scan_status"'
    )

    topology_apps = [
        {
            "cmd": "/bin/bash",
            "argv": [
                "bash",
                "-c",
                topology_command
            ],
            "maxprocs": len(expected_hosts)
        }
    ]

    topology_job_info = [
        {
            "key": pmix.PMIX_MAPBY,
            "value": "ppr:1:node",
            "val_type": pmix.PMIX_STRING
        },
        {
            "key": pmix.PMIX_NOTIFY_COMPLETION,
            "value": True,
            "val_type": pmix.PMIX_BOOL
        }
    ]

    print("topology probe policy: ppr:1:node")

    topology_spawn_result = tool.spawn(
        topology_job_info,
        topology_apps
    )

    print("topology spawn:", topology_spawn_result)

    if topology_spawn_result[0] != 0:
        raise SystemExit("topology probe spawn failed")

    topology_completion.set_namespace(topology_spawn_result[1])

    topology_files = wait_for_files(
        topology_pattern,
        len(expected_hosts),
        "topology proof",
        TOPOLOGY_PROOF_TIMEOUT_SECONDS
    )

    topology_domains = {}
    topology_cache_ids = {}

    for topology_file in topology_files:
        with open(topology_file) as file:
            lines = [
                line.strip()
                for line in file
                if line.strip()
            ]

        if not lines:
            raise SystemExit(
                f"{os.path.basename(topology_file)} contains no L3 caches"
            )

        for line in lines:
            fields = line.split("|")

            if len(fields) != 3:
                raise SystemExit(
                    f"invalid topology proof line: {line}"
                )

            hostname, cache_id, shared_cpu_list = fields

            topology_domains.setdefault(hostname, set()).add(
                shared_cpu_list
            )

            cache_key = (hostname, shared_cpu_list)

            if (
                cache_key in topology_cache_ids
                and topology_cache_ids[cache_key] != cache_id
            ):
                raise SystemExit(
                    f"inconsistent L3 cache ID for "
                    f"{hostname} CPUs {shared_cpu_list}"
                )

            topology_cache_ids[cache_key] = cache_id

    if set(topology_domains) != set(expected_hosts):
        raise SystemExit(
            "topology probes did not run on exactly the expected hosts"
        )

    expected_cache_domains = set()

    for hostname in expected_hosts:
        domains = topology_domains.get(hostname, set())

        if not domains:
            raise SystemExit(
                f"no L3-cache domains discovered on {hostname}"
            )

        print(
            f"host {hostname} L3 cache count:",
            len(domains)
        )

        for shared_cpu_list in sorted(domains):
            cache_id = topology_cache_ids[
                (hostname, shared_cpu_list)
            ]

            print(
                f"host {hostname} L3 cache "
                f"{cache_id or '(no ID)'} CPUs:",
                shared_cpu_list
            )

            expected_cache_domains.add(
                (hostname, shared_cpu_list)
            )

    total_l3_caches = len(expected_cache_domains)
    num_processes = (
        total_l3_caches * processes_per_l3cache
    )

    print("expected hosts:", ",".join(expected_hosts))
    print("total L3 cache count:", total_l3_caches)
    print(
        "processes per L3 cache:",
        processes_per_l3cache
    )
    print("total process count:", num_processes)

    # Each mapped process records its CPU and matching L3-cache domain.
    process_command = (
        'host=$(hostname -s); '
        'pid=$$; '
        'rank=${PMIX_RANK:-unknown}; '
        f'started={proof_directory_shell}/started_${{host}}_${{pid}}_${{rank}}_l3; '
        'started_tmp="${started}.tmp"; '
        'printf "%s|%s|%s\\n" "$host" "$pid" "$rank" '
        '> "$started_tmp"; '
        'mv "$started_tmp" "$started"; '
        f'out={proof_directory_shell}/process_${{host}}_${{pid}}_l3; '
        'tmp="${out}.tmp"; '
        "cpu=$(awk '{print $39}' /proc/$$/stat); "
        "affinity=$(awk '/^Cpus_allowed_list:/ {print $2}' "
        "/proc/$$/status); "
        'cache_id=""; '
        'shared=""; '
        'for level_file in '
        '/sys/devices/system/cpu/cpu${cpu}/cache/index*/level; '
        'do '
        '    [ -r "$level_file" ] || continue; '
        '    [ "$(cat "$level_file")" = "3" ] || continue; '
        '    cache_dir=${level_file%/level}; '
        '    cache_id=$(cat "$cache_dir/id" 2>/dev/null || true); '
        '    shared=$(cat "$cache_dir/shared_cpu_list" '
        '2>/dev/null || true); '
        '    break; '
        'done; '
        '[ -n "$shared" ] || exit 1; '
        f'printf "%s|%s|%s|%s|%s|%s\\n" '
        '"$host" "$pid" "$cpu" "$affinity" '
        '"$cache_id" "$shared" > "$tmp"; '
        'mv "$tmp" "$out"'
    )

    process_apps = [
        {
            "cmd": "/bin/bash",
            "argv": [
                "bash",
                "-c",
                process_command
            ],
            "maxprocs": num_processes
        }
    ]

    map_policy = (
        f"ppr:{processes_per_l3cache}:l3cache"
    )

    process_job_info = [
        {
            "key": pmix.PMIX_MAPBY,
            "value": map_policy,
            "val_type": pmix.PMIX_STRING
        },
        {
            "key": pmix.PMIX_BINDTO,
            "value": "core",
            "val_type": pmix.PMIX_STRING
        },
        {
            "key": pmix.PMIX_NOTIFY_COMPLETION,
            "value": True,
            "val_type": pmix.PMIX_BOOL
        }
    ]

    print("map-by policy:", map_policy)
    print("bind-to policy: core")

    process_spawn_result = spawn_after_topology_completion(
        tool,
        process_job_info,
        process_apps,
        topology_completion,
        TOPOLOGY_COMPLETION_TIMEOUT_SECONDS
    )

    print(
        "L3 topology completion diagnostic:",
        f"topology_namespace={topology_completion.namespace}",
        f"completion_source={topology_completion.source}",
        f"mapped_spawn_result={process_spawn_result}",
        flush=True
    )

    print("process spawn:", process_spawn_result)

    if process_spawn_result[0] != 0:
        raise SystemExit("mapped process spawn failed")

    if not process_spawn_result[1]:
        raise SystemExit("mapped spawn returned no namespace")
    process_completion.set_namespace(process_spawn_result[1])

    process_files = wait_for_mapped_completion_and_proofs(
        process_completion,
        process_pattern,
        num_processes
    )

    cache_counts = Counter()
    observed_cache_domains = set()

    for process_file in process_files:
        with open(process_file) as file:
            line = file.readline().strip()

        fields = line.split("|")

        if len(fields) != 6:
            raise SystemExit(
                f"invalid process proof line: {line}"
            )

        (
            hostname,
            pid_text,
            cpu_text,
            affinity,
            cache_id,
            shared_cpu_list
        ) = fields

        cpu_number = int(cpu_text)
        cache_key = (hostname, shared_cpu_list)

        print(
            f"{os.path.basename(process_file)}:",
            f"host={hostname}",
            f"pid={pid_text}",
            f"cpu={cpu_number}",
            f"affinity={affinity}",
            f"l3_id={cache_id or '(no ID)'}",
            f"l3_cpus={shared_cpu_list}"
        )

        if hostname not in expected_hosts:
            raise SystemExit(
                f"process ran on unexpected host {hostname}"
            )

        if cache_key not in expected_cache_domains:
            raise SystemExit(
                f"process reported unexpected L3 cache "
                f"{hostname}:{shared_cpu_list}"
            )

        expected_cache_id = topology_cache_ids[cache_key]

        if (
            expected_cache_id
            and cache_id
            and cache_id != expected_cache_id
        ):
            raise SystemExit(
                f"process reported L3 cache ID {cache_id}; "
                f"expected {expected_cache_id}"
            )

        shared_cpus = parse_cpu_list(shared_cpu_list)
        affinity_cpus = parse_cpu_list(affinity)

        if not affinity_cpus:
            raise SystemExit(
                f"process {pid_text} reported an empty CPU affinity"
            )

        if cpu_number not in affinity_cpus:
            raise SystemExit(
                f"CPU {cpu_number} is outside process "
                f"{pid_text} affinity {affinity}"
            )

        if cpu_number not in shared_cpus:
            raise SystemExit(
                f"CPU {cpu_number} is not in reported L3 CPU list "
                f"{shared_cpu_list}"
            )

        if not affinity_cpus.issubset(shared_cpus):
            raise SystemExit(
                f"process {pid_text} affinity {affinity} extends "
                f"outside L3 CPU list {shared_cpu_list}"
            )

        observed_cache_domains.add(cache_key)
        cache_counts[cache_key] += 1

    if len(process_files) != num_processes:
        raise SystemExit(
            f"found {len(process_files)} process proof files; "
            f"expected {num_processes}"
        )

    if observed_cache_domains != expected_cache_domains:
        raise SystemExit(
            "processes did not use every expected L3-cache domain"
        )

    for hostname, shared_cpu_list in sorted(
        expected_cache_domains
    ):
        observed_count = cache_counts[
            (hostname, shared_cpu_list)
        ]

        cache_id = topology_cache_ids[
            (hostname, shared_cpu_list)
        ]

        print(
            f"host {hostname} L3 cache "
            f"{cache_id or '(no ID)'} process count:",
            observed_count
        )

        if observed_count != processes_per_l3cache:
            raise SystemExit(
                f"{hostname} L3 cache "
                f"{cache_id or shared_cpu_list} ran "
                f"{observed_count} processes; expected "
                f"{processes_per_l3cache}"
            )

finally:
    finalize_result = tool.finalize()
    print("finalize:", finalize_result)


if finalize_result != 0:
    raise SystemExit("finalize failed")


print("PPR L3CACHE PLACEMENT VERIFIED")
