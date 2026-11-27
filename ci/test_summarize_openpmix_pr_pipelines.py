import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import summarize_openpmix_pr_pipelines as summary


class SummaryTests(unittest.TestCase):
    def test_pagination_excludes_polls_and_preserves_retries(self):
        noop = {"id": 4, "name": None, "status": "success"}
        first = dict(id=3, name="openpmix-pr:42:" + "a" * 40, status="success",
                     web_url="https://gitlab.example/group/suite/-/pipelines/3")
        retry = dict(first, id=2, status="failed",
                     web_url="https://gitlab.example/group/suite/-/pipelines/2")
        with patch.object(summary, "get_json", side_effect=[
                [first] + [noop] * 99, [retry]]) as request:
            rows = summary.collect("https://gitlab.example/api/v4", "123", "secret", 48)
        self.assertEqual([row[2] for row in rows], ["success", "failed"])
        self.assertEqual(rows[0][3], first["web_url"])
        query = parse_qs(urlsplit(request.call_args[0][0]).query)
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["source"], ["parent_pipeline"])
        self.assertIn("updated_after", query)
        self.assertNotIn("secret", summary.render(rows, 48))

    def test_empty_results_do_not_claim_a_test_passed(self):
        with patch.object(summary, "get_json", return_value=[]):
            rows = summary.collect("https://gitlab.example/api/v4", "123", "secret", 48)
        self.assertIn("No actual PR test pipelines", summary.render(rows, 48))

    def test_invalid_config_never_sends_token(self):
        with patch.object(summary, "get_json") as request:
            for api in ("http://gitlab.example/api/v4", "https://user@gitlab.example/api/v4"):
                with self.assertRaises(summary.DiscoveryError):
                    summary.collect(api, "123", "secret", 48)
            request.assert_not_called()

    def test_private_token_and_malformed_named_results(self):
        with patch.object(summary, "get_json", return_value=[
                dict(id=1, name="openpmix-pr:42:" + "b" * 40, status="unknown")]) as request:
            with self.assertRaises(summary.DiscoveryError):
                summary.collect("https://gitlab.example/api/v4", "123", "secret", 48, True)
        self.assertEqual(request.call_args[0][1]["PRIVATE-TOKEN"], "secret")


if __name__ == "__main__":
    unittest.main()
