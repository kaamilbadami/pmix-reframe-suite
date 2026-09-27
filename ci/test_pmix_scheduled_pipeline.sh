#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)

python3 - "$repo_root/.gitlab-ci.yml" "$repo_root/ci/generate_pmix_child_pipeline.py" <<'PY'
import json
from pathlib import Path
import sys

import yaml


parent = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
pass_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


schedule_rule = {"if": '$CI_PIPELINE_SOURCE == "schedule"'}
always_schedule_rules = [
    {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "always"},
    {"when": "never"},
]
normal_schedule_rules = [schedule_rule, {"when": "never"}]
authoritative_state = (
    "/lustre/orion/gen243/world-shared/kbadami/openpmix-ci/pmix-master.env"
)
generate_name = "generate-pmix-child-pipeline-scheduled"
trigger_name = "trigger-pmix-child-pipeline-scheduled"
collect_name = "collect-pmix-child-results-scheduled"
reconcile_name = "reconcile-pmix-child-results-scheduled"
apply_name = "apply-pmix-reconciled-state-scheduled"
generate = parent[generate_name]
trigger = parent[trigger_name]
collect = parent[collect_name]
reconcile = parent[reconcile_name]
apply = parent[apply_name]

check(parent["variables"]["PMIX_AUTHORITATIVE_STATE_FILE"] == authoritative_state,
      "approved authoritative state default changed")
check("test-shared-state-write" not in parent,
      "temporary shared-state permission job remains")
passed("the approved Lustre env-state path is configured and the probe job is removed")

check(parent["stages"].index(generate["stage"]) < parent["stages"].index(trigger["stage"]),
      "scheduled trigger is not after generation")
check(parent["stages"].index(trigger["stage"]) < parent["stages"].index(collect["stage"]),
      "scheduled collection is not after the trigger")
check(parent["stages"].index(collect["stage"]) < parent["stages"].index(reconcile["stage"]),
      "scheduled reconciliation is not after collection")
check(parent["stages"].index(reconcile["stage"]) < parent["stages"].index(apply["stage"]),
      "scheduled application is not after reconciliation")
check(generate["rules"] == normal_schedule_rules, "scheduled generation rules changed")
check(trigger["rules"] == normal_schedule_rules, "scheduled trigger rules changed")
check(collect["rules"] == always_schedule_rules, "scheduled collection is not always-run")
check(reconcile["rules"] == always_schedule_rules, "scheduled reconciliation is not always-run")
check(apply["rules"] == always_schedule_rules, "scheduled application is not always-run")
passed("the five scheduled jobs are gated and ordered exactly")

check("cache" not in generate, "scheduled generation still consumes GitLab cache")
generate_script = "\n".join(generate["script"])
for required in (
    "PMIX_AUTHORITATIVE_STATE_FILE",
    "apply_reconciled_pmix_state.py snapshot",
    "baseline-pmix-master.env",
    "discover_untested_pmix_commits.sh",
    "generate_pmix_child_pipeline.py",
):
    check(required in generate_script, f"scheduled generation omits {required}")
for forbidden in (
    "PMIX_CHILD_PIPELINE_BASE_SHA",
    "PMIX_CHILD_PIPELINE_FAIL_COMMIT",
    "--fail-commit",
    "should_run_pmix_suite",
):
    check(forbidden not in generate_script,
          f"scheduled generation consumes forbidden pilot/latest behavior: {forbidden}")
check(generate["artifacts"]["paths"] == [
    "ci-scheduled/baseline-pmix-master.env",
    "ci-scheduled/pmix-untested-commits.txt",
    "ci-scheduled/pmix-child-pipeline.yml",
], "scheduled generation artifacts changed")
passed("scheduled generation strictly snapshots authoritative Lustre state")

check(trigger["variables"] == {
    "PRRTE_BRANCH": "v5.0",
    "PRRTE_COMMIT": "22820a01e17547dbf1c4f9628eac327f193caa45",
}, "scheduled trigger changed the pinned PRRTE configuration")
check(trigger["trigger"] == {
    "include": [{
        "artifact": "ci-scheduled/pmix-child-pipeline.yml",
        "job": generate_name,
    }],
    "strategy": "mirror",
}, "scheduled child trigger changed")
passed("scheduled trigger mirrors the generated exact-commit child with pinned PRRTE")

check(collect["needs"] == [
    {"job": generate_name, "artifacts": True},
    {"job": trigger_name, "artifacts": False},
], "scheduled collection needs changed")
collect_script = "\n".join(collect["script"])
check("--trigger-job-name trigger-pmix-child-pipeline-scheduled" in collect_script,
      "collector does not select the scheduled trigger")
check("0|3)" in collect_script and "4|5|6)" in collect_script,
      "collector status policy changed")
check(collect_script.index("pmix_scheduled_status.py clean") <
      collect_script.index("collect_pmix_child_results.py"),
      "stale statuses are not removed before collection")
for required in (
    "pmix_scheduled_status.py publish",
    "--kind collector",
    '--pipeline-id "$CI_PIPELINE_ID"',
    '--suite-commit "$CI_COMMIT_SHA"',
):
    check(required in collect_script, f"collector marker omits {required}")
check(collect["artifacts"]["when"] == "always",
      "collector artifacts are not always retained")
passed("scheduled collection permits incomplete results and preserves hard failures")

check("cache" not in reconcile, "scheduled reconciliation still consumes GitLab cache")
check(reconcile["needs"] == [
    {"job": generate_name, "artifacts": True},
    {"job": collect_name, "artifacts": True},
], "scheduled reconciliation needs changed")
reconcile_script = "\n".join(reconcile["script"])
gate_position = reconcile_script.index("hard collector status prevents reconciliation")
reconciler_position = reconcile_script.index("python3 ci/reconcile_pmix_results.py")
check(gate_position < reconciler_position,
      "hard collector failure is checked after reconciliation")
check(reconcile_script.index("--kind reconciliation") < gate_position,
      "stale reconciliation authority is not removed before collector gating")
check(reconcile_script.index("pmix_scheduled_status.py publish", reconciler_position) >
      reconciler_position,
      "reconciliation authority is published before the reconciler runs")
for required in (
    "PMIX_AUTHORITATIVE_STATE_FILE",
    "pmix_scheduled_status.py verify",
    '--pipeline-id "$CI_PIPELINE_ID"',
    "baseline-pmix-master.env",
    "current-pmix-master.env",
    "pmix-untested-commits.txt",
    "ci-scheduled/collection",
    '--suite-commit "$CI_COMMIT_SHA"',
):
    check(required in reconcile_script, f"reconciliation omits {required}")
check(reconcile["artifacts"] == {
    "when": "always",
    "expire_in": "14 days",
    "paths": [
        "ci-scheduled/current-pmix-master.env",
        "ci-scheduled/reconciliation-status.env",
        "ci-scheduled/reconciliation/reconciliation.env",
        "ci-scheduled/reconciliation/proposed-pmix-master.env",
    ],
}, "scheduled reconciliation artifacts changed")
passed("scheduled reconciliation uses exact baseline/current snapshots and existing logic")

check(apply["resource_group"] == "pmix-python-suite-frontier",
      "state application does not share PMIx serialization")
check("cache" not in apply, "scheduled application still writes GitLab cache")
check(apply["needs"] == [
    {"job": generate_name, "artifacts": True},
    {"job": reconcile_name, "artifacts": True},
], "state application needs changed")
apply_script = "\n".join(apply["script"])
status_gate_position = apply_script.index(
    "reconciliation status prevents state application"
)
application_position = apply_script.index("apply_reconciled_pmix_state.py apply")
check(status_gate_position < application_position,
      "state application runs before reconciliation-status validation")
check("0|3)" in apply_script,
      "state application does not accept complete and blocked reconciliation statuses")
for required in (
    "pmix_scheduled_status.py verify",
    '--pipeline-id "$CI_PIPELINE_ID"',
    "apply_reconciled_pmix_state.py apply",
    "baseline-pmix-master.env",
    '--authoritative-state "$PMIX_AUTHORITATIVE_STATE_FILE"',
    "pmix-untested-commits.txt",
    "reconciliation.env",
    "proposed-pmix-master.env",
):
    check(required in apply_script, f"state application omits {required}")
passed("dedicated serialized application revalidates identity and writes Lustre state")

pull_push_jobs = []
for name, job in parent.items():
    if not isinstance(job, dict):
        continue
    cache = job.get("cache")
    if isinstance(cache, dict) and cache.get("policy") == "pull-push":
        pull_push_jobs.append(name)
check(pull_push_jobs == [],
      f"unexpected pull-push cache writers: {pull_push_jobs}")

suite = parent["pmix-python-suite"]
check(schedule_rule not in suite["rules"], "legacy suite still has a schedule rule")
check(suite["cache"]["policy"] == "pull", "ordinary web suite can push production state")
suite_script = "\n".join(suite["script"])
for forbidden in (
    "Saved successful PMIx suite state",
    'state_file=".ci-state/pmix-master.env"',
    "LAST_SUCCESS_EPOCH=%s",
):
    check(forbidden not in suite_script, "ordinary web suite still writes production state")
check("PMIX_AUTHORITATIVE_STATE_FILE" not in suite_script,
      "ordinary web suite references authoritative scheduled state")
passed("ordinary web suite cannot write authoritative scheduled state")

pilot_generate = parent["generate-pmix-child-pipeline-pilot"]
pilot_collect = parent["collect-reconcile-pmix-child-pipeline-pilot"]
check(pilot_generate["cache"]["policy"] == "pull",
      "manual pilot cache is not read-only")
check("cache" not in pilot_collect and "resource_group" not in pilot_collect,
      "manual pilot gained shared-state authority")
pilot_serialized = json.dumps([pilot_generate, pilot_collect]).lower()
check("pull-push" not in pilot_serialized and "apply_reconciled_pmix_state.py apply" not in pilot_serialized,
      "manual pilot can apply production state")
passed("manual multi-commit pilot remains proposal-only")

generator_source = Path(sys.argv[2]).read_text(encoding="utf-8")
for forbidden in (
    "PMIX_AUTHORITATIVE_STATE_FILE",
    "apply_reconciled_pmix_state",
    "pmix-master.env",
):
    check(forbidden not in generator_source,
          f"generated child jobs gained state-writing capability: {forbidden}")
passed("exact-commit child jobs contain no authoritative-state capability")

scheduled_jobs = [generate, trigger, collect, reconcile, apply]
scheduled_serialized = json.dumps(scheduled_jobs)
for forbidden in ("PMIX_CHILD_PIPELINE_BASE_SHA", "PMIX_CHILD_PIPELINE_FAIL_COMMIT"):
    check(forbidden not in scheduled_serialized,
          f"scheduled flow consumes pilot selector {forbidden}")
check("report_github_status" not in scheduled_serialized,
      "scheduled flow added GitHub status reporting")
check("bash ci/test_apply_reconciled_pmix_state.sh" in suite["script"],
      "application unit test is not run by the local helper suite")
check("bash ci/test_pmix_scheduled_status.sh" in suite["script"],
      "scheduled status unit test is not run by the local helper suite")
check("bash ci/test_pmix_scheduled_pipeline.sh" in suite["script"],
      "scheduled configuration test is not run by the local helper suite")
passed("scheduled flow excludes pilot selectors and unrelated reporting")

print(f"1..{pass_count}")
PY
