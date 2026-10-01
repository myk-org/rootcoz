# Submitting Analyses

Use this page when you have CI output and want RootCoz to turn it into a report. It covers the four input modes, what each one needs, the difference between collecting CI results and running AI analysis over them, and the role you need before you can submit anything.

## Prerequisites
- A running RootCoz server. See [Quickstart](quickstart.html) or [Deploying Rootcoz](deploy-rootcoz.html).
- An account with the `operator` or `admin` role. `reviewer` and `viewer` can read results but cannot submit.
- At least one AI provider credential for `claude`, `gemini`, or `cursor`, unless you are only collecting CI results.
- Your input: a Jenkins job and build, a Prow job and build ID, a JUnit XML file, or raw JUnit XML text.

## Quick Example
```bash
rootcoz analyze --source file --file junit-results.xml
```

This queues a JUnit XML analysis from the terminal and prints the job ID to poll. The same work from the browser is `New Analysis` → `Upload File` → `Submit Analysis`.

## Step-by-Step
1. **Open `New Analysis`.**

   The form loads your server defaults first, so most fields are already filled in. If the defaults request times out, the form still works and shows a warning that it fell back to local values.

   You are signed in as `operator` or `admin`; otherwise the `New Analysis` link is not available and the API returns a permission error.

2. **Pick the input mode that matches what you have.**

   The four mode buttons are `Jenkins Job`, `Prow Job`, `Paste XML`, and `Upload File`. `Upload File` and `Paste XML` are both JUnit XML input and send the same `file` request type; the difference is only how the XML gets into the form.

   | Mode | `type` sent | Required fields | What happens after submit |
   | --- | --- | --- | --- |
   | `Jenkins Job` | `jenkins` | `Job Name`, `Build Number` | Status page, then the report |
   | `Prow Job` | `prow` | `Job Name`, `Build ID` | Status page, then the report |
   | `Upload File` | `file` | a `.xml` file | Report opens directly |
   | `Paste XML` | `file` | `JUnit XML Content` | Report opens directly |

   `Paste XML` and `Upload File` only accept JUnit XML. Console output, log files, and HTML error pages produce a report with nothing to analyze.

3. **Fill in only what the mode needs.**

   Everything outside the mode's own section is optional and falls back to your server settings.

   | Field | Appears in | Notes |
   | --- | --- | --- |
   | `Job Name` | `Jenkins Job`, `Prow Job` | Jenkins uses the full path, for example `folder/job-name` |
   | `Build Number` | `Jenkins Job` | Positive integer |
   | `Build ID` | `Prow Job` | Numeric only; the field strips non-digits as you type |
   | `Jenkins URL`, `Jenkins User`, `Jenkins Password / Token` | `Jenkins Job` | Override the server default for this run only |
   | `Wait for build completion` | `Jenkins Job` | On by default. Off means analyze the build as it stands now |
   | `Poll Interval (min)`, `Max Wait (min, 0 = no limit)` | `Jenkins Job` | Only shown when `Wait for build completion` is on |
   | `Prow URL`, `GCS Bucket` | `Prow Job` | Override the server default for this run only |
   | `GCS Prefix` | `Prow Job` | Leave empty. RootCoz resolves it from `prowjob.json` or the PR directory pointer |
   | `JUnit XML Content` | `Paste XML` | Raw JUnit XML |
   | `AI Provider`, `AI Model` | always | Must be an available provider on the server, or a key on your own account |
   | `Tags` | always | Free-form, added to the job and merged with rule-assigned labels |
   | `Fetch build artifacts` / `Fetch GCS build artifacts` | `Jenkins Job`, `Prow Job` | On by default |

   > **Note:** Jenkins and Prow validate what you type before the request is sent. An invalid Prow job name or a non-numeric build ID is rejected client-side; `GCS Prefix` is also checked against the job name and build ID server-side when you fill it in.

4. **Click `Submit Analysis`.**

   The button stays disabled until the mode's required fields are valid and an AI provider and model are available. `Paste XML` and `Upload File` go straight to the report page; `Jenkins Job` and `Prow Job` open the status page first because they have to reach your CI server and wait for the build.

   See [Tracking Analysis Progress](track-analysis-progress.html) for what the status page shows.

5. **Know what runs after the request is accepted.**

   `POST /analyze` returns `202` immediately with a `job_id` and does the real work in the background. In order:

   | Stage | What happens |
   | --- | --- |
   | Wait | With `Wait for build completion` on, the job sits in `waiting` and polls Jenkins until the build finishes |
   | Fetch | The source plugin collects failures, passed tests, and skipped tests from the CI job or the XML |
   | Preflight | RootCoz confirms the AI provider and model are actually reachable before starting expensive work |
   | Group | Failures are grouped by error signature: a SHA-256 hash of the normalized error message and stack trace, with timestamps, UUIDs, pod names, and build numbers stripped out first |
   | Clone | The tests repo and any additional repos are cloned into a workspace, and `.rootcoz/` agents, skills, and extensions are copied in |
   | Analyze | One AI call per unique error signature, not per failing test. The analysis is then applied to every test that shares that signature |
   | Cross-failure pass | A final pass looks for patterns spanning multiple failure groups |
   | Save | Results, all test outcomes, and token usage are persisted |

   Two consequences worth knowing before you submit. A run with fifty failing tests that share three distinct errors makes three AI calls, not fifty. And if every group fails, the job is marked `failed` rather than quietly reporting partial results.

   > **Note:** Analysis sessions run with a restricted tool set (`read`, `ls`, `find`, `grep`). The AI can read the cloned workspace but cannot run shell commands.

6. **Check the result.**

   The report groups failures by the same error signature used during analysis, so a card can cover several tests. See [Reviewing and Classifying Failures](review-and-classify-failures.html) for the review and classification workflow, and [Exploring History and Reports](explore-history-and-reports.html) to find an older run.

## Advanced Usage
- **Collect results now, analyze later.** `POST /submit` runs the exact same fetch and storage as `POST /analyze` and then stops before the clone and AI stages. The job is stored with `analysis_state=submitted` and the summary `CI results have been collected and stored. AI analysis has not run.` Later, `POST /results/{job_id}/analyze` runs the AI pipeline in place against the same job ID. Use this when you want a cheap, immediate record of a build and only need the expensive analysis for the ones that matter.

  | | `POST /analyze` | `POST /submit` |
  | --- | --- | --- |
  | CI fetch and storage | yes | yes |
  | Repository clone, workspace, AI calls | yes | no |
  | Resulting `analysis_state` | `analyzed` | `submitted` |
  | Needs AI configuration | yes | no |

  In the web UI, a submitted job shows `Submitted` in the dashboard Mode column and an `Analyze` button in the report header instead of `Re-Analyze`.

  > **Tip:** Jobs that predate this split are stored as `analyzed`, so you never have to guess what a missing `analysis_state` means.

- **Change the analysis context for one run.** `Tests Repo URL` and `Ref / Branch`, `Additional Repositories`, `Raw Prompt`, `Peer Analysis`, `Jira Integration`, and `AI Call Timeout` are all per-submission overrides that take precedence over server settings. See [Configuring Analysis Context](configure-analysis-context.html) for details.

- **Submit from the terminal.** `rootcoz analyze` and `rootcoz submit` take the same flags, and `--source` selects between `jenkins`, `file`, and `prow`. The CLI reads the same server for defaults, so anything you set in the form is set in your CLI profile.

  ```bash
  rootcoz analyze --source jenkins --job-name folder/job-name --build-number 123
  rootcoz submit --source jenkins --job-name folder/job-name --build-number 123
  ```

  See [CLI Command Reference](cli-reference.html) for the full flag list and [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html) for scripted workflows.

- **Reuse a stored submission.** `Re-Analyze` in the report header replays the original request with your overrides applied and produces a new job ID. Sensitive fields such as the tests repo token are stored encrypted and restored automatically, so you do not re-enter credentials. The new report links back to its origin.

## Troubleshooting
- **I do not see `New Analysis`, or the API returns a permission error.**  
  Your account is `viewer` or `reviewer`. Submitting requires `operator` or `admin`. Set `DEFAULT_USER_ROLE=operator` for a local trial, or ask an admin to change your role.

- **The form says `Select an available AI provider and model.`**  
  No provider the form lists is usable for this account. Add your own API key in your account settings, ask an admin to configure server providers, or use `POST /submit` and run the AI later with `POST /results/{job_id}/analyze`.

- **`Submit Analysis` is greyed out.**  
  The required fields for the selected mode are not filled in. Jenkins needs both `Job Name` and a positive `Build Number`; Prow needs a valid `Job Name` and a numeric `Build ID`; the XML modes need non-empty XML content.

- **A Jenkins or Prow submission fails immediately.**  
  The server could not reach CI. Check `Jenkins URL`, `Jenkins User`, and `Jenkins Password / Token`, or `Prow URL` and `GCS Bucket`, either in the form or in the server environment variables.

- **The Prow job fails to find artifacts.**  
  Leave `GCS Prefix` empty so RootCoz resolves it, and confirm the `GCS Bucket` actually holds the build. Source-level problems such as denied GCS access are shown under `Source Warnings` on the status page rather than failing the run outright.

- **The report has no failures but the XML was not empty.**  
  RootCoz only analyzes `failure` and `error` test cases in JUnit XML. Verify you uploaded JUnit XML and not console output, and check the report header's passed and skipped counts.

- **The job is `completed` with `No test failures found in the provided input.`**  
  The fetch succeeded and there was nothing to analyze, so RootCoz skipped the AI, the clone, and the workspace entirely. Test outcomes are still saved and the counts still appear on the dashboard.

## Related Pages
- [Quickstart](quickstart.html)
- [Tracking Analysis Progress](track-analysis-progress.html)
- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Configuring Analysis Context](configure-analysis-context.html)
- [CLI Command Reference](cli-reference.html)
- [API Reference](api-reference.html)
