import unittest
from unittest.mock import patch
from pathlib import Path
import re
import yaml

import audit_pmix_timeouts as audit


class FakeClient:
    project = 126

    def __init__(self, roots, jobs=None, bridges=None, traces=None):
        self.roots, self.jobs = roots, jobs or {}
        self.bridges, self.traces = bridges or {}, traces or {}

    def pages(self, path, query=None):
        if "pipeline_schedules" in path:
            assert query["status"] == "failed"
            return iter(self.roots)
        pipeline = int(path.split("/")[2])
        if path.endswith("jobs"):
            assert query["include_retried"] == "true"
            return iter(self.jobs.get(pipeline, []))
        return iter(self.bridges.get(pipeline, []))

    def get(self, path, **kwargs):
        result = self.traces[int(path.split("/")[2])]
        if isinstance(result, Exception):
            raise result
        return result

    def link(self, value):
        return value


def root(i):
    return {"id": i, "web_url": "https://gitlab.example/pipelines/" + str(i)}


def job(i, reason):
    return {"id": i, "name": "pmix-" + "a" * 40, "status": "failed",
            "failure_reason": reason, "web_url": "https://gitlab.example/jobs/" + str(i)}


class AuditTests(unittest.TestCase):
    def test_children_retries_and_runs_are_counted_separately(self):
        client = FakeClient([root(1), root(4)],
                            jobs={3: [job(10, "script_failure"), job(11, "stuck_or_timeout_failure")],
                                  4: [job(12, "script_failure")]},
                            bridges={1: [{"downstream_pipeline": {"id": 2}}],
                                     2: [{"downstream_pipeline": {"id": 3}}]},
                            traces={10: "ERROR: Job failed: script timeout: context deadline exceeded",
                                    11: "step_script could not run to completion because the timeout was exceeded",
                                    12: "Assertion failed"})
        report = audit.audit(client, 46)
        self.assertTrue(report["complete"])
        self.assertEqual(report["confirmed_execution_timeout_job_attempts"], 2)
        self.assertEqual(report["failed_schedule_runs_with_confirmed_timeouts"], 1)
        self.assertEqual(report["schedule_runs_fully_scanned"], 2)

    def test_stuck_runner_is_not_counted_as_an_execution_limit(self):
        report = audit.audit(FakeClient([root(1)], jobs={1: [job(10, "stuck_or_timeout_failure")]},
                                        traces={10: "No matching runner available"}), 46)
        self.assertEqual(report["confirmed_execution_timeout_job_attempts"], 0)
        self.assertEqual(report["unclassified_or_ambiguous_jobs"], 1)

    def test_missing_trace_is_visible_as_incomplete(self):
        report = audit.audit(FakeClient([root(1)], jobs={1: [job(10, "script_failure")]},
                                        traces={10: audit.AuditError("GitLab returned HTTP 404")}), 46)
        self.assertFalse(report["complete"])
        self.assertEqual(report["unclassified_or_ambiguous_jobs"], 1)
        self.assertIn("lower bounds", audit.render(report))

    def test_denied_history_never_produces_a_complete_zero(self):
        client = FakeClient([])
        with patch.object(client, "pages", side_effect=audit.AuditError("HTTP 403")):
            report = audit.audit(client, 46)
        self.assertFalse(report["complete"])
        self.assertIn("HTTP 403", audit.render(report))

    def test_more_than_one_hundred_pipelines_are_paginated(self):
        client = audit.Client("https://gitlab.example/api/v4", "126", "secret")
        with patch.object(client, "get", side_effect=[[root(i) for i in range(1, 101)], [root(101)]]) as get:
            rows = list(client.pages("/pipeline_schedules/46/pipelines", {"status": "failed"}))
        self.assertEqual(len(rows), 101)
        self.assertEqual(get.call_args[0][1]["page"], 2)

    def test_audit_web_pipeline_does_not_start_the_suite(self):
        parent = yaml.safe_load((Path(__file__).resolve().parent.parent / ".gitlab-ci.yml").read_text())
        self.assertEqual(parent["audit-pmix-schedule-timeouts"]["rules"][0]["if"],
                         '$CI_PIPELINE_SOURCE == "web" && $PMIX_TIMEOUT_AUDIT == "1"')
        self.assertIn({"if": '$PMIX_TIMEOUT_AUDIT == "1"', "when": "never"},
                      parent["pmix-python-suite"]["rules"])
        self.assertNotIn("schedule", parent["audit-pmix-schedule-timeouts"]["rules"][0]["if"].lower())
        self.assertEqual(parent["audit-pmix-schedule-timeouts"]["artifacts"]["when"], "always")
        def enabled(config, env):
            for rule in config["rules"]:
                expression = rule.get("if")
                if expression:
                    expression = re.sub(r"\$([A-Z0-9_]+)",
                                        lambda m: repr(env.get(m[1], "")), expression)
                    if not eval(expression.replace("&&", " and ").replace("||", " or "),
                                {"__builtins__": {}}, {}):
                        continue
                return rule.get("when") != "never"
            return False
        selected = [name for name, config in parent.items()
                    if name != "workflow" and not name.startswith(".") and isinstance(config, dict) and "rules" in config
                    and enabled(config, {"CI_PIPELINE_SOURCE": "web", "PMIX_TIMEOUT_AUDIT": "1"})]
        self.assertEqual(selected, ["audit-pmix-schedule-timeouts"])
        for pr in ("", "1"):
            self.assertFalse(enabled(parent["audit-pmix-schedule-timeouts"],
                                     {"CI_PIPELINE_SOURCE": "schedule", "OPENPMIX_PR_AUTODISCOVERY": pr}))

    def test_invalid_origin_and_cross_instance_links_rejected(self):
        with self.assertRaises(audit.AuditError):
            audit.Client("http://gitlab.example/api/v4", "126", "secret")
        client = audit.Client("https://gitlab.example/api/v4", "126", "secret")
        with self.assertRaises(audit.AuditError):
            client.link("https://other.example/jobs/1")


if __name__ == "__main__":
    unittest.main()
