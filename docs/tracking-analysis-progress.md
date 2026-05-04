# Tracking Analysis Progress

Use this page when you want to see whether an analysis is still queued, waiting on Jenkins, actively running, or ready to review. It also shows how to tell normal waiting from a real timeout and how to start a fresh run when the result is outdated.

- A RootCoz run has already been submitted. If not, see [Run Your First Analysis](run-your-first-analysis.html).
- If you use the CLI, `rootcoz` is pointed at the correct server and username.
- You have permission to view the run.

```bash
rootcoz analyze --job-name "folder/job-name" --build-number 123
rootcoz results show JOB_ID
```

The first command queues the run and prints a `Job queued` value plus a `Poll:` link. Open that link in a browser, or run `rootcoz results show JOB_ID` again whenever you want to check the latest stored status from the terminal.

> **Note:** The `Poll:` value can be a relative path such as `/results/...`. Open it on the same RootCoz server you submitted to.

1. Start from the job ID or the `Poll:` link.

   - `rootcoz analyze` returns immediately with `Status: queued`.
   - In the web app, Jenkins submissions go straight to the live status page.
   - You can reopen the same result URL later. Active and failed runs land on the status view, while completed runs open the final report.

2. Read the run status correctly.

   | What you see | What it means | What to do |
   | --- | --- | --- |
   | `queued` | RootCoz accepted the request and created a job ID. | Save the job ID or open the `Poll:` link. |
   | `pending` | The run is in RootCoz's own queue. | Wait for it to start. |
   | `waiting` | RootCoz is polling Jenkins because waiting is enabled. | Keep the status page open, or tune wait settings on the next run. |
   | `running` | AI analysis is in progress. | Keep watching until the report is ready. |
   | `completed` | The report is ready. | Open the final report and review the results. |
   | `failed` | The run stopped before finishing. | Read the error and decide whether to rerun. |
   | `Analysis Timed Out` | The web UI recognized an AI timeout failure. | Rerun with a longer AI timeout. |

   > **Note:** A Jenkins wait timeout still ends as `failed`. The special `Analysis Timed Out` label is for AI timeouts, not for Jenkins wait overruns.

3. Watch the live status page.

   - RootCoz refreshes the page every 10 seconds while the run is active.
   - The progress list keeps timestamped steps such as waiting for Jenkins, analyzing, optional Jira search, saving, and peer review rounds.
   - You can refresh the browser without losing the progress history that was already recorded.

4. Know when the run is finished.

   - A `completed` run moves you to the report automatically.
   - A `failed` run stays on the status page and shows the error so you can decide what to do next.
   - Completed reports show when the run was created, when it completed, and how long the analysis step took.

5. Re-run the job when results need refreshing.

```bash
rootcoz re-analyze JOB_ID
```

   | Re-run method | Best when | What happens |
   | --- | --- | --- |
   | `rootcoz re-analyze JOB_ID` | You want the same analysis again quickly. | RootCoz queues a new run with the stored settings. |
   | `Re-Analyze` in the web app | You want to change the rerun settings. | RootCoz queues a new run and lets you adjust AI provider, model, AI CLI timeout, peer review, Jira search, repositories, artifacts, and force behavior. |

   Every rerun gets a new job ID. The original run stays available.

## Advanced Usage

```bash
rootcoz analyze \
  --job-name "folder/job-name" \
  --build-number 123 \
  --wait \
  --poll-interval 5 \
  --max-wait 45 \
  --ai-cli-timeout 20
```

Use these flags when the default progress behavior is not enough:

- `--wait` and `--no-wait` control whether RootCoz waits for Jenkins before analysis.
- `--poll-interval` controls how often RootCoz checks Jenkins.
- `--max-wait` caps the Jenkins wait. `0` means no limit.
- `--ai-cli-timeout` caps the AI step, not the Jenkins wait.
- `--jenkins-timeout` is separate and only affects individual Jenkins API requests.

> **Tip:** Waiting is on by default. If RootCoz does not have a Jenkins URL, the run cannot stay in `waiting` and will move straight toward analysis instead.

```toml
[defaults]
wait_for_completion = true
poll_interval_minutes = 2
max_wait_minutes = 0
ai_cli_timeout = 10
```

Put default wait and timeout values in `~/.config/rootcoz/config.toml` if you do not want to repeat them on every run.

```bash
rootcoz results dashboard
rootcoz results show JOB_ID --json
```

Use `rootcoz results dashboard` when you want a terminal view of several runs at once. Use `rootcoz results show JOB_ID --json` when you want the full stored response for scripting or automation.

> **Tip:** If the server restarts while a run is still in `waiting`, RootCoz resumes it. Runs that were already `pending` or `running` are better treated as interrupted and may need a rerun.

For the full CLI flag list, see [CLI Command Reference](cli-command-reference.html).

## Troubleshooting

- A run stays in `waiting` longer than expected: the Jenkins build may still be running, or RootCoz may not be able to reach Jenkins. Increase `--max-wait`, shorten `--poll-interval` if you want more frequent checks, or submit with `--no-wait`.
- You see `Analysis Timed Out`: use `Re-Analyze` and raise `AI CLI Timeout`, or submit the build again with a larger `--ai-cli-timeout`. `rootcoz re-analyze JOB_ID` reuses the old timeout.
- The status page says `Failed to reach the server. Retrying...`: keep it open. RootCoz automatically retries transient connection failures.
- You get `Job not found` or `Access denied`: verify the job ID, the selected server, and the user you are signed in as. The run may also have been deleted.
- Re-analysis is rejected because the original run has no stored settings: submit a fresh analysis instead of rerunning that older record.

## Related Pages

- [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html)
- [Finding Runs on the Dashboard](finding-runs-on-the-dashboard.html)
- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)
- [Automating Common Tasks with the CLI](automating-common-tasks-with-the-cli.html)
- [CLI Command Reference](cli-command-reference.html)