# Automating Common Tasks with the CLI

The `rootcoz` CLI is the scripting surface for RootCoz. This page walks the workflows an integrator actually automates — authenticating without a prompt, submitting an analysis, waiting for it, pulling structured results, reviewing, and pushing to an exporter — and then composes them into one script. For the full flag list, see [CLI Command Reference](cli-reference.html).

## Prerequisites

- The `rootcoz` CLI on your `PATH`.
- A server URL and an API key. Get the key with `rootcoz register USERNAME` or from an administrator.
- `jq` for the examples below. Everything except the CSV mode of `rootcoz admin token-usage` is JSON with `--json`.

## Quick Example

```bash
export ROOTCOZ_SERVER=https://rootcoz.example.com
export ROOTCOZ_API_KEY=your-api-key

JOB_ID=$(rootcoz --json analyze --source jenkins -j nightly -b 412 \
  --provider claude --model claude-opus-4-6 | jq -r .job_id)

until [ "$(rootcoz --json status "$JOB_ID" | jq -r .status)" = "completed" ]; do sleep 15; done

rootcoz --json results show "$JOB_ID" --fields result.summary,result.failed_count
```

That submits a Jenkins analysis, waits for it to finish, and prints a two-field summary — the whole submit-poll-read loop in six lines.

> **Warning:** The `until` loop above exits only on `completed`. A job that ends as `failed` or `aborted` never satisfies the condition, so the script polls forever instead of reporting the problem. In automation, branch on the terminal statuses and exit non-zero:

```bash
STATUS=$(rootcoz --json status "$JOB_ID" | jq -r .status)
case "$STATUS" in
  completed) rootcoz --json results show "$JOB_ID" --fields result.summary,result.failed_count ;;
  failed|aborted|error) echo "analysis $JOB_ID ended as $STATUS" >&2; exit 1 ;;
  *) echo "unexpected status $STATUS" >&2; exit 1 ;;
esac
```

Bound the wait as well (for example `for i in $(seq 1 240)`) so a job stuck in `pending` or `running` cannot hang the pipeline. See [Tracking Analysis Progress](track-analysis-progress.html) for every status value.

## Step-by-Step

1. **Authenticate without any interactive prompt.**

   The CLI has no session store. `rootcoz auth login` only validates a key and prints your role; it does not save anything. For unattended runs, supply credentials in one of these ways, in order of preference:

   | Method | How |
   | --- | --- |
   | Environment | `export ROOTCOZ_SERVER=... ROOTCOZ_API_KEY=... ROOTCOZ_USERNAME=...` |
   | Config file | `api_key` and `username` under `[servers.NAME]` in `~/.config/rootcoz/config.toml` |
   | Per command | `rootcoz --server NAME --api-key KEY ...` |

   ```toml
   # ~/.config/rootcoz/config.toml
   [default]
   server = "prod"

   [servers.prod]
   url = "https://rootcoz.example.com"
   username = "ci-bot"
   api_key = "rootcoz_..."
   ```

   With that file in place, every later command drops `--server` and `--api-key`.

   ```bash
   rootcoz config servers   # confirm which name is the default
   rootcoz auth whoami      # confirm the role the key resolves to
   ```

   > **Warning:** A key written into a config file is plaintext. On a shared CI runner, prefer `ROOTCOZ_API_KEY` from a masked secret over a checked-in config file.

2. **Submit an analysis and capture the job ID.**

   `analyze` prints human-readable confirmation lines, so add `--json` when you need to pipe it.

   ```bash
   # Jenkins
   JOB_ID=$(rootcoz --json analyze --source jenkins -j nightly -b 412 \
     --provider claude --model claude-opus-4-6 | jq -r .job_id)

   # JUnit XML, no CI connectivity needed
   JOB_ID=$(rootcoz --json analyze --source file -f results.xml | jq -r .job_id)

   # Prow
   JOB_ID=$(rootcoz --json analyze --source prow -j my-periodic-test -b 20250731 \
     --prow-url https://prow.example.com --gcs-bucket prow-artifacts | jq -r .job_id)
   ```

   Use `rootcoz submit` instead of `analyze` when you want the CI results stored without paying for AI, then `rootcoz results analyze JOB_ID` to run the analysis later on the same job ID. Both take the same flags — `submit` is the same command registered under a second name, and the CLI posts to the ingest-only endpoint when you invoke it under that name.

   Tag and label the run so it shows up in filtered dashboards and reports:

   ```bash
   rootcoz --json analyze --source jenkins -j nightly -b 412 \
     --tag smoke --label team-alpha --label tier-1 | jq -r .job_id
   ```

   > **Note:** `--tag` categorizes the run; `--label` attaches job metadata. They are different fields and both are repeatable.

3. **Poll until the analysis completes.**

   `rootcoz status JOB_ID` returns the job status. In-progress statuses are `pending`, `running`, and `waiting`; `completed` is the terminal success state.

   ```bash
   while true; do
     STATE=$(rootcoz --json status "$JOB_ID" | jq -r .status)
     case "$STATE" in
       completed) echo "done: $JOB_ID"; break ;;
       failed|error|aborted) echo "analysis did not complete: $JOB_ID ($STATE)"; exit 1 ;;
     esac
     sleep 15
   done
   ```

   Cap the loop in real automation so a stuck job does not spin forever:

   ```bash
   for _ in $(seq 1 80); do
     [ "$(rootcoz --json status "$JOB_ID" | jq -r .status)" = "completed" ] && break
     sleep 15
   done
   ```

   `rootcoz abort JOB_ID` cancels a run you no longer want.

4. **Fetch the result.**

   `rootcoz results show JOB_ID` prints a one-line summary by default. For anything machine-readable, add `--json`, or project a subset with `--fields`.

   ```bash
   rootcoz results show "$JOB_ID"

   # Sparse payload: cheap to poll even for a large job
   rootcoz --json results show "$JOB_ID" \
     --fields result.summary,result.failed_count,result.ai_provider \
     | jq '.result'
   ```

   `rootcoz results fields` lists every accepted path. Unknown paths are rejected with HTTP 400, so check that list before building long `--fields` strings.

5. **Paginate through test entries.**

   `rootcoz results tests` returns `entries`, `total`, and `has_more` under `--json`. Page size is capped at 200.

   ```bash
   OFFSET=0
   while :; do
     PAGE=$(rootcoz --json results tests "$JOB_ID" --status failed --limit 200 --offset "$OFFSET")
     echo "$PAGE" | jq -r '.entries[] | "\(.test_name)\t\(.duration)"'
     [ "$(echo "$PAGE" | jq -r '.has_more')" = "true" ] || break
     OFFSET=$((OFFSET + $(echo "$PAGE" | jq -r '.entries | length')))
   done
   ```

   For pipeline jobs, scope to one child with `--child-job` and `--child-build`.

6. **Review and classify.**

   ```bash
   # Look at progress first
   rootcoz results review-status "$JOB_ID"

   # Mark a failure reviewed
   rootcoz results set-reviewed "$JOB_ID" --test "TEST_NAME" --reviewed

   # Override what the AI decided
   rootcoz override-classification "$JOB_ID" --test "TEST_NAME" --classification "PRODUCT BUG"
   rootcoz override-pattern "$JOB_ID" --test "TEST_NAME" --pattern "REGRESSION"

   # Add your own classification with a reason and a ticket link
   rootcoz classify "TEST_NAME" --type REGRESSION --job-id "$JOB_ID" \
     --reason "Fails only on the new checkout path" --references "PLAT-1234"

   # Record where the follow-up work lives
   rootcoz results set-tracked-in "$JOB_ID" --test "TEST_NAME" --url "https://jira.example.com/browse/PLAT-1234"

   # Leave a note for the next reviewer
   rootcoz comments add "$JOB_ID" --test "TEST_NAME" --message "Reproduced on the staging pool"
   ```

   If the job is a pipeline parent, add `--child-job CHILD_JOB --child-build 12345` to every one of those commands so the change lands on the right failure.

7. **Push to an exporter.**

   ```bash
   rootcoz exporters                                        # which plugins are enabled
   rootcoz --json push "$JOB_ID" --plugin reportportal | jq .
   rootcoz --json push "$JOB_ID" --plugin reportportal \
     --child-job-name CHILD_JOB --child-build-number 12345
   ```

   Report Portal has a shortcut that skips `--plugin`:

   ```bash
   rootcoz --json push-reportportal "$JOB_ID"
   ```

   The response includes `pushed`, `errors`, `unmatched`, and `launch_id`, so an automation can fail loudly when `unmatched` is non-empty.

8. **Script report output.**

   Reports need `admin` or `can_view_reports`. All three subcommands take the same filters.

   ```bash
   rootcoz --json reports totals --team alpha --from 2025-01-01 --to 2025-06-30 \
     --review-status reviewed | jq '.total_jobs, .total_failures, .total_reviewed'

   rootcoz --json reports overrides --tags nightly,smoke | jq '.groups'
   rootcoz --json reports issues --team alpha | jq -r '.issues[] | "\(.issue_type)\t\(.title)"'

   # Trend lookup before you even submit anything
   rootcoz --json history failures --search tests.test_auth --classification INFRASTRUCTURE \
     --limit 50 | jq -r '.failures[].test_name'
   ```

   For cost reporting, `rootcoz admin token-usage` is the one command with a native CSV mode:

   ```bash
   rootcoz admin token-usage --period month --group-by model --format csv > usage.csv
   rootcoz --json admin token-usage --job-id "$JOB_ID" | jq '.records[].cost_usd'
   ```

9. **Compose it into one script.**

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   : "${ROOTCOZ_SERVER:?set ROOTCOZ_SERVER}"
   : "${ROOTCOZ_API_KEY:?set ROOTCOZ_API_KEY}"

   JOB_NAME="${1:?usage: analyze.sh JOB_NAME BUILD_NUMBER}"
   BUILD_NUMBER="${2:?usage: analyze.sh JOB_NAME BUILD_NUMBER}"

   JOB_ID=$(rootcoz --json analyze --source jenkins -j "$JOB_NAME" -b "$BUILD_NUMBER" \
     --provider claude --model claude-opus-4-6 --tag ci \
     | jq -r '.job_id')
   echo "queued $JOB_ID"

   for _ in $(seq 1 80); do
     STATE=$(rootcoz --json status "$JOB_ID" | jq -r '.status')
     [ "$STATE" = "completed" ] && break
     [ "$STATE" = "failed" ] && { echo "analysis failed" >&2; exit 1; }
     sleep 15
   done

   SUMMARY=$(rootcoz --json results show "$JOB_ID" \
     --fields result.summary,result.failed_count | jq -c '.result')
   echo "summary: $SUMMARY"

   rootcoz --json results tests "$JOB_ID" --status failed --limit 200 \
     | jq -r '.entries[].test_name' | while read -r TEST; do
         rootcoz results set-reviewed "$JOB_ID" --test "$TEST" --reviewed
         rootcoz results set-tracked-in "$JOB_ID" --test "$TEST" \
           --url "https://jira.example.com/browse/PLAT-$BUILD_NUMBER"
       done

   rootcoz --json push "$JOB_ID" --plugin reportportal | jq '{pushed, unmatched, errors}'
   ```

   The same shape works for a cron-driven report: swap step 2 for `reports totals` and drop the polling loop entirely.

## Advanced Usage

- Pass `--api-key` from a CI secret and `--server` from a variable to run the same script against dev, staging, and prod without editing it.
- Use `rootcoz analyze --source file -f results.xml` to analyze artifacts downloaded after the fact, which is the easiest way to backfill historical CI runs.
- `rootcoz results delete --all --confirm` deletes jobs in bulk. The `--confirm` requirement exists so an accidental flag cannot wipe the server. The command collects job IDs from a single unpaginated `/api/dashboard` request, so on a server with more than 500 jobs it only sees the first page and leaves the rest in place. For a full cleanup, delete the remainder by explicit `JOB_ID` values from `rootcoz --json results list`.
- `rootcoz re-analyze JOB_ID` re-runs a finished analysis with its original settings, which is faster than resubmitting when you only changed the model on the server side.
- `rootcoz history search --signature HASH` finds every other test that failed with the same error signature, useful for deciding whether a new failure is really a new problem.
- `rootcoz --json admin-chat send "Which jobs regressed this week?"` is the fastest way to get a cross-job answer in a pipeline log.

## Troubleshooting

- The script hangs on the first command.  
  The CLI is waiting for a prompt. `auth ai-keys set` prompts unless you pass `--stdin`, `validate-token` always prompts for `--token`, and `admin users delete` prompts unless you pass `--force`.
- `Error: No server specified.` on the first call.  
  `ROOTCOZ_SERVER` is not exported in the script's environment, and no config file exists. Set both variables, or add a `[default]` server to `~/.config/rootcoz/config.toml`.
- `Error: 401` partway through a long script.  
  The key expired or was rotated. Re-run `rootcoz auth whoami` to confirm, then issue a new one with `rootcoz auth rotate-key`.
- The poll loop never exits.  
  A `--wait` submission stays in `waiting` until the CI build finishes. Add an iteration cap, and check `rootcoz status JOB_ID` manually to see which state it is stuck in.
- `results tests` returns fewer entries than the dashboard failure count.  
  The dashboard counts analysis failures, while `results tests` pages the CI test entries. Raise `--limit` to 200 and keep incrementing `--offset` until `has_more` is `false`.
- `push` reports unmatched tests.  
  The exporter could not line the test names up. Run `rootcoz exporters` to confirm the plugin is enabled, and add `--child-job-name` and `--child-build-number` for pipeline jobs.
- A `403` mentions `can_view_reports`.  
  Your role is fine but the reports grant is missing. An admin can add it with `rootcoz admin users set-can-view-reports USERNAME true`.

## Related Pages

- [CLI Command Reference](cli-reference.html)
- [Quickstart](quickstart.html)
- [Submitting Analyses](submit-analyses.html)
- [Tracking Analysis Progress](track-analysis-progress.html)
- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [Exploring History and Reports](explore-history-and-reports.html)
