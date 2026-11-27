import contextlib
import io
import sys
import unittest
from unittest.mock import patch

import select_prrte_for_source as selector


class PRMasterTests(unittest.TestCase):
    def test_master_stdout_is_one_exact_pair_without_header_selection(self):
        sha = 'c' * 40

        def resolve():
            print('resolution diagnostic')
            return sha

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['select', 'checkout', 'a' * 40, '--prrte-master']), \
                patch.object(selector, 'resolve_prrte_master', side_effect=resolve) as lookup, \
                patch.object(selector.subprocess, 'run') as command, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            selector.main()
            lookup.assert_called_once_with()
            command.assert_not_called()
        self.assertEqual(stdout.getvalue(), 'master ' + sha + '\n')
        self.assertIn('resolution diagnostic', stderr.getvalue())

    def test_failed_resolution_cannot_return_a_pair(self):
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['select', 'checkout', 'a' * 40, '--prrte-master']), \
                patch.object(selector, 'resolve_prrte_master', side_effect=SystemExit(2)), \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                selector.main()
        self.assertEqual(stdout.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
