"""Two-commit pilot checks using local Git history and no Frontier allocation."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import generate_pmix_child_pipeline as child
import generate_pmix_multi_allocation_pilot as pilot

ROOT = Path(__file__).resolve().parents[1]


class MultiAllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = self.root / 'history'
        subprocess.run(['git', 'init', '-q', '-b', 'master', str(self.history)], check=True)
        header = self.history / 'src/util/pmix_string_copy.h'
        header.parent.mkdir(parents=True)
        header.write_text('char *pmix_getline(FILE *fp);\n')
        self.commit('baseline')
        self.base = pilot.git(self.history, 'rev-parse', 'HEAD')
        self.commit('first')
        self.first = pilot.git(self.history, 'rev-parse', 'HEAD')
        self.commit('second')
        self.tip = pilot.git(self.history, 'rev-parse', 'HEAD')
        self.output = self.root / 'output'

    def commit(self, message):
        subprocess.run(['git', '-C', str(self.history), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.history), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'commit', '-q',
                        '--allow-empty', '-m', message], check=True)

    def test_range_generates_two_full_suite_jobs_and_separate_results(self):
        pilot.generate(self.history, self.tip, self.output)
        self.assertEqual((self.output / 'commits.txt').read_text().splitlines(),
                         [self.first, self.tip])
        self.assertEqual((self.output / 'base-sha.txt').read_text().strip(), self.base)
        text = (self.output / 'pipeline.yml').read_text()
        self.assertEqual(text.count('\npmix-'), 2)
        self.assertEqual(text.count('bash ci/run_pmix_single_allocation.sh'), 2)
        self.assertEqual(text.count('export PMIX_PILOT_TEST_SCOPE=all'), 2)
        self.assertEqual(text.count('PRRTE_COMMIT: "' + child.PRRTE_LEGACY[1] + '"'), 2)
        for sha in (self.first, self.tip):
            self.assertIn('PMIX_COMMIT: "' + sha + '"', text)
            self.assertIn('ci-results/' + sha + '.env', text)
        self.assertIn('${CI_JOB_ID}', text)
        self.assertIn('ci/write_pmix_commit_result.sh', text)
        self.assertIn('ci/cleanup_pmix_slurm_jobs.py', text)
        self.assertIn('sources/manifest.json', text)
        self.assertIn('report.json', text)
        self.assertNotIn('resource_group:', text)
        self.assertNotIn('cache:', text)
        self.assertNotIn('apply_reconciled_pmix_state.py', text)

    def test_parent_routes_only_manual_pilot_and_blocks_pin_forwarding(self):
        # PyYAML is optional on login nodes; parsed validation also runs locally.
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML unavailable')
        config = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
        flag = '$CI_PIPELINE_SOURCE == "web" && $PMIX_MULTI_ALLOCATION_PILOT == "1"'
        generation = config['generate-pmix-multi-allocation-pilot']
        trigger = config['trigger-pmix-multi-allocation-pilot']
        collect = config['collect-reconcile-pmix-multi-allocation-pilot']
        self.assertEqual(generation['rules'], [{'if': flag}, {'when': 'never'}])
        self.assertEqual(trigger['rules'], generation['rules'])
        preparation = generation['script'][0]
        self.assertLess(preparation.index('module load miniforge3/23.11.0-0'),
                        preparation.index('python3 ci/test_multi_allocation_pilot.py'))
        self.assertEqual(collect['rules'], [{'if': flag, 'when': 'always'}, {'when': 'never'}])
        self.assertFalse(trigger['trigger']['forward']['pipeline_variables'])
        self.assertEqual(trigger['trigger']['strategy'], 'mirror')
        for name in ('pmix-python-suite', 'pmix-single-allocation-pilot',
                     'generate-pmix-child-pipeline-pilot', 'trigger-pmix-child-pipeline-pilot',
                     'collect-reconcile-pmix-child-pipeline-pilot'):
            self.assertEqual(config[name]['rules'][0],
                             {'if': '$PMIX_MULTI_ALLOCATION_PILOT == "1"', 'when': 'never'})
        selections = {self.first: child.PRRTE_LEGACY, self.tip: child.PRRTE_GETLINE}
        document = yaml.safe_load(child.render_pipeline(
            [self.first, self.tip], prrte_selections=selections, single_allocation=True))
        for sha in (self.first, self.tip):
            job = document['pmix-' + sha]
            self.assertEqual(job['variables']['PMIX_COMMIT'], sha)
            self.assertEqual(job['variables']['PRRTE_COMMIT'], selections[sha][1])
            self.assertEqual(job['artifacts']['when'], 'always')
            subprocess.run(['bash', '-n'], input=job['script'][0], universal_newlines=True, check=True)
            self.assertNotIn('needs', job)
            self.assertNotIn('resource_group', job)
        for name in ('generate-pmix-multi-allocation-pilot', 'collect-reconcile-pmix-multi-allocation-pilot'):
            subprocess.run(['bash', '-n'], input=config[name]['script'][0], universal_newlines=True, check=True)

    def test_invalid_tip_never_runs_git_or_creates_output(self):
        with patch.object(pilot, 'git') as git:
            for value in ('master', 'a' * 39, 'A' * 40, 'a' * 40 + '\n'):
                with self.assertRaises(SystemExit):
                    pilot.generate(self.history, value, self.output)
            git.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_tip_outside_master_rejected(self):
        subprocess.run(['git', '-C', str(self.history), 'checkout', '-qb', 'other', self.base], check=True)
        self.commit('off-master')
        outside = pilot.git(self.history, 'rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError):
            pilot.generate(self.history, outside, self.output)
        self.assertFalse(self.output.exists())

    def test_merged_range_cannot_expand_pilot(self):
        subprocess.run(['git', '-C', str(self.history), 'checkout', '-qb', 'side', self.base], check=True)
        self.commit('side')
        subprocess.run(['git', '-C', str(self.history), 'checkout', '-q', 'master'], check=True)
        subprocess.run(['git', '-C', str(self.history), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'merge', '-q', '--no-ff',
                        '-m', 'merge', 'side'], check=True)
        with self.assertRaises(SystemExit):
            pilot.generate(self.history, pilot.git(self.history, 'rev-parse', 'HEAD'), self.output)
        self.assertFalse(self.output.exists())

    def test_pins_can_differ_and_standard_generator_stays_standard(self):
        selections = {self.first: child.PRRTE_LEGACY, self.tip: child.PRRTE_GETLINE}
        text = child.render_pipeline([self.first, self.tip], prrte_selections=selections,
                                     single_allocation=True)
        for branch, sha in selections.values():
            self.assertIn('PRRTE_COMMIT: "' + sha + '"', text)
            self.assertIn('PRRTE_BRANCH: "' + branch + '"', text)
        normal = child.render_pipeline([self.first, self.tip], prrte_selections=selections)
        self.assertNotIn('run_pmix_single_allocation.sh', normal)
        self.assertEqual(normal.count('bash ci/run_exact_pmix_commit.sh'), 2)

    def test_general_single_allocation_generator_accepts_zero_one_and_many(self):
        for shas in ([], [self.first], [self.base, self.first, self.tip]):
            inp = self.root / 'commits'
            inp.write_text(''.join(sha + '\n' for sha in shas))
            child.main([str(inp), str(self.root / 'child.yml'), '--single-allocation',
                        '--select-prrte', '--pmix-history', str(self.history)])
            text = (self.root / 'child.yml').read_text()
            self.assertEqual(text.count('bash ci/run_pmix_single_allocation.sh'), len(shas))
            if not shas:
                self.assertIn('no-untested-pmix-commits:', text)

    def test_single_allocation_requires_dependency_selection(self):
        inp = self.root / 'commits'
        inp.write_text(self.tip + '\n')
        with self.assertRaises(SystemExit):
            child.main([str(inp), str(self.root / 'child.yml'), '--single-allocation'])

    def test_parent_backlog_mode_snapshots_live_state_without_apply(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML unavailable')
        config = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
        script = config['generate-pmix-multi-allocation-pilot']['script'][0]
        self.assertIn('PMIX_MULTI_ALLOCATION_FROM_STATE', script)
        self.assertIn('ci/apply_reconciled_pmix_state.py snapshot', script)
        self.assertIn('--state "$PMIX_AUTHORITATIVE_STATE_FILE"', script)
        self.assertIn('--from-state ci-allocation-pilot/checkpoint-snapshot.env', script)
        self.assertNotIn('apply_reconciled_pmix_state.py apply', script)
        self.assertNotIn('cache:', str(config['generate-pmix-multi-allocation-pilot']))

    def test_cli_from_snapshot_uses_frozen_master_and_all_commits(self):
        self.commit('third')
        snapshot = self.root / 'snapshot.env'
        snapshot.write_text('PMIX_COMMIT=' + self.base + '\nSUITE_COMMIT=' + 'd' * 40 +
                            '\nLAST_SUCCESS_EPOCH=12345\n')
        original = snapshot.read_bytes()
        real_run = subprocess.run
        clone_calls = []
        def local_clone(argv, **kwargs):
            argv = list(argv)
            if 'clone' in argv:
                clone_calls.append(argv[:])
                argv[argv.index('https://github.com/openpmix/openpmix.git')] = str(self.history)
            return real_run(argv, **kwargs)
        with patch.object(sys, 'argv', ['generator', 'master', str(self.output),
                                       '--from-state', str(snapshot)]), \
             patch('subprocess.run', side_effect=local_clone):
            pilot.main()
        self.assertEqual(len(clone_calls), 1)
        self.assertEqual(len((self.output / 'commits.txt').read_text().splitlines()), 3)
        self.assertEqual(snapshot.read_bytes(), original)

    def test_checkpoint_range_preserves_state_and_freezes_full_backlog(self):
        from apply_reconciled_pmix_state import snapshot_state, parse_state
        state = self.root / 'live.env'
        content = ('PMIX_COMMIT=' + self.base + '\nSUITE_COMMIT=' + 'd' * 40 +
                   '\nLAST_SUCCESS_EPOCH=12345\n')
        state.write_text(content)
        snapshot = self.root / 'snapshot.env'
        snapshot_state(state, snapshot)
        base = parse_state(snapshot)[0]['PMIX_COMMIT']
        self.commit('third')
        self.commit('fourth')
        frozen = pilot.git(self.history, 'rev-parse', 'master')
        pilot.generate(self.history, frozen, self.output, base=base)
        self.assertEqual(len((self.output / 'commits.txt').read_text().splitlines()), 4)
        self.assertEqual((self.output / 'tip-sha.txt').read_text().strip(), frozen)
        self.commit('arrived after discovery')
        self.assertEqual(len((self.output / 'commits.txt').read_text().splitlines()), 4)
        self.assertEqual(state.read_text(), content)
        self.assertEqual(snapshot.read_text(), content)

    def test_checkpoint_at_master_generates_noop(self):
        pilot.generate(self.history, self.tip, self.output, base=self.tip)
        self.assertEqual((self.output / 'commits.txt').read_text(), '')
        self.assertIn('no-untested-pmix-commits:', (self.output / 'pipeline.yml').read_text())

    def test_checkpoint_not_ancestor_is_rejected(self):
        subprocess.run(['git', '-C', str(self.history), 'checkout', '-qb', 'other', self.base], check=True)
        self.commit('outside')
        outside = pilot.git(self.history, 'rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError):
            pilot.generate(self.history, self.tip, self.output, base=outside)
        self.assertFalse(self.output.exists())

    def test_backlog_mode_includes_merged_commits(self):
        subprocess.run(['git', '-C', str(self.history), 'checkout', '-qb', 'side', self.base], check=True)
        self.commit('side')
        side = pilot.git(self.history, 'rev-parse', 'HEAD')
        subprocess.run(['git', '-C', str(self.history), 'checkout', '-q', 'master'], check=True)
        subprocess.run(['git', '-C', str(self.history), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'merge', '-q', '--no-ff',
                        '-m', 'merge', 'side'], check=True)
        tip = pilot.git(self.history, 'rev-parse', 'HEAD')
        pilot.generate(self.history, tip, self.output, base=self.base)
        commits = (self.output / 'commits.txt').read_text().splitlines()
        self.assertEqual(len(commits), 4)
        self.assertIn(side, commits)
        self.assertEqual(commits[-1], tip)


if __name__ == '__main__':
    unittest.main()
