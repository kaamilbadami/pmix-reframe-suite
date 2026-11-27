"""Add evidence to existing runtime jobs without changing their assertions."""
from pathlib import Path
import shlex


def instrument_test(test):
    helper = Path(__file__).with_name('runtime_diagnostics.py')
    python = shlex.quote(test.pmix.python_env)
    capture = python + ' ./runtime_diagnostics.py'
    commands = [
        'cp ' + shlex.quote(str(helper)) + ' ./runtime_diagnostics.py',
        'pmix_diag_phase() { printf "PMIX_PHASE %s %s\\n" "$(date -u +%FT%TZ)" "$1" | tee -a runtime-phases.log || :; }',
        capture + ' environment > runtime-environment.json 2>&1 || :',
        capture + ' watch --pid $$ > runtime-processes.log 2>&1 &',
        'pmix_diag_watcher=$!',
        # Chain the existing EXIT handler (the isolated PR config uses it to
        # clean node-local runtime directories). Bash emits a shell-quoted body.
        'pmix_diag_previous_exit=$(trap -p EXIT)',
        'pmix_diag_previous_handler=""; if [[ -n $pmix_diag_previous_exit ]]; then pmix_diag_previous_exit=${pmix_diag_previous_exit#trap -- }; pmix_diag_previous_exit=${pmix_diag_previous_exit% EXIT}; eval "pmix_diag_previous_handler=$pmix_diag_previous_exit"; fi',
        'pmix_diag_finish() { pmix_diag_rc=$?; trap - EXIT; set +e; pmix_diag_phase "job-exit rc=$pmix_diag_rc"; kill "$pmix_diag_watcher" 2>/dev/null; wait "$pmix_diag_watcher" 2>/dev/null; if [[ -n $pmix_diag_previous_handler ]]; then pmix_diag_restore_status() { return "$pmix_diag_rc"; }; pmix_diag_restore_status; eval "$pmix_diag_previous_handler"; fi; exit "$pmix_diag_rc"; }',
        'trap pmix_diag_finish EXIT',
    ]
    for index, command in enumerate(test.prerun_cmds, 1):
        label = 'compile' if command.lstrip().startswith(('mpicc ', 'cc ', 'gcc ')) else 'prepare-{}'.format(index)
        commands += ['pmix_diag_phase ' + label + '-start', command,
                     'pmix_diag_phase ' + label + '-finished']
    commands += ['pmix_diag_phase controller-start']
    test.prerun_cmds = commands
    test.keep_files = list(test.keep_files) + ['runtime-*.json', 'runtime-*.log']
