# CLI Command Reference

Every RootCoz web action has a `rootcoz` CLI counterpart. This page is the lookup table: what each command group does, the exact usage line, and the flags that actually exist. For task-oriented walkthroughs instead of a flag list, see [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html).

## Prerequisites

- The `rootcoz` CLI on your `PATH`. It is installed with the project, and the web UI's Docker image already includes it.
- A reachable RootCoz server, either from `--server`/`ROOTCOZ_SERVER` or from `~/.config/rootcoz/config.toml`.
- An API key from `rootcoz register`, an admin key, or a key issued by an administrator.

## Quick Example

```bash
rootcoz --json results dashboard --limit 5
```

That single command resolves the server, authenticates, and returns machine-readable JSON you can pipe into `jq`.

## Step-by-Step

1. **Set up the global options.** These go before the command group and apply to everything that follows.

| Option | Env var | Purpose |
| --- | --- | --- |
| `--server`, `-s` | `ROOTCOZ_SERVER` | Config server name or full URL. |
| `--json` | — | Emit JSON instead of tables. |
| `--user` | `ROOTCOZ_USERNAME` | Username shown in comments and reviews. |
| `--api-key` | `ROOTCOZ_API_KEY` | Bearer token for a user or admin account. |
| `--no-verify-ssl` | `ROOTCOZ_NO_VERIFY_SSL` | Skip TLS certificate verification. |
| `--verify-ssl` | — | Force verification on, overriding the config profile. |
| `--insecure` | — | Alias for `--no-verify-ssl`. |
| `--install-completion` | — | Install shell completion. |
| `--show-completion` | — | Print completion script to stdout. |

> **Note:** `--json` is also accepted after the subcommand, so `rootcoz --json results list` and `rootcoz results list --json` are equivalent. `--verify-ssl` and `--insecure` together are an error.

2. **Check server state.**

| Command | Purpose |
| --- | --- |
| `rootcoz register USERNAME` | Register a user and print the new API key once. |
| `rootcoz health` | Server health, per-check status, and recent error rate. |
| `rootcoz version` | Server version string. |

3. **Manage authentication and AI keys.**

| Command | Purpose |
| --- | --- |
| `rootcoz auth login --username NAME --api-key KEY` | Validate credentials and print the resolved role. Does not persist anything. |
| `rootcoz auth whoami` | Current username, role, admin flag, and report access. |
| `rootcoz auth rotate-key` | Issue a new API key for yourself, shown once. |
| `rootcoz auth logout` | Clear the admin session. |
| `rootcoz auth ai-keys list` | Show which providers already have a key for your account. |
| `rootcoz auth ai-keys set PROVIDER --model MODEL [--stdin]` | Store a provider key, verified against `MODEL`. |
| `rootcoz auth ai-keys delete PROVIDER` | Remove your stored key for one provider. |

Roles reported by `auth whoami` are `viewer`, `reviewer`, `operator`, and `admin`.

4. **Manage the local config file.** These read `~/.config/rootcoz/config.toml` (or `$XDG_CONFIG_HOME/rootcoz/config.toml`) and do not need a running server, except `config defaults`.

| Command | Purpose |
| --- | --- |
| `rootcoz config` or `rootcoz config show` | Config path, default server, and each server's URL, user, and SSL mode. |
| `rootcoz config servers` | List servers; supports `--json`. |
| `rootcoz config completion [bash\|zsh]` | Print the rc-file snippet that wires up completion. |
| `rootcoz config defaults` | Server-side analysis defaults (non-sensitive). Requires a server. |

Config keys are grouped as `[default] server = "NAME"`, `[servers.NAME]` entries, and a shared `[defaults]` table merged into every server. Per-server keys include `url`, `username`, `api_key`, `no_verify_ssl`, `ai_provider`, `ai_model`, `jenkins_*`, `jira_*`, `github_token`, `peers`, `additional_repos`, `labels`, `prow_url`, and `gcs_bucket`. See [Configuration Reference](configuration-reference.html) for the full list.

5. **Submit analyses.**

`rootcoz analyze [OPTIONS]` submits a job and runs the full pipeline. There is no `rootcoz submit` command and no ingest-only flag: ingest-without-analysis (`POST /submit`) is available through the API only.

| Option | Purpose |
| --- | --- |
| `--source` | `jenkins` (default), `file`, or `prow`. |
| `--job-name`, `-j` | Job name. Required for `jenkins` and `prow`. |
| `--build-number`, `-b` | Build number, or Prow build ID as a numeric string. Required for `jenkins` and `prow`. |
| `--file`, `-f` | Path to a JUnit XML file. Required for `file`. |
| `--prow-url`, `--gcs-bucket`, `--gcs-prefix` | Prow Deck URL, artifact bucket, and object prefix. `PROW_URL` and `GCS_BUCKET` also come from the environment. |
| `--name`, `-n` | Display name on the dashboard. |
| `--provider`, `--model` | Exact provider ID and model to use. |
| `--jira` / `--no-jira` | Enable or disable Jira integration for this run. |
| `--jenkins-url`, `--jenkins-user`, `--jenkins-password` | Jenkins credentials. `JENKINS_URL`, `JENKINS_USER`, `JENKINS_PASSWORD`. |
| `--jenkins-ssl-verify` / `--no-jenkins-ssl-verify`, `--jenkins-timeout`, `--jenkins-artifacts-max-size-mb` | Jenkins transport tuning. |
| `--get-job-artifacts` / `--no-get-job-artifacts` | Download build artifacts for AI context. |
| `--tests-repo-url`, `--tests-repo-token` | Tests repository and its token. `TESTS_REPO_URL`, `TESTS_REPO_TOKEN`. |
| `--jira-url`, `--jira-email`, `--jira-api-token`, `--jira-pat`, `--jira-project-key` | Jira connection. Each has a matching `JIRA_*` env var. |
| `--jira-ssl-verify` / `--no-jira-ssl-verify`, `--jira-max-results` | Jira search tuning. |
| `--github-token` | GitHub API token. `GITHUB_TOKEN`. |
| `--ai-call-timeout` | AI timeout in minutes. |
| `--force-server-credentials` / `--no-force-server-credentials` | Use server-managed AI credentials instead of your own key. |
| `--raw-prompt` | Extra instructions appended to the AI prompt. |
| `--issue-prompt` | Custom issue-generation prompt, overriding `.rootcoz/ROOTCOZ_ISSUE_PROMPT.md`. |
| `--peers` | Peer models as `"provider:model,provider:model"`. |
| `--peer-analysis-max-rounds` | Debate rounds, 1-10. Server default is 3. |
| `--additional-repos` | Extra context repos as `"name:url,name:url"`. |
| `--wait` / `--no-wait`, `--poll-interval`, `--max-wait` | Wait for a still-running Jenkins build. |
| `--max-concurrent` | Cap concurrent AI calls. `0` keeps the config or server default. |
| `--passed-tests`, `--skipped-tests` | JSON arrays of extra test entries. |
| `--tag` | Categorization tag. Repeatable. |
| `--label`, `-l` | Job metadata label merged on analyze. Repeatable, and distinct from `--tag`. |

Related lifecycle commands:

| Command | Purpose |
| --- | --- |
| `rootcoz results analyze JOB_ID` | Run AI on a job that was only ingested. |
| `rootcoz re-analyze JOB_ID` | Re-run a finished analysis with its original settings. |
| `rootcoz status JOB_ID` | Job status; also names the origin job for a re-analysis. |
| `rootcoz abort JOB_ID` | Abort a running or waiting analysis. |

6. **Read and review results.** `rootcoz results ...`

| Command | Flags | Purpose |
| --- | --- | --- |
| `list` | `--limit`/`-l` (50), `--analysis-state` | Recent analyzed jobs. |
| `dashboard` | `--label`/`-l` (repeatable), `--exclude-tag` (repeatable), `--search`/`-s`, `--review-status` (`all`, `reviewed`, `not_reviewed`), `--analysis-state`, `--limit` (500) | Jobs with failure counts and review progress. |
| `show JOB_ID` | `--full`/`-f`, `--fields` | Summary by default, full JSON with `--full`, or a sparse projection with `--fields`. |
| `fields` | — | Print the allowlist of field paths accepted by `--fields`. |
| `tests JOB_ID` | `--status`/`-s` (`passed`, `skipped`, `failed`, repeatable), `--child-job`, `--child-build`, `--offset` (0), `--limit` (50, max 200) | Paginated test entries with `total` and `has_more`. |
| `review-status JOB_ID` | — | Failure, reviewed, and comment counts. |
| `set-reviewed JOB_ID` | `--test`/`-t`, `--reviewed`/`--not-reviewed`, `--child-job`, `--child-build` | Mark one failure reviewed. |
| `set-tracked-in JOB_ID` | `--test`/`-t`, `--url`/`-u`, `--type` (`jira`, `github`, or auto), `--child-job`, `--child-build` | Link a failure to a ticket. |
| `delete-tracked-in JOB_ID` | `--link-id` | Remove one tracked-in link. |
| `enrich-comments JOB_ID` | — | Refresh live PR and ticket status on comments. |
| `delete [JOB_ID ...]` | `--all`, `--confirm` | Delete jobs and related data. `--all` requires `--confirm`. |

Sparse field paths include `result.summary`, `result.failed_count`, `result.failures.test_name`, `result.failures.classification`, and `result.failures.details`. Unknown paths are rejected with HTTP 400, so run `rootcoz results fields` first.

> **Note:** Pass `--child-job` before `--child-build`; most commands reject a build number without a job name. `results set-tracked-in` is the exception — it does not enforce the pairing and silently drops `child_build_number` when `--child-job` is absent, so always pass both.

7. **Query failure history.** `rootcoz history ...`

| Command | Flags | Purpose |
| --- | --- | --- |
| `test TEST_NAME` | `--limit`/`-l` (20), `--job-name`/`-j`, `--exclude-job-id` | Failure rate, last classification, recent runs, and comments for one test. |
| `search` | `--signature`/`-s` (required), `--exclude-job-id` | Other tests that failed with the same error signature. |
| `stats JOB_NAME` | `--exclude-job-id` | Aggregate failure rate and most common failures. |
| `failures` | `--limit`/`-l` (50), `--offset`/`-o` (0), `--search`/`-s`, `--classification`/`-c`, `--job-name`/`-j` | Paginated failure list. |

8. **Classify failures and discuss them.**

| Command | Flags | Purpose |
| --- | --- | --- |
| `rootcoz classify TEST_NAME` | `--type`/`-t` (required: `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, `KNOWN_BUG`, `INTERMITTENT`), `--job-id` (required), `--reason`/`-r`, `--job-name`/`-j`, `--references`, `--child-job`, `--child-build` | Record a classification. |
| `rootcoz classifications list` | `--job-id`, `--test-name`/`-t`, `--type`/`-c`, `--job-name`/`-j`, `--parent-job-name` | Existing classifications. |
| `rootcoz comments list JOB_ID` | — | Comments on a job. |
| `rootcoz comments add JOB_ID` | `--test`/`-t`, `--message`/`-m`, `--child-job`, `--child-build` | Add a comment. |
| `rootcoz comments delete JOB_ID COMMENT_ID` | — | Remove a comment. |
| `rootcoz override-classification JOB_ID` | `--test`/`-t`, `--classification`/`-c` (`CODE ISSUE`, `PRODUCT BUG`, `INFRASTRUCTURE`), `--child-job`, `--child-build` | Correct the classification. |
| `rootcoz override-pattern JOB_ID` | `--test`/`-t`, `--pattern`/`-p` (`NEW`, `REGRESSION`, `FLAKY`, `INTERMITTENT`, `KNOWN_BUG`, `PERSISTENT`), `--child-job`, `--child-build` | Correct the failure pattern. |
| `rootcoz analyze-comment-intent COMMENT` | `--job-id`, `--ai-provider`, `--ai-model` | Ask AI whether a comment implies the failure was reviewed. |
| `rootcoz mentionable-users` | — | Usernames valid for `@mentions`. |
| `rootcoz mentions` | `--limit`/`-l` (50), `--offset`/`-o`, `--unread` | Your mentions across all reports. |
| `rootcoz mentions-mark-read` | `--ids` (comma-separated) | Mark specific mentions read. |
| `rootcoz mentions-mark-all-read` | — | Mark everything read. |

9. **Look up and re-analyze single failures.** `rootcoz failure ...`

| Command | Purpose |
| --- | --- |
| `failure show FAILURE_UUID` | Job, test, error, classification, and details for one failure. |
| `failure re-analyze FAILURE_UUID` | Re-run analysis for just that failure. |

10. **Manage job metadata.** `rootcoz metadata ...`

| Command | Flags | Purpose |
| --- | --- | --- |
| `list` | `--team`, `--tier`, `--version`, `--label`/`-l` (repeatable) | Filtered metadata rows. |
| `get JOB_NAME` | — | Metadata for one job. |
| `set JOB_NAME` | `--team`, `--tier`, `--version`, `--label`/`-l` (repeatable) | Create or update. |
| `delete JOB_NAME` | — | Remove metadata. |
| `import FILE` | — | Bulk load a JSON or YAML array of `{job_name, team, tier, version, labels}`. |
| `rules` | — | Auto-assignment rules and their source file. |
| `preview JOB_NAME` | — | What the rules would assign to a job name. |

11. **Create follow-up issues.**

| Command | Flags | Purpose |
| --- | --- | --- |
| `rootcoz capabilities` | — | Whether the server can create GitHub issues and Jira bugs. |
| `rootcoz jira-projects` | `--query`, `--jira-token`, `--jira-email` | Available Jira projects. |
| `rootcoz jira-security-levels PROJECT_KEY` | `--jira-token`, `--jira-email` | Security levels for a project. |
| `rootcoz preview-issue JOB_ID` | `--test`/`-t`, `--type` (`github` or `jira`), `--child-job`, `--child-build`, `--include-links`, `--ai-provider`, `--ai-model`, `--github-token`, `--github-repo-url`, `--jira-token`, `--jira-email`, `--jira-project-key`, `--jira-security-level`, `--issue-prompt` | Render issue title, body, and similar issues without creating anything. |
| `rootcoz get-issue-prompt JOB_ID` | — | The issue prompt attached to the job's tests repo. |
| `rootcoz create-issue JOB_ID` | `--test`/`-t`, `--type`, `--title`, `--body`, `--child-job`, `--child-build`, `--github-token`, `--github-repo-url`, `--jira-token`, `--jira-email`, `--jira-project-key`, `--jira-security-level`, `--jira-issue-type` (default `Bug`) | Create the issue. |
| `rootcoz validate-token github\|jira` | `--token` (hidden prompt), `--email` | Check a tracker token before using it. |
| `rootcoz ai-models` | `--provider`/`-p` | Model IDs per provider, with credential sources. |

12. **Push to exporters.**

| Command | Flags | Purpose |
| --- | --- | --- |
| `rootcoz exporters` | — | Available exporter plugins and their enabled state. |
| `rootcoz push JOB_ID` | `--plugin`/`-p` (required), `--child-job-name`, `--child-build-number` | Push results through a named exporter plugin. |
| `rootcoz push-reportportal JOB_ID` | `--child-job-name`, `--child-build-number` | Report Portal shortcut; `push-rp` is a hidden alias. |

13. **Chat with the analysis.**

| Command | Flags | Purpose |
| --- | --- | --- |
| `rootcoz chat init JOB_ID` | `--provider`/`-p`, `--model`/`-m` (both required), `--server-credentials` | Start a job-scoped chat and clone its repos. |
| `rootcoz chat send JOB_ID MESSAGE` | `--provider`/`-p`, `--model`/`-m`, `--server-credentials` | Send a message and wait up to about two minutes for the reply. |
| `rootcoz chat history JOB_ID` | `--limit`/`-l` (200) | Full transcript. |
| `rootcoz chat abort JOB_ID` | — | Stop the message being processed. |
| `rootcoz chat clear JOB_ID` | — | Delete job chat history. |
| `rootcoz chat close JOB_ID` | — | Signal you left the chat view. |
| `rootcoz admin-chat init` | `--provider`/`-p`, `--model`/`-m` (both required), `--server-credentials` | Start a server-wide admin chat. |
| `rootcoz admin-chat send MESSAGE` | `--provider`/`-p`, `--model`/`-m`, `--server-credentials` | Ask a cross-job question. |
| `rootcoz admin-chat history` | `--limit`/`-l` (200) | Admin transcript. |
| `rootcoz admin-chat clear` | — | Clear admin history and saved artifacts. |
| `rootcoz admin-chat save-artifact HTML_FILE` | `--filename`/`-f` | Upload a local HTML report. |
| `rootcoz admin-chat download-artifact ARTIFACT_ID` | `--output`/`-o` | Download an artifact; defaults to `report-<id>.html`. |

See [Use Server Chat for Cross-Job Analysis](use-server-chat-for-cross-job-analysis.html) for prompt patterns and artifact behaviour.

14. **Run analytics reports.** `rootcoz reports ...` — requires `admin` or `can_view_reports`.

`totals`, `overrides`, and `issues` take the same filter set:

| Option | Purpose |
| --- | --- |
| `--team`, `--tier`, `--version` | Metadata filters. |
| `--from`, `--to` | Date window as `YYYY-MM-DD`. |
| `--status` | Job status. |
| `--tags` | Comma-separated include list. |
| `--exclude-tags` | Comma-separated exclude list. |
| `--review-status` | `reviewed` or `not_reviewed`. |

15. **Administer the server.** `rootcoz admin ...`

| Command | Flags | Purpose |
| --- | --- | --- |
| `component-versions` | — | Installed AI-sidecar component versions. |
| `token-usage` | `--period` (`today`, `week`, `month`, `all`), `--start-date`, `--end-date`, `--provider`, `--model`, `--call-type`, `--group-by` (`provider`, `model`, `call_type`, `day`, `week`, `month`, `job`), `--job-id`, `--format` (`table`, `json`, `csv`) | AI token usage and cost. No filters prints the summary dashboard. |
| `users list` | — | All users, roles, and grants. |
| `users create USERNAME` | `--role` (required), `--can-view-reports`/`--no-can-view-reports`, `--can-use-server-providers`/`--no-can-use-server-providers` | Create a user; the API key is shown once. |
| `users delete USERNAME` | `--force`/`-f` | Delete a user without the confirmation prompt. |
| `users rotate-key USERNAME` | — | Issue a new key for a user. |
| `users change-role USERNAME ROLE` | — | Change role. Promoting to `admin` issues a key. |
| `users set-can-view-reports USERNAME VALUE` | — | Grant or revoke report access. `VALUE` accepts `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`. |
| `users set-can-use-server-providers USERNAME VALUE` | — | Grant or revoke server AI credentials. |
| `users pending` | — | Registrations awaiting approval. |
| `users approve USERNAME` | `--can-use-server-providers`/`--no-can-use-server-providers` | Approve a registration. |
| `users reject USERNAME` | — | Reject a registration. |
| `settings list` | `--category`/`-c`, `--reveal` | Current values, sources, and categories. Values are masked unless `--reveal`. |
| `settings set KEY VALUE` | — | Persist a server setting. |
| `settings history` | `--key`/`-k`, `--limit`/`-l` (50) | Change log. |
| `settings reset KEY` | — | Drop the DB override and fall back to env or default. |
| `models refresh` | — | Re-read the model list from the sidecar. |

See [Managing Users and Server Settings](manage-users-and-server-settings.html) for what these settings mean.

## Advanced Usage

- `rootcoz results show JOB_ID --fields "result.summary,result.failures.test_name,result.failures.classification"` returns only what a report needs, which keeps large results cheap to poll.
- `rootcoz admin token-usage --group-by model --format csv > usage.csv` is the only command with a first-class CSV mode; everything else uses `--json`.
- `rootcoz --server staging` switches servers without editing any file, and `rootcoz config servers` shows which name maps to which URL.
- `rootcoz analyze --source file -f results.xml` works with no Jenkins or Prow access at all.
- `rootcoz auth ai-keys set claude --model <MODEL_ID> --stdin` reads the key from a pipe, so it never lands in shell history or a CI log.

## Troubleshooting

- `Error: No server specified.`  
  Pass `--server NAME` or `URL`, set `ROOTCOZ_SERVER`, or add a `[default]` server to `~/.config/rootcoz/config.toml`.
- `Error: Server 'name' not found in config.`  
  The `--server` value was treated as a config name and no `[servers.name]` entry exists. Run `rootcoz config servers`, or pass a full `http://` or `https://` URL to bypass the config lookup.
- `Error: 401` or a hint to use `--api-key`.  
  No credentials reached the server. Set `ROOTCOZ_API_KEY`, add `api_key` to the server entry, or pass `--api-key`.
- `Error: 403` and a hint about the allow list or a higher role.  
  Your account is not approved, or your role is too low for that action. An admin can fix this with `rootcoz admin users approve USERNAME` or `rootcoz admin users change-role USERNAME operator`.
- `rootcoz config show` prints `No config file found`.  
  That is normal on a fresh machine. `rootcoz config show` prints the exact `mkdir` and heredoc needed to create one.
- `rootcoz results show JOB_ID --fields ...` returns HTTP 400.  
  The path is not on the allowlist. Run `rootcoz results fields` and copy the path exactly.
- `--child-build requires --child-job`.  
  Pass both flags together; there is no way to target a child build without naming its job.
- A `push` reports every test as unmatched.  
  The exporter could not match test names. Confirm the plugin is enabled with `rootcoz exporters`, and check the child scoping flags if the job is a pipeline parent.

## Related Pages

- [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html)
- [Quickstart](quickstart.html)
- [Configuration Reference](configuration-reference.html)
- [API Endpoint Reference](api-reference.html)
- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Exploring History and Reports](explore-history-and-reports.html)
