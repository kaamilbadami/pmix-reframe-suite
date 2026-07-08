# PMIx Python Binding Tests

This directory contains ReFrame tests and helper programs for validating the PMIx Python bindings with PRRTE on Frontier.

The tests use a Python controller to connect to a PRRTE DVM through PMIx, spawn simple MPI sleeper jobs, and check that the jobs launch, complete, and report expected output.

## File map

### Shared payload

| File | Purpose |
|------|---------|
| `sleeper_mpi_new.c` | Small MPI program spawned by the Python controllers. It initializes MPI, sleeps briefly, prints `DONE`, and exits. |

### Scaling and mapping tests

These are the earlier PMIx Python tests. The ReFrame wrapper calls a shell helper, and the shell helper calls a Python spawn controller.

| ReFrame test | Shell helper | Python controller | Purpose |
|-------------|--------------|-------------------|---------|
| `pmix_python_scaling_test.py` | `run_pmix_python_scaling_test.sh` | `spawn_scaling_test.py` | Single-node PMIx Python process scaling |
| `pmix_python_scaling_multinode_test.py` | `run_pmix_python_scaling_multinode_test.sh` | `spawn_scaling_multinode_test.py` | Multi-node PMIx Python spawning |
| `pmix_python_mapping_ppr_node_test.py` | `run_pmix_python_mapping_ppr_node_test.sh` | `spawn_mapping_ppr_node_test.py` | PPR node mapping through PMIx Python |

### Compatibility tests

These are the newer PMIx Python compatibility tests. The ReFrame wrapper calls the Python controller directly.

| ReFrame test | Python controller | Purpose |
|-------------|-------------------|---------|
| `pmix_python_worker_threads_compat_test.py` | `run_pmix_python_worker_threads_compat.py` | Tests concurrent spawn submission from Python worker threads |
| `pmix_python_targeted_compat_test.py` | `run_pmix_python_targeted_compat.py` | Tests requested host targeting through `PMIX_HOST` |
| `pmix_python_mixed_thread_compat_test.py` | `run_pmix_python_mixed_thread_compat.py` | Tests mixed job sizes and slot tracking |

## How the tests run

At a high level, the PMIx Python tests follow this pattern:

1. ReFrame requests a Slurm allocation.
2. ReFrame builds or uses the PMIx, PRRTE, and libevent fixtures.
3. The test starts a PRRTE DVM.
4. A Python controller connects through PMIx.
5. The controller spawns `sleeper_mpi_new`.
6. The sleeper jobs print `DONE` and exit.
7. The controller or shell helper prints `PASS` or `FAIL`.
8. ReFrame checks the output for expected pass strings.

## Environment variables

The tests support configurable paths so they can run outside the original development environment.

| Variable | Purpose |
|----------|---------|
| `PMIX_PYTHON` | Optional Python interpreter with the PMIx bindings installed |
| `PYTHON` | Older fallback variable used by shell helpers |
| `PMIX` | PMIx installation path |
| `PRRTE` | PRRTE installation path |
| `LIBEVENT` | libevent installation path |

If `PMIX_PYTHON` is set, the tests use it as the Python interpreter. If it is not set, the tests fall back to the development Python path used during initial Frontier validation.

Example:

    export PMIX_PYTHON=/path/to/python-with-pmix-bindings

## Expected pass output

Different tests check different pass strings.

Examples include:

| Test area | Expected output |
|----------|-----------------|
| Single-node scaling | `PROCESS COUNT ... PASS` |
| Multi-node scaling | `NODE COUNT ... PASS` |
| PPR node mapping | `PPR NODE MAPPING TEST PASS` |
| Worker threads | `WORKER COUNT ... PASS` |
| Targeted placement | `TARGETED PLACEMENT PASS` and `TARGET HOST SET PASS` |
| Mixed job sizes | `MIXED JOB SIZES PASS` and `MIXED JOB SIZE SET PASS` |

The sleeper payload also prints `DONE` after a spawned process completes.

## Notes

The compatibility tests are the most recent additions and are the main files to review first:

1. Worker threads
2. Targeted placement
3. Mixed job sizes

The scaling and PPR mapping tests are still useful because they provide earlier PMIx Python coverage, but they are secondary to the newer compatibility tests.
