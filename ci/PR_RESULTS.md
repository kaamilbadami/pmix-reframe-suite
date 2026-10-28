# Checking PR trial results

Open the latest **OpenPMIx PR discovery — 5-minute trial** parent pipeline,
then its `generate-openpmix-pr-pipelines` job. The end of its log lists actual
named PR child pipelines updated in the last 48 hours: PR number, full PMIx
commit, pipeline status, and pipeline link. Empty polls and PMIx master batches
are excluded. Retries remain separate rows. The same snapshot is downloadable
as the job artifact `.ci-openpmix-pr-discovery/pr-summary.txt` (expires in one day).

This is a snapshot taken during discovery, before that poll launches new PR
children. The next poll picks up their status. A successful empty poll does not
mean a PR was tested; an empty summary explicitly says no PR test pipelines
were found. Use an actual PR pipeline's test log to confirm ReFrame results.
If summary retrieval fails, the log says so and PR discovery still continues.

For a standalone read-only query, set `CI_API_V4_URL` to your GitLab instance's
HTTPS API URL, `CI_PROJECT_ID` to this project's numeric ID, and
`GITLAB_READ_TOKEN` to a token with permission to read its pipelines. Keep the
token in the environment; do not put it in command arguments or commit it.
Then run:

```sh
python3 ci/summarize_openpmix_pr_pipelines.py --hours 48
```

Inside CI the script uses the existing `CI_JOB_TOKEN` when no
`GITLAB_READ_TOKEN` is set. It only reads pipeline metadata; it does not start
jobs, change results, or post to GitHub. No extra token is needed for the
scheduled summary. Local validation:

```sh
python3 -m unittest discover -s ci -p test_summarize_openpmix_pr_pipelines.py
```
