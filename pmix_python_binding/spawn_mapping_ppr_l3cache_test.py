import glob
import os
import shlex
import sys
import time
from collections import Counter

import pmix


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


for old_file in glob.glob(topology_pattern + "*"):
    os.remove(old_file)

for old_file in glob.glob(process_pattern + "*"):
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


def wait_for_files(pattern, expected_count, description):
    """Wait up to 15 seconds for the requested proof files."""
    proof_files = []

    for _ in range(150):
        proof_files = sorted(glob.glob(pattern))

        if len(proof_files) == expected_count:
            return proof_files

        time.sleep(0.1)

    raise SystemExit(
        f"found {len(proof_files)} {description} files; "
        f"expected {expected_count}"
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
    # First launch one process per node to discover all L3-cache domains.
    topology_command = (
        'host=$(hostname -s); '
        f'out={proof_directory_shell}/topology_${{host}}_$$_l3; '
        'tmp="${out}.tmp"; '
        'for level_file in '
        '/sys/devices/system/cpu/cpu[0-9]*/cache/index*/level; '
        'do '
        '    [ -r "$level_file" ] || continue; '
        '    [ "$(cat "$level_file")" = "3" ] || continue; '
        '    cache_dir=${level_file%/level}; '
        '    cache_id=$(cat "$cache_dir/id" 2>/dev/null || true); '
        '    shared=$(cat "$cache_dir/shared_cpu_list" '
        '2>/dev/null || true); '
        '    [ -n "$shared" ] || continue; '
        '    printf "%s|%s|%s\\n" '
        '"$host" "$cache_id" "$shared"; '
        'done | sort -u > "$tmp"; '
        'mv "$tmp" "$out"'
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

    topology_files = wait_for_files(
        topology_pattern,
        len(expected_hosts),
        "topology proof"
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
        }
    ]

    print("map-by policy:", map_policy)
    print("bind-to policy: core")

    process_spawn_result = tool.spawn(
        process_job_info,
        process_apps
    )

    print("process spawn:", process_spawn_result)

    if process_spawn_result[0] != 0:
        raise SystemExit("mapped process spawn failed")

    process_files = wait_for_files(
        process_pattern,
        num_processes,
        "process proof"
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
