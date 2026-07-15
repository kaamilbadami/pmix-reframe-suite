import unittest

from pmix_python_binding.common.pmix_event_utils import (
    format_pmix_job_term_status,
    get_pmix_info_value,
)


class TestGetPMIxInfoValue(unittest.TestCase):
    def test_success_status_with_bytes_constant(self):
        info = [{'key': 'pmix.job.term.status', 'value': 0}]

        self.assertEqual(
            get_pmix_info_value(info, b'pmix.job.term.status'),
            0
        )

    def test_failed_status_is_preserved(self):
        info = [{'key': 'pmix.job.term.status', 'value': -1}]

        self.assertEqual(
            get_pmix_info_value(info, b'pmix.job.term.status'),
            -1
        )

    def test_missing_status_returns_none(self):
        self.assertIsNone(
            get_pmix_info_value([], b'pmix.job.term.status')
        )

    def test_string_constant_is_supported(self):
        info = [{'key': 'pmix.job.term.status', 'value': 0}]

        self.assertEqual(
            get_pmix_info_value(info, 'pmix.job.term.status'),
            0
        )

    def test_job_termination_status_is_not_decoded_as_pmix_error(self):
        self.assertEqual(
            format_pmix_job_term_status(7),
            'PMIX_JOB_TERM_STATUS=7 (application termination status)'
        )


if __name__ == '__main__':
    unittest.main()
