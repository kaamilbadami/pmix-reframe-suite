# One-off master schedule timeout audit

After this commit is mirrored to GitLab, choose **Build > Pipelines > New pipeline**,
select `fix/scheduled-batch-lifecycle`, and add only this variable:

```text
PMIX_TIMEOUT_AUDIT=1
```

Run the pipeline. Its `audit-pmix-schedule-timeouts` shell job only reads GitLab
metadata and logs. It does not submit Slurm jobs, build dependencies, launch
PMIx tests, post statuses, or update master state. The normal manual suite is
excluded. Do not combine this flag with other manual pilot flags. Ordinary
scheduled pipelines do not include the audit job.

The job scans all pages of **failed runs of schedule 46**, traverses both child
levels, and includes failed retried job attempts. Its log and artifacts
`ci-timeout-audit/summary.txt` and `report.json` report:

- Failed schedule runs scanned and how many contained a confirmed execution timeout.
- Number of individual job attempts that hit an execution limit, with job links.
- Other failed jobs and ambiguous/unavailable evidence separately.

This is not a failure rate across all scheduled polls. Successful parent runs,
including any earlier failed attempts in them, are outside this audit. Deleted
pipelines are not discoverable. A timeout does not establish whether Slurm queue
waiting, a hang, or another cause consumed the execution allowance.

GitLab's `stuck_or_timeout_failure` reason combines different situations. The
audit only counts it as an execution timeout if a job log contains an explicit
execution-limit message. It never equates missing logs or denied history with
zero timeouts. Partial scans are marked INCOMPLETE and exit unsuccessfully,
while preserving the findings collected so far. Traces are inspected in memory;
raw logs and authentication tokens are not included in the report.

The job tries its existing `CI_JOB_TOKEN`. Some instances do not permit this
token to read schedule history or job traces. If the report says HTTP 401/403,
an authorized project maintainer can supply a masked, protected CI variable
`GITLAB_READ_TOKEN` with `read_api` scope and access to this project. Do not paste
the value into chat or command arguments. No token or permissions are created
or changed by the audit. The standalone script uses the same environment.

Local checks:

```sh
python3 -m unittest discover -s ci -p test_audit_pmix_timeouts.py
```
