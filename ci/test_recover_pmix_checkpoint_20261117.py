import json
from pathlib import Path
import tempfile
import unittest

import recover_pmix_checkpoint_20261117 as recovery
from apply_reconciled_pmix_state import parse_state


class RecoveryTests(unittest.TestCase):
    def test_recovery_retains_backup_gap_and_actual_test_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.env"
            # A later health retest of the older baseline must not prevent
            # advancing to an independently verified descendant commit.
            original = "PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH={}\n".format(
                recovery.EXPECTED, "a" * 40, recovery.SUCCESS_EPOCH + 60).encode()
            state.write_bytes(original)
            recovery.recover(state, root / "output")
            values, _ = parse_state(state)
            self.assertEqual(values["PMIX_COMMIT"], recovery.TARGET)
            self.assertEqual(values["SUITE_COMMIT"], recovery.SUITE)
            self.assertEqual(int(values["LAST_SUCCESS_EPOCH"]), recovery.SUCCESS_EPOCH)
            record = json.loads((root / "output/recovery.json").read_text())
            self.assertEqual(Path(record["backup"]).read_bytes(), original)
            self.assertEqual(record["verified_job_id"], 256304)
            self.assertEqual(record["accepted_gap"]["pmix_commit"][:8], "24735901")
            self.assertEqual((root / "output/after.env").read_bytes(), state.read_bytes())
            recovery.recover(state, root / "repeat")

    def test_unexpected_or_newer_state_is_never_overwritten(self):
        for commit, epoch in [("b" * 40, 1794800000),
                              ("63de821" + "0" * 33, recovery.SUCCESS_EPOCH + 1),
                              ("caa7dc477abfea115e3170afe2c6ddfff4914afa", 1794800000)]:
            with self.subTest(commit=commit, epoch=epoch), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / "state.env"
                original = "PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH={}\n".format(
                    commit, recovery.SUITE, epoch).encode()
                state.write_bytes(original)
                with self.assertRaises(ValueError):
                    recovery.recover(state, root / "output")
                self.assertEqual(state.read_bytes(), original)
                self.assertEqual(list(root.glob("*.manual-*")), [])


if __name__ == "__main__":
    unittest.main()
