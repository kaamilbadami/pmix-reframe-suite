# PMIx ReFrame Test Suite

A [ReFrame](https://reframe-hpc.readthedocs.io/en/stable/)-based test suite for PMIx and PRRTE on Frontier. The repository builds `libevent`, OpenPMIx, and PRRTE from source and exercises PMIx through Python binding, functional, placement, concurrency, failure-path, and startup tests.

## Repository layout

| Path | Purpose |
|------|---------|
| `libevent_build_class.py` | Fetch and build libevent |
| `pmix_build_class.py` | Fetch and build OpenPMIx with Python bindings |
| `prrte_build_class.py` | Fetch and build PRRTE against the suite-built PMIx and libevent |
| `pmix_python_binding/reframe/` | The 11 ReFrame checks run by the PMIx Python CI suite |
| `pmix_python_binding/controllers/` | PMIx Python spawn controllers |
| `pmix_python_binding/workloads/` | Spawned Python, C, mapping, and scaling workloads |
| `pmix_python_binding/wrappers/` | Slurm-allocation wrappers for mapping and scaling checks |
| `pmix_python_binding/unit_tests/` | Unit tests for shared PMIx event utilities |
| `build_pmix_test.py` | Fixtures that build the external `pmix-tests` workloads |
| `run_pmix_test.py` | Root functional suite for hostname, cycle, prun-wrapper, and manystress coverage |
| `prte_startup/` | Standalone PRTE startup performance checks and configuration |
| `ci/` | CI entry point, gating, GitHub status, artifact collection, and shell tests |
| `sysconfig.yaml` | Frontier `frontier:batch` ReFrame configuration |
| `setup_env.sh` | Optional environment setup for the root functional suite |

The shared build fixtures follow this dependency order:

```text
fetch_libevent -> build_libevent
fetch_pmix ---------------------> build_pmix
fetch_prrte ---------------------------------> build_prrte
                    build_libevent ----------^     ^
                    build_pmix --------------------|
```

## Installation

```bash
git clone https://github.com/kaamilbadami/pmix-reframe-suite.git
cd pmix-reframe-suite

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "Cython==3.2.6" "reframe-hpc==4.10.0"
```

The tracked configurations use the Frontier `gen243` project. Adjust the account, partition, modules, or environment definitions before using the suite on another system.

## PMIx source selection

`fetch_pmix` builds the latest commit on OpenPMIx `master` by default. A different branch can be selected with a ReFrame variable:

```bash
reframe -C sysconfig.yaml -c pmix_python_binding/reframe \
  --system=frontier:batch \
  -S fetch_pmix.branch=my-branch \
  -l
```

To build an exact commit, provide both its branch and full commit SHA. The fetch fixture verifies that the commit exists and is an ancestor of the selected remote branch:

```bash
reframe -C sysconfig.yaml -c pmix_python_binding/reframe \
  --system=frontier:batch \
  -S fetch_pmix.branch=master \
  -S fetch_pmix.commit=0123456789abcdef0123456789abcdef01234567 \
  -r --keep-stage-files
```

`PMIX_COMMIT` is the environment-variable equivalent used by CI and is convenient for scripted exact-commit runs:

```bash
export PMIX_COMMIT=0123456789abcdef0123456789abcdef01234567
```

PRRTE and libevent remain selectable by version, for example:

```bash
-S fetch_prrte.version=4.1.0 -S fetch_libevent.version=2.1.12
```

## PMIx Python CI suite

The CI suite runs the PMIx event utility unit tests, confirms that ReFrame discovers exactly 11 checks, and then runs all checks on `frontier:batch`.

| ReFrame check | Test file | Coverage |
|---------------|-----------|----------|
| `PMIxPythonScalingTest` | `pmix_python_scaling_test.py` | Single-node process-count scaling |
| `PMIxPythonScalingMultinodeTest` | `pmix_python_scaling_multinode_test.py` | One-, two-, and four-node spawning and placement |
| `PMIxPythonMappingPPRNodeTest` | `pmix_python_mapping_ppr_node_test.py` | Explicit processes-per-node mapping |
| `PMIxPythonMappingPPRL3CacheTest` | `pmix_python_mapping_ppr_l3cache_test.py` | L3-cache mapping, binding, and topology validation |
| `PMIxPythonWorkerThreadsCompat1Test` | `pmix_python_worker_threads_compat_test.py` | Single Python dispatch-worker behavior |
| `PMIxPythonWorkerThreadsCompat2Test` | `pmix_python_worker_threads_compat_test.py` | Concurrent two-worker spawn submission and participation |
| `PMIxPythonTargetedCompatTest` | `pmix_python_targeted_compat_test.py` | Requested-host targeting through `PMIX_HOST` |
| `PMIxPythonMixedThreadCompatTest` | `pmix_python_mixed_thread_compat_test.py` | Mixed job sizes and slot accounting |
| `PMIxPythonChildTimeoutTest` | `pmix_python_child_timeout_test.py` | Bounded controller failure for a long-running child |
| `PMIxPythonEventFailurePropagationTest` | `pmix_python_event_failure_test.py` | Nonzero child termination propagated through PMIx events |
| `PMIxPythonTargetHostFailurePropagationTest` | `pmix_python_target_host_failure_test.py` | Invalid target-host spawn failure propagation |

The interpreter selected through `PMIX_PYTHON` is also used to build and run the PMIx Python bindings. It must be executable and able to import Cython. If no override is supplied directly to the ReFrame fixtures, the build falls back to `python3`.

### Run the CI suite locally

```bash
export PMIX_PYTHON="$PWD/.venv/bin/python"
export RFM_BIN="$PWD/.venv/bin/reframe"

# Run unit tests and validate the 11-check ReFrame listing only.
bash ci/run_pmix_python_suite.sh --list-only

# Run unit tests, validate discovery, and execute all 11 checks.
bash ci/run_pmix_python_suite.sh
```

## Root functional suite

The root suite is a separate entry point for nine functional checks covering hostname launch, `hello_world`, PMIx initialize/finalize cycles, `prun-wrapper`, and `manystress`. It uses fixtures from `build_pmix_test.py` to clone and build the external `pmix-tests` workloads.

```bash
source setup_env.sh
reframe -C ./sysconfig.yaml -c run_pmix_test.py \
  --system=frontier:batch -r
```

`setup_env.sh` activates `.venv`, preserves stage files, selects `sysconfig.yaml`, and places this suite's ReFrame prefix under `outputdir/`.

## PRTE startup suite

The PRTE startup suite is independent of the two PMIx suites. It contains one startup timing check without explicit slot counts and one with 32 advertised slots per node.

```bash
reframe -C prte_startup/prte_startup_config.py \
  -c prte_startup \
  --system=frontier:compute -r
```

The startup workloads locate `prte` and `pterm` through `PRTE_DIR`, `PATH`, or the supported repository-relative dependency layouts.

## GitLab pipelines

The tracked workflow accepts only two pipeline sources:

- A manual pipeline started from the GitLab web interface always runs the complete PMIx Python suite.
- An hourly scheduled pipeline evaluates the OpenPMIx and suite state before deciding whether to run the complete suite.

Merge-request and push pipelines are not enabled by `.gitlab-ci.yml`.

### Scheduled-pipeline gating

Before an hourly scheduled run, `ci/should_run_pmix_suite.sh` queries the current SHA of OpenPMIx `master` and compares it with the cached state from the last successful complete run. The state records:

```text
PMIX_COMMIT=<OpenPMIx SHA>
SUITE_COMMIT=<pmix-reframe-suite SHA>
LAST_SUCCESS_EPOCH=<UTC epoch>
```

The complete suite runs when any of the following is true:

- no valid successful-run state is available;
- the OpenPMIx SHA changed;
- the suite SHA changed;
- both SHAs changed;
- at least 86,400 seconds have passed since the last successful complete run; or
- the saved timestamp is in the future.

The scheduled pipeline intentionally skips the complete suite only when both SHAs are unchanged and the last successful complete run is less than 24 hours old. A new state is saved only after a successful full run, so the hourly schedule also provides at least one daily health run when neither repository changes.

Manual and scheduled pipelines report pending and final commit status to GitHub. A Frontier resource group prevents overlapping PMIx suite jobs.

### Manual trusted-author `pmix-tests` PR pilot

The opt-in `PMIX_TESTS_PR_EXECUTION_PILOT=1` workflow is a bounded manual MVP
for same-repository pull requests in `kaamilbadami/pmix-tests`. Eligibility is
limited to the existing `rhc54` and `kaamilbadami` allowlist, fork-originated
PRs are rejected, and code from an approved author is trusted to execute under
the Frontier service account. This workflow is not a sandbox for arbitrary,
hostile, or fork-originated PR code. Supporting that threat model would require
a separate account, container boundary, or equivalent stronger isolation.

The workflow uses three jobs. Preparation retrieves and revalidates the exact
head SHA and posts pending status. Both artifact-producing jobs remove their
fixed output directory before an early failure can expose stale data, and the
strict preparation and result schemas are bound to the current numeric GitLab
pipeline ID. Execution receives the validated record, removes the GitLab job
environment with `env -i`, rebuilds the trusted Frontier module environment,
checks out only the exact SHA from the fixed public URL, and runs one
suite-owned adapter that builds the PR-owned `prrte/hello_world` source and
launches two ranks with a foreground `prterun`. Finalization runs from a fresh
suite checkout, strictly compares
the preparation and result pipeline IDs and SHAs, and reports status only on
the original preparation SHA. GitHub and GitLab credential variables are
removed before approved PR code executes, but the shared service-account
filesystem remains within the approved-author trust assumption.

Trusted CI tools are installed in project-controlled shared storage under
`/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools`. The pilot uses the
fixed PMIx Python 3.10.20 interpreter at
`pmix-py310/bin/python`, the fixed ReFrame 4.10.0 executable at
`reframe-4.10/bin/reframe`, and ReFrame's Python packages at
`reframe-4.10/lib/python3.11/site-packages`. These installations are
readable and executable, but not writable, by the `gen243` group and are
usable from the credential-scrubbed `env -i` execution environment.

`checkout.env`, `checkout-commit.txt`, and `test-source.sha256` are diagnostic
artifacts for operators, not trusted security evidence. The final decision uses
only the strict preparation and execution-result records. A complete GitLab
pipeline cancellation can prevent the always-run finalization job from running
and therefore leave the GitHub status pending; a separate cleanup mechanism is
future work.

## Artifacts and generated files

Normal ReFrame runs may create these generated directories at the repository root:

| Path | Contents |
|------|----------|
| `output/` | Copied job and build output |
| `stage/` | Staged sources, generated scripts, builds, and test-specific logs |
| `perflogs/` | ReFrame performance logs |
| `reports/` | ReFrame reports when report output is enabled |
| `outputdir/` | Prefix used by `setup_env.sh` for the root functional suite |

GitLab CI always runs `ci/collect_ci_artifacts.sh` in `after_script`. It recreates `ci-artifacts/`, writes `ci-artifacts/artifact-summary.txt`, and copies any available `output/`, `perflogs/`, and `reports/` trees. It also preserves selected PMIx fetch/build evidence under the same relative `stage/frontier/batch/pmix_test/...` paths, including:

- `rfm_build.sh`, `rfm_build.out`, and `rfm_build.err`;
- the PMIx `config.log`;
- the installed `python-site-packages` link; and
- `pmix-commit.env`.

GitLab uploads `ci-artifacts/` even for failed or intentionally skipped pipelines and retains it for 14 days. `.ci-venv/`, `ci-artifacts/`, ReFrame output trees, and reports are generated content covered by `.gitignore`.

## Execution flow

1. Fetch libevent, the selected OpenPMIx branch/commit, PRRTE, and—when required—the external `pmix-tests` repository.
2. Build libevent, PMIx with Python bindings, and PRRTE in fixture dependency order.
3. Build or stage the workload required by each entry point.
4. Configure `PATH`, `PYTHONPATH`, and `LD_LIBRARY_PATH` from the fixture installations.
5. Run the selected PMIx Python, root functional, or PRTE startup checks and apply their ReFrame sanity/performance validation.
