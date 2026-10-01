# Tracking Analysis Progress

Use this page when you have submitted an analysis and want to know what it is doing right now, or how to check on it from a script. It covers the status page, the states a job moves through, the dashboard view of everything in flight, and how live updates reach the browser.

## Prerequisites
- A submitted job. See [Submitting Analyses](submit-analyses.html).
- Any role that can view results. Tracking is read-only; the exceptions are `Abort` and re-analysis, listed below.

## Quick Example
```bash
rootcoz results show JOB_ID --fields status,result.analysis_state,result.summary
```

This prints just the three fields a status check needs, instead of the full result payload. The rest of this page covers the same job in the browser.

## Step-by-Step
1. **Open the status page.**

   Jenkins and Prow submissions open `/status/{job_id}` automatically. From the dashboard, clicking a row whose status is `waiting`, `pending`, `running`, `failed`, or `aborted` opens the status page; completed jobs go straight to the report.

   The page shows a status headline, a metadata block, and a live progress list. Because the progress log is stored server-side, reloading the page restores the full history rather than starting over.

2. **Read the status headline.**

   | Status | Badge | What it means |
   | --- | --- | --- |
   | `pending` | `Pending` | The job is queued and has not started |
   | `waiting` | `Waiting` | RootCoz is polling your CI server for the build to finish. Only Jenkins submissions with `Wait for build completion` enabled sit here |
   | `running` | `Running` | Fetching, cloning, or analyzing |
   | `completed` | `Completed` | Finished. The page redirects to the report |
   | `failed` | `Failed` | The job could not complete. The error is shown with its details |
   | `aborted` | `Aborted` | Cancelled by a user before it finished |

   `pending`, `running`, and `waiting` are the active states. The count of active jobs drives the pulsing badge next to `Dashboard` in the sidebar, so a single glance tells you whether the server is busy.

   A seventh badge, `Analysis Timed Out`, is derived rather than stored: when the AI call exceeded its timeout, the failed job is presented as a timeout with the hint that you can re-analyze with a longer one.

3. **Read the metadata rows.**

   | Row | Source |
   | --- | --- |
   | `JOB ID` | The ID to poll from the CLI or the API |
   | `JOB` | The display name, or the source's default name such as `file-analysis` |
   | `BUILD` | Build number or Prow build ID, linked to the CI build when a URL is known |
   | `STATUS` | The badge from the table above |
   | `MAIN AI` | Provider and model for this run |
   | `USAGE / COST` | Token usage, or `GRAFT SAVINGS` when usage is not yet available |
   | `PEERS` | One line per peer model, when peer analysis is enabled |
   | `TEST REPO` | The tests repo URL and ref, linked |
   | `QUEUED` | When the job was accepted |

4. **Follow the progress list.**

   Each phase the pipeline enters is appended to a timestamped list. The most recent entry is highlighted, and the current phase is also shown under the headline.

   | Phase | Shown as |
   | --- | --- |
   | `waiting_for_jenkins`, `waiting_for_build` | Waiting for the build to complete |
   | `fetching` | Fetching test results |
   | `agent_routing` | Routing failure groups to agents |
   | `cross_failure` | Finding cross-failure patterns |
   | `cloning` | Cloning repositories, with a per-repo expandable list showing URL, ref, and state |
   | `analyzing`, `analyzing_failures`, `analyzing_child_jobs` | Analyzing test failures, including the current group number |
   | `peer_review_round_N`, `orchestrator_revising_round_N` | Peer review or main-AI revision, by round |
   | `enriching_jira` | Searching Jira for matching bugs |
   | `saving` | Saving results |

   If a repository fails to clone, that repo is marked in red and the job continues if the remaining context is enough to analyze.

5. **Check for `Source Warnings`.**

   Non-fatal problems from the source appear in their own block, for example GCS access errors or oversized artifacts. The job still completes, so read this block before concluding that a clean-looking report means a clean fetch.

6. **Act on a stuck or unwanted job.**

   `Abort` is available while the job is `waiting`, `pending`, or `running`. Two conditions apply, and the first is easy to miss: the caller must hold **at least the `reviewer` role**, and then it must be either the user who submitted the job or an admin. Being the submitter is not sufficient on its own — if your role is later downgraded from `operator` to `viewer` while your job is still active, the button stays enabled on the status page but the request returns 403. For anyone else the button is disabled with a tooltip explaining why.

   If the job finished and you want a fresh answer, use the header button on the report page: `Analyze` for a submitted job that has not been analyzed yet, `Re-Analyze` for an analyzed one. Both open the same dialog for changing AI settings, and both are disabled while the job is still active. Re-analysis is `operator` or `admin` only. See [Reviewing and Classifying Failures](review-and-classify-failures.html) for the review workflow that follows.

7. **Understand what happens after a server restart.**

   Jobs left in `pending` or `running` cannot be recovered and are marked failed with `Analysis interrupted by server restart. Please re-submit.` Jobs in `waiting` are resumed automatically, so a Jenkins submission that was polling for its build survives a restart.

## Advanced Usage
- **Everything in flight, from the dashboard.** The dashboard lists every job with its status badge, a Mode column reading `Submitted` or `Analyzed`, failure and pass/skip counts, review progress, comment count, and the submitter. Filter by status, review state, analysis state, date range, team, tier, version, and labels. Newest rows appear without a manual refresh, so several submissions can be watched at once.

- **Check status from a script.** `--fields` limits the response to an allowlisted set of paths, which is much cheaper than fetching a full report.

  ```bash
  rootcoz results fields
  rootcoz results show JOB_ID --fields status,result.analysis_state
  rootcoz results list --analysis-state submitted
  rootcoz results analyze JOB_ID
  ```

  `rootcoz results fields` prints the allowlist for your server. Any path outside it is rejected rather than silently ignored, so scripts fail loudly when a field name is wrong. `rootcoz results list` prints `status` and `analysis_state` for every job and takes the same `--analysis-state submitted` filter as the dashboard. `rootcoz results analyze` is the CLI equivalent of `POST /results/{job_id}/analyze` and runs the AI pipeline in place on a submitted job, keeping the same job ID.

  See [CLI Command Reference](cli-reference.html) and [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html).

- **Poll the API directly.** `GET /results/{job_id}` returns the status, the result payload, and the full metadata you need to script your own checks. The same `fields` query parameter is available there. A browser hitting that URL is redirected to the status page whenever the job is `waiting`, `pending`, `running`, **or `failed`** — `ReportPage.load()` sends a `failed` job to `/status/{id}` rather than rendering a report, because there is no analysis to show. The dashboard routes `failed` and `aborted` rows to the status page too, though they differ there: an `aborted` job can still have a result and can render the report if you navigate to it directly. An `aborted` job is the one terminal state where the report renders.

- **Watch a running job for nothing else.** To answer "did anything change on this job right now", subscribe to the `results:{job_id}` stream and listen for `status-changed`. The browser app uses this, but it is the same mechanism an external tool can use.

## Troubleshooting
- **The page looks frozen and the progress list stopped growing.**  
  The status page refetches only when a `status-changed` event arrives, so a dropped event stream leaves it showing the last known state. Reload the page. Nothing is lost: the progress log and the current status are both stored server-side.

- **The sidebar badge disagrees with the dashboard.**  
  The badge counts active jobs server-wide for every status you can reach, while the dashboard is filtered by whatever filters you have set. Clear the filters or check another view before assuming a job is missing.

- **Only some of my open tabs update.**  
  Updates are shared between browser tabs over one connection, and the connection is led by a single tab. If a tab misses events, reload it; it re-subscribes and refetches on mount.

- **A job shows `Analysis interrupted by server restart. Please re-submit.`**  
  The background task did not survive the restart. Submit again, or re-analyze from the report header.

- **`Abort` is greyed out.**  
  Only the submitter of that job and admins can abort it. The tooltip says the same thing.

- **`Abort` and the progress list are missing entirely.**  
  The job is not in an active state. Once it reaches `completed`, `failed`, or `aborted`, the status page stops offering it and the report page takes over.

- **`rootcoz results show --fields` returns an unknown-field error.**  
  The path is not on your server's allowlist. Run `rootcoz results fields` to list the valid paths and retry with those.

## Related Pages
- [Submitting Analyses](submit-analyses.html)
- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Exploring History and Reports](explore-history-and-reports.html)
- [CLI Command Reference](cli-reference.html)
- [API Reference](api-reference.html)
