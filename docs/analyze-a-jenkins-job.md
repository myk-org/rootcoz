# Analyze a Jenkins Job

Run analysis against a Jenkins build, follow it from queue to report, and start another pass when you need updated output or different AI settings. This gets you from a job name and build number to a usable result quickly, without losing the original run when you retry.

> **Note:** The web UI uses the dashboard to monitor and reopen analyses. New Jenkins analyses are submitted through `jji` or the REST API, then tracked from the dashboard and live status view.

## Prerequisites
- A reachable JJI server.
- A Jenkins job name and build number.
- Jenkins access and an AI provider/model already configured on the server, or passed on the request.
- For the browser, a username in the web app if prompted.
- For `jji`, either pass `--server` and `--user`, or use a default CLI server profile.
- If your server uses an allow list, use a permitted username.

## Quick Example

```bash
jji --server http://localhost:8000 --user alice analyze \
  --job-name "my-job" \
  --build-number 42 \
  --provider claude \
  --model opus

jji --server http://localhost:8000 --user alice status <job-id>

jji --server http://localhost:8000 --user alice re-analyze <job-id>
```

The first command queues the analysis and prints a new `job_id` plus a poll link. Use that `job_id` in the next commands to watch the run or queue another analysis of the same Jenkins build.

> **Tip:** If you already set a default CLI server profile, you can usually omit `--server` and `--user`. See [Configuration and Environment Reference](configuration-and-environment-reference.html) for details.

## Step-by-Step
1. Queue the analysis.

```bash
jji --server http://localhost:8000 --user alice analyze \
  --job-name "my-job" \
  --build-number 42 \
  --provider claude \
  --model opus
```

JJI returns immediately with `queued`, a new `job_id`, and a `result_url`. Keep the `job_id`; you will use it to watch the run and open the finished report.

2. Watch the run from the status view or dashboard.

```bash
jji --server http://localhost:8000 --user alice status <job-id>
```

Open the dashboard in your browser to watch the same run live. Active rows open the status view, and both the dashboard and status view refresh automatically.

| Status | What it means | What to do |
| --- | --- | --- |
| `queued` | JJI accepted the request | Save the `job_id` |
| `pending` | The analysis is waiting in JJI's queue | Keep watching |
| `waiting` | JJI is waiting for Jenkins to finish the build | Let the build finish, or change wait settings on a new submission |
| `running` | AI analysis is in progress | Stay on the status view |
| `completed` | The report is ready | Open the result |
| `failed` | The run stopped before completing | Read the error and decide whether to rerun |
| `Timed Out` | The web UI recognized an AI timeout | Rerun with a longer AI timeout or a different model |

> **Note:** `Timed Out` is a web UI label for AI-analysis timeouts. The API and CLI still report the underlying run as `failed`.

3. Open the finished result.

```bash
jji --server http://localhost:8000 --user alice results show <job-id>
```

In the browser, click the completed row in the dashboard. If you open the returned `result_url` before the run finishes, JJI redirects you to the live status view automatically.

See [Investigate Failure History](investigate-failure-history.html) for details if you need to compare recurring failures across builds. See [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html) for details if the result should become a ticket.

4. Run it again when you need another pass.

```bash
jji --server http://localhost:8000 --user alice re-analyze <job-id>
```

Use this when you want the fastest retry with the same saved settings. JJI creates a new analysis record and a new `job_id`, but it keeps the same Jenkins job and build number.

Use the `Re-Analyze` button on a failed status page or a completed report when you need to change AI settings before retrying. That path can override provider/model, AI timeout, prompt, repo context, Jira settings, and peer-review settings.

> **Warning:** `Re-Analyze` does not trigger a new Jenkins build. If you need fresh Jenkins output, rerun the job in Jenkins first and then submit the new build number with `jji analyze` or `POST /analyze`.

## Advanced Usage
### Submit Through the REST API

```bash
curl -sS -X POST "http://localhost:8000/analyze" \
  -H "Content-Type: application/json" \
  -H "Cookie: jji_username=alice" \
  -d '{"job_name":"my-job","build_number":42,"ai_provider":"claude","ai_model":"opus"}'
```

Poll the returned job with `GET /results/<job-id>` until it reaches `completed` or `failed`. While a run is still active, `GET /results/<job-id>` returns HTTP `202`; once it finishes, it returns HTTP `200`.

### Re-Analyze Through the REST API With Overrides

```bash
curl -sS -X POST "http://localhost:8000/re-analyze/<job-id>" \
  -H "Content-Type: application/json" \
  -H "Cookie: jji_username=alice" \
  -d '{"ai_provider":"claude","ai_model":"opus","ai_cli_timeout":20}'
```

Use this when the quick CLI rerun is too limited. The rerun keeps the original Jenkins job and build number, but lets you override the shared analysis fields that the endpoint accepts.

### Pick the Right Rerun Path

| If you need to... | Use |
| --- | --- |
| Retry the same Jenkins build with the same saved settings | `jji re-analyze <job-id>` |
| Retry the same Jenkins build with different AI settings | The `Re-Analyze` button or `POST /re-analyze/<job-id>` |
| Analyze a newer Jenkins build | A fresh `jji analyze` or `POST /analyze` request with the new build number |
| Change Jenkins wait behavior | A fresh `jji analyze` or `POST /analyze` request |
| Script against the full status payload | `jji --json status <job-id>` |

### Useful `jji analyze` Flags

| Need | Option |
| --- | --- |
| Skip waiting for Jenkins and queue the analysis immediately | `--no-wait` |
| Wait for Jenkins, but poll less often | `--poll-interval 5` |
| Stop waiting after a fixed amount of time | `--max-wait 30` |
| Analyze a build even if Jenkins marked it successful | `--force` |

The live status view can show more than one phase on longer runs, including waiting for Jenkins, analyzing test failures, analyzing child jobs, Jira enrichment, peer-review rounds, and the final save step. The progress log is persisted, so refreshing the page does not erase what has already happened.

See [CLI Command Reference](cli-command-reference.html) for details on every command and flag. See [REST API Reference](rest-api-reference.html) for details on every request field and status code. See [Configuration and Environment Reference](configuration-and-environment-reference.html) for details on reusable CLI server profiles and server defaults.

## Troubleshooting
- You cannot see `Re-Analyze`: wait for the run to fail or complete. Active runs stay on the live status flow.
- `400` when you submit a run usually means the request is missing required fields such as `job_name`, `build_number`, `ai_provider`, or `ai_model`, or the build number is not numeric.
- `403` from the API or `Access denied` in the browser usually means your user is not allowed to use this server or view that job.
- `Job not found` usually means the `job_id` is wrong or the analysis was deleted.
- `Timed Out` in the web UI means the AI analysis timed out. Retry with a longer AI timeout or a different model.
- After a server restart, jobs that were still `waiting` resume automatically. Jobs that were already `pending` or `running` should be requeued.

## Related Pages

- [Analyze Your First Jenkins Job](analyze-your-first-jenkins-job.html)
- [Customize AI Analysis](customize-ai-analysis.html)
- [Review and Classify Failures](review-and-classify-failures.html)
- [Investigate Failure History](investigate-failure-history.html)
- [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html)