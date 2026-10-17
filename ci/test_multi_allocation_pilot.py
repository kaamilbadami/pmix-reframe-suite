"""Two-commit pilot checks using local Git history and no Frontier allocation."""
from pathlib import Path
import subprocess
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

    def test_cli_rejects_unbounded_or_unpinned_pilot(self):
        for shas, extra in [([self.first], ['--select-prrte']),
                            ([self.base, self.first, self.tip], ['--select-prrte']),
                            ([self.first, self.tip], []),
                            ([self.first, self.tip], ['--select-prrte', '--fail-commit', self.tip])]:
            inp = self.root / 'commits'
            inp.write_text('\n'.join(shas) + '\n')
            with self.assertRaises(SystemExit):
                child.main([str(inp), str(self.root / 'child.yml'), '--single-allocation'] + extra)
        self.assertFalse((self.root / 'child.yml').exists())


if __name__ == '__main__':
    unittest.main()
