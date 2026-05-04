# CLI Command Reference

`rootcoz [GLOBAL OPTIONS] COMMAND [ARGS]...`

> **Note:** For profile fields, config file layout, and environment-backed defaults, see [Configuration Reference](configuration-reference.html).


> **Tip:** For task-focused command patterns, see [Automating Common Tasks with the CLI](automating-common-tasks-with-the-cli.html).


> **Note:** For HTTP request and response schemas, see [API Endpoint Reference](api-endpoint-reference.html).

## Global Options
These options are accepted before the command name.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--server`, `-s` | string | default profile if configured | Server profile name or full base URL. Reads `ROOTCOZ_SERVER`. |
| `--json` | boolean | `false` | Print JSON instead of text output for commands that support JSON output. |
| `--user` | string | profile value or empty | Username used for comments and review actions. Reads `ROOTCOZ_USERNAME`. |
| `--api-key` | string | profile value or empty | Bearer token used for admin-authenticated requests. Reads `ROOTCOZ_API_KEY`. |
| `--no-verify-ssl` | boolean | profile value or `false` | Disable HTTPS certificate verification. Reads `ROOTCOZ_NO_VERIFY_SSL`. |
| `--verify-ssl` | boolean | unset | Force HTTPS certificate verification on, even when the selected profile disables it. |
| `--insecure` | boolean | `false` | Alias for `--no-verify-ssl`. |
| `--show-completion` | `bash` \| `zsh` | unset | Print the shell completion script for the selected shell. |
| `--help` | boolean | `false` | Show help for the current command. |

> **Warning:** `--verify-ssl` and `--insecure` cannot be used together.


> **Note:** When `--server` is a full URL, profile values do not cascade into the command. When `--server` is omitted, `rootcoz` uses the default profile from `$XDG_CONFIG_HOME/rootcoz/config.toml` or `~/.config/rootcoz/config.toml`.

Example:

```bash
rootcoz --server dev --json results list
```

## Output Modes
| Mode | Syntax | Effect |
| --- | --- | --- |
| Text | default | Prints aligned tables, short status lines, or command-specific plain text. Table output preserves full values. |
| JSON | `rootcoz --json ...` or `rootcoz <leaf-command> --json` | Prints pretty-printed JSON with 2-space indentation for commands that support JSON output. |
| Full result JSON | `rootcoz results show JOB_ID --full` | Prints the complete stored result document. |
| Token usage CSV | `rootcoz admin token-usage --format csv` | Prints CSV instead of text. |

> **Note:** Command tables below list command-specific parameters only. Apply the global options above to any leaf command unless the command section says otherwise.

Example:

```bash
rootcoz --json health
rootcoz results list --json
rootcoz results show job-123 --full
rootcoz admin token-usage --group-by provider --format csv
```

## Analysis and Results
> **Tip:** See [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html), [Tracking Analysis Progress](tracking-analysis-progress.html), and [Finding Runs on the Dashboard](finding-runs-on-the-dashboard.html) for workflow guidance.

### `health`
Checks server availability.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz health
```

Return value/effect: Prints the current service status and any named health checks.

### `analyze`
Queues a Jenkins build for analysis.

> **Note:** Unset options can fall back to the selected server profile. Env-backed flags read the environment variable named in the description before profile fallback.


> **Warning:** `--build-number`, `--ai-cli-timeout`, `--jenkins-timeout`, `--jenkins-artifacts-max-size-mb`, `--jira-max-results`, and `--poll-interval` must be greater than `0` when set. `--max-wait` and `--max-concurrent` must be non-negative. `--peer-analysis-max-rounds` must be between `1` and `10`.

Core options

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--job-name`, `-j` | option | string | required | Jenkins job name. |
| `--build-number`, `-b` | option | integer | required | Jenkins build number to analyze. |
| `--provider` | option | string | unset | AI provider. Profile fallback applies. |
| `--model` | option | string | unset | AI model. Profile fallback applies. |
| `--jira` / `--no-jira` | option | boolean | unset | Enable or disable Jira integration for this request. |
| `--raw-prompt` | option | string | empty | Extra AI instructions appended to the request. |

Jenkins and repository options

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--jenkins-url` | option | string | unset | Jenkins base URL. Reads `JENKINS_URL`. |
| `--jenkins-user` | option | string | unset | Jenkins username. Reads `JENKINS_USER`. |
| `--jenkins-password` | option | string | unset | Jenkins password or API token. Reads `JENKINS_PASSWORD`. |
| `--jenkins-ssl-verify` / `--no-jenkins-ssl-verify` | option | boolean | unset | Override Jenkins HTTPS certificate verification. |
| `--jenkins-timeout` | option | integer | unset | Jenkins API timeout in seconds. |
| `--jenkins-artifacts-max-size-mb` | option | integer | unset | Maximum downloaded artifact size in MB. |
| `--get-job-artifacts` / `--no-get-job-artifacts` | option | boolean | unset | Download all job artifacts for analysis context. |
| `--tests-repo-url` | option | string | unset | Tests repository URL. Reads `TESTS_REPO_URL`. |
| `--tests-repo-token` | option | string | unset | Token for cloning a private tests repository. Reads `TESTS_REPO_TOKEN`. |
| `--additional-repos` | option | string | unset | Comma-separated repo specs in `name:url`, optionally with `:ref` and `@token`. |

Jira, GitHub, and peer-AI options

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--jira-url` | option | string | unset | Jira base URL. Reads `JIRA_URL`. |
| `--jira-email` | option | string | unset | Jira Cloud email. Reads `JIRA_EMAIL`. |
| `--jira-api-token` | option | string | unset | Jira Cloud API token. Reads `JIRA_API_TOKEN`. |
| `--jira-pat` | option | string | unset | Jira Server/Data Center PAT. Reads `JIRA_PAT`. |
| `--jira-project-key` | option | string | unset | Jira project key. Reads `JIRA_PROJECT_KEY`. |
| `--jira-ssl-verify` / `--no-jira-ssl-verify` | option | boolean | unset | Override Jira HTTPS certificate verification. |
| `--jira-max-results` | option | integer | unset | Maximum number of Jira search matches. |
| `--github-token` | option | string | unset | GitHub token. Reads `GITHUB_TOKEN`. |
| `--ai-cli-timeout` | option | integer | unset | AI CLI timeout in minutes. |
| `--peers` | option | string | unset | Comma-separated `provider:model` peer-analysis list. |
| `--peer-analysis-max-rounds` | option | integer | unset | Maximum peer-analysis rounds. |

Queue and concurrency options

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--wait` / `--no-wait` | option | boolean | unset | Wait or do not wait for the Jenkins build to finish before analysis. |
| `--poll-interval` | option | integer | unset | Minutes between Jenkins polls. |
| `--max-wait` | option | integer | unset | Maximum minutes to wait for completion. `0` means no time limit when the server accepts it. |
| `--force` / `--no-force` | option | boolean | unset | Analyze even when the build succeeded, or explicitly disable that behavior. |
| `--max-concurrent` | option | integer | `0` | Maximum concurrent AI CLI calls. `0` means no CLI-side override. |

Example:

```bash
rootcoz analyze \
  --job-name ocp-e2e-aws \
  --build-number 1542 \
  --provider claude \
  --model claude-sonnet-4 \
  --wait
```

Return value/effect: Queues the analysis and prints the new `job_id`, queue `status`, and result polling URL.

### `re-analyze`
Queues a new analysis using the saved settings from an earlier job.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Existing analysis job ID to re-run. |

Example:

```bash
rootcoz re-analyze job-123
```

Return value/effect: Queues a new analysis and prints the new `job_id`, `status`, and result URL.

### `status`
Shows the current state of one analysis job.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID to query. |

Example:

```bash
rootcoz status job-123
```

Return value/effect: Default output is a two-column table with `job_id` and `status`. JSON mode returns the full stored result payload.

### `results list`
Lists recent analysis jobs.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--limit`, `-l` | option | integer | `50` | Maximum number of jobs to return. |

Example:

```bash
rootcoz results list --limit 10
```

Return value/effect: Prints a table with `job_id`, `status`, `jenkins_url`, and `created_at`.

### `results dashboard`
Lists analysis jobs with dashboard metadata.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz results dashboard
```

Return value/effect: Prints a table with `job_id`, `job_name`, `build_number`, `status`, `failure_count`, `reviewed_count`, `comment_count`, and `created_at`.

### `results show`
Shows one stored analysis result.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID to display. |
| `--full`, `-f` | option | boolean | `false` | Print the complete result document as JSON. |

Example:

```bash
rootcoz results show job-123
```

Return value/effect: Default output is a summary table with `job_id`, `status`, `summary`, `failures`, `children`, `ai_provider`, and `created_at`. `--full` prints the full result JSON.

### `results delete`
Deletes one or more stored analysis jobs.

> **Warning:** `--all` requires `--confirm` and cannot be combined with explicit job IDs.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID ...` | arg | string[] | required unless `--all` | One or more analysis job IDs to delete. |
| `--all` | option | boolean | `false` | Delete every job returned by the dashboard listing. |
| `--confirm` | option | boolean | `false` | Required safety flag for `--all`. |

Example:

```bash
rootcoz results delete job-123 job-124
```

Return value/effect: Deletes the requested job IDs. Single-job runs print one confirmation line; multi-job runs print a success summary and any failures.

### `results review-status`
Shows review progress for one result set.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |

Example:

```bash
rootcoz results review-status job-123
```

Return value/effect: Prints a table with `total_failures`, `reviewed_count`, and `comment_count`.

### `results set-reviewed`
Sets or clears the reviewed state for one failure.

> **Warning:** `--child-build` must be non-negative. Values greater than `0` require `--child-job`.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |
| `--test`, `-t` | option | string | required | Test name to update. |
| `--reviewed` / `--not-reviewed` | option | boolean | required | Mark the failure reviewed or not reviewed. |
| `--child-job` | option | string | empty | Child job name for pipeline child failures. |
| `--child-build` | option | integer | `0` | Child build number for pipeline child failures. |

Example:

```bash
rootcoz results set-reviewed job-123 --test tests.TestA.test_one --reviewed
```

Return value/effect: Prints a short confirmation line, including the reviewer name when the server returns it.

### `results enrich-comments`
Refreshes stored comment links with live tracker state.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |

Example:

```bash
rootcoz results enrich-comments job-123
```

Return value/effect: Refreshes linked PR and ticket state for the job's comments and prints a completion line.

### `ai-configs`
Lists AI provider/model pairs found in completed analyses.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz ai-configs
```

Return value/effect: Prints a table with `ai_provider` and `ai_model`. If no completed analyses contributed configurations, it prints an empty-state message.

### `ai-models`
Lists currently available models by AI provider.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--provider`, `-p` | option | string | empty | Limit output to one provider. |

Example:

```bash
rootcoz ai-models --provider claude
```

Return value/effect: With `--provider`, prints the models for that provider. Without it, prints one section per provider with a model count and `id`/`name` rows.

## History and Classification
> **Tip:** See [Exploring Failure History and Test Trends](exploring-failure-history-and-test-trends.html) and [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html) for workflow guidance.

### `history test`
Shows failure history for one fully qualified test name.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `TEST_NAME` | arg | string | required | Fully qualified test name. |
| `--limit`, `-l` | option | integer | `20` | Maximum number of recent runs to return. |
| `--job-name`, `-j` | option | string | empty | Limit history to one Jenkins job name. |
| `--exclude-job-id` | option | string | empty | Exclude results from one analysis job ID. |

Example:

```bash
rootcoz history test tests.TestA.test_one --limit 10
```

Return value/effect: Prints summary lines for the test, then a `Recent runs` table and a `Comments` table when those sections are present.

### `history search`
Finds tests that share one error signature.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--signature`, `-s` | option | string | required | Error signature hash to search for. |
| `--exclude-job-id` | option | string | empty | Exclude results from one analysis job ID. |

Example:

```bash
rootcoz history search --signature 2f0c8d0c...
```

Return value/effect: Prints the signature, total occurrence count, unique test count, and a table of matching tests.

### `history stats`
Shows aggregate history statistics for one Jenkins job.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_NAME` | arg | string | required | Jenkins job name. |
| `--exclude-job-id` | option | string | empty | Exclude results from one analysis job ID. |

Example:

```bash
rootcoz history stats ocp-e2e-aws
```

Return value/effect: Prints job-level totals and, when available, a `Most common failures` table.

### `history failures`
Lists paginated failure history across jobs.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--limit`, `-l` | option | integer | `50` | Maximum rows to return. |
| `--offset`, `-o` | option | integer | `0` | Pagination offset. |
| `--search`, `-s` | option | string | empty | Test-name search string. |
| `--classification`, `-c` | option | string | empty | Classification filter. |
| `--job-name`, `-j` | option | string | empty | Jenkins job-name filter. |

Example:

```bash
rootcoz history failures --classification "PRODUCT BUG" --limit 25
```

Return value/effect: Prints a total-count line followed by a table with `test_name`, `job_name`, `classification`, and `analyzed_at`.

### `classify`
Creates a classification for one failure.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `TEST_NAME` | arg | string | required | Fully qualified test name. |
| `--type`, `-t` | option | string | required | Classification type: `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, `KNOWN_BUG`, or `INTERMITTENT`. |
| `--job-id` | option | string | required | Analysis job ID this classification belongs to. |
| `--reason`, `-r` | option | string | empty | Free-text reason. |
| `--job-name`, `-j` | option | string | empty | Jenkins job name for the failure. |
| `--references` | option | string | empty | Bug URLs or ticket keys. |
| `--child-job` | option | string | empty | Child job name for pipeline child failures. |
| `--child-build` | option | integer | `0` | Child build number for pipeline child failures. |

Example:

```bash
rootcoz classify tests.TestA.test_one --type FLAKY --job-id job-123 --reason "Intermittent DNS lookup"
```

Return value/effect: Creates a classification record and prints its server-generated ID.

### `classifications list`
Lists stored classifications.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--job-id` | option | string | empty | Filter by analysis job ID. |
| `--test-name`, `-t` | option | string | empty | Filter by test name. |
| `--type`, `-c` | option | string | empty | Filter by classification type. |
| `--job-name`, `-j` | option | string | empty | Filter by job name. |
| `--parent-job-name` | option | string | empty | Filter by parent job name. |

Example:

```bash
rootcoz classifications list --type FLAKY --job-name ocp-e2e-aws
```

Return value/effect: Prints a table with `test_name`, `classification`, `reason`, `created_by`, and `created_at`. If nothing matches, it prints an empty-state message.

### `override-classification`
Overrides one failure's top-level classification.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |
| `--test`, `-t` | option | string | required | Test name to override. |
| `--classification`, `-c` | option | string | required | Override value, such as `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE`. |
| `--child-job` | option | string | empty | Child job name for pipeline child failures. |
| `--child-build` | option | integer | `0` | Child build number for pipeline child failures. |

Example:

```bash
rootcoz override-classification job-123 --test tests.TestA.test_one --classification "PRODUCT BUG"
```

Return value/effect: Updates the stored classification and prints the final value returned by the server.

## Comments and Mentions
> **Tip:** See [Commenting and Mentioning Teammates](commenting-and-mentioning-teammates.html) for workflow guidance.

### `comments list`
Lists comments for one analysis job.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |

Example:

```bash
rootcoz comments list job-123
```

Return value/effect: Prints a table with `id`, `test_name`, `comment`, `username`, and `created_at`. If the job has no comments, it prints an empty-state message.

### `comments add`
Adds a comment to one failure.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |
| `--test`, `-t` | option | string | required | Test name to comment on. |
| `--message`, `-m` | option | string | required | Comment text. |
| `--child-job` | option | string | empty | Child job name for pipeline child failures. |
| `--child-build` | option | integer | `0` | Child build number for pipeline child failures. |

Example:

```bash
rootcoz comments add job-123 --test tests.TestA.test_one --message "Opened PROJ-456"
```

Return value/effect: Creates the comment and prints the new comment ID.

### `comments delete`
Deletes one comment.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |
| `COMMENT_ID` | arg | integer | required | Comment ID to remove. |

Example:

```bash
rootcoz comments delete job-123 42
```

Return value/effect: Deletes the comment and prints a confirmation line.

### `mentionable-users`
Lists usernames that can be mentioned in comments.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz mentionable-users
```

Return value/effect: Prints one username per line. If no users are available, it prints an empty-state message.

### `mentions`
Lists the current user's mentions.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--limit`, `-l` | option | integer | `50` | Maximum mentions to return. |
| `--offset`, `-o` | option | integer | `0` | Pagination offset. |
| `--unread` | option | boolean | `false` | Limit results to unread mentions. |

Example:

```bash
rootcoz mentions --unread
```

Return value/effect: Prints the total and unread counts, then a table with `id`, `job_id`, `test_name`, `comment`, `username`, `is_read`, and `created_at`.

### `mentions-mark-read`
Marks selected mentions as read.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--ids` | option | `csv<int>` | required | Comma-separated positive comment IDs. |

Example:

```bash
rootcoz mentions-mark-read --ids 10,11,12
```

Return value/effect: Marks the listed mentions as read and exits successfully when the server accepts the request.

### `mentions-mark-all-read`
Marks every mention for the current user as read.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz mentions-mark-all-read
```

Return value/effect: Marks all mentions as read and exits successfully when the server accepts the request.

### `analyze-comment-intent`
Runs AI intent detection on one comment string.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `COMMENT` | arg | string | required | Comment text to analyze. |
| `--job-id` | option | string | empty | Job ID used to resolve AI settings from an analyzed job. |
| `--ai-provider` | option | string | empty | Override AI provider. |
| `--ai-model` | option | string | empty | Override AI model. |

Example:

```bash
rootcoz analyze-comment-intent "Fixed in PR #42"
```

Return value/effect: Prints whether the comment suggests the failure is reviewed and, when present, the returned reason string.

## Issue and Integration Commands
> **Tip:** See [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html) for issue workflows and [Connecting GitHub, Jira, and Report Portal](connecting-github-jira-and-report-portal.html) for setup details.

### `capabilities`
Shows server-side automation feature flags.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz capabilities
```

Return value/effect: Prints a JSON object that reports whether GitHub issue creation and Jira bug creation are enabled on the server.

### `jira-projects`
Lists Jira projects available to the current credentials.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--query` | option | string | empty | Search filter applied to Jira projects. |
| `--jira-token` | option | string | unset | Jira token override. If omitted, the selected profile may supply it. |
| `--jira-email` | option | string | unset | Jira email override. If omitted, the selected profile may supply it. |

Example:

```bash
rootcoz jira-projects --query platform
```

Return value/effect: Prints a table with project `key` and `name`. If no projects match, it prints an empty-state message.

### `jira-security-levels`
Lists Jira security levels for one project.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `PROJECT_KEY` | arg | string | required | Jira project key. |
| `--jira-token` | option | string | unset | Jira token override. If omitted, the selected profile may supply it. |
| `--jira-email` | option | string | unset | Jira email override. If omitted, the selected profile may supply it. |

Example:

```bash
rootcoz jira-security-levels PROJ
```

Return value/effect: Prints a table with security level `name` and `description`. If the project exposes none, it prints an empty-state message.

### `preview-issue`
Generates issue content for GitHub or Jira without creating the issue.

> **Warning:** `--type` accepts only `github` or `jira`.


> **Note:** GitHub-only overrides are used only when `--type github` is selected. Jira-only overrides are used only when `--type jira` is selected.

Core options

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |
| `--test`, `-t` | option | string | required | Test name to preview. |
| `--type` | option | string | required | Issue target: `github` or `jira`. |
| `--child-job` | option | string | empty | Child job name for pipeline child failures. |
| `--child-build` | option | integer | `0` | Child build number for pipeline child failures. |
| `--include-links` | option | boolean | `false` | Include full URLs in the generated text. |
| `--ai-provider` | option | string | empty | Override AI provider for issue generation. |
| `--ai-model` | option | string | empty | Override AI model for issue generation. |
| `--issue-prompt` | option | string | empty | Extra AI instructions for issue generation. |

Tracker override options

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--github-token` | option | string | unset | GitHub token override. |
| `--github-repo-url` | option | string | unset | GitHub repository URL override. |
| `--jira-token` | option | string | unset | Jira token override. |
| `--jira-email` | option | string | unset | Jira email override. |
| `--jira-project-key` | option | string | unset | Jira project key override. |
| `--jira-security-level` | option | string | unset | Jira security level name override. |

Example:

```bash
rootcoz preview-issue job-123 --test tests.TestA.test_one --type github --include-links
```

Return value/effect: Prints the generated issue title and body. When the server returns related matches, it also prints a `Similar issues` section.

### `get-issue-prompt`
Shows the issue-generation prompt associated with one analysis job.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |

Example:

```bash
rootcoz get-issue-prompt job-123
```

Return value/effect: Prints the stored prompt text. If the job has no issue prompt, it prints an empty-state message.

### `create-issue`
Creates a GitHub issue or Jira issue from an analyzed failure.

> **Warning:** `--type` accepts only `github` or `jira`.

Core options

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID. |
| `--test`, `-t` | option | string | required | Test name to create the issue from. |
| `--type` | option | string | required | Issue target: `github` or `jira`. |
| `--title` | option | string | required | Final issue title. |
| `--body` | option | string | required | Final issue body. |
| `--child-job` | option | string | empty | Child job name for pipeline child failures. |
| `--child-build` | option | integer | `0` | Child build number for pipeline child failures. |

Tracker override options

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--github-token` | option | string | unset | GitHub token override. |
| `--github-repo-url` | option | string | unset | GitHub repository URL override. |
| `--jira-token` | option | string | unset | Jira token override. |
| `--jira-email` | option | string | unset | Jira email override. |
| `--jira-project-key` | option | string | unset | Jira project key override. |
| `--jira-security-level` | option | string | unset | Jira security level name override. |
| `--jira-issue-type` | option | string | `Bug` | Jira issue type name, such as `Bug`, `Story`, or `Task`. |

Example:

```bash
rootcoz create-issue \
  job-123 \
  --test tests.TestA.test_one \
  --type jira \
  --title "DNS timeout in tests.TestA.test_one" \
  --body "Observed in build 1542." \
  --jira-issue-type Bug
```

Return value/effect: Creates the issue, then prints the created key or number, the issue URL, and an added comment ID when the server returns one.

### `validate-token`
Validates a GitHub or Jira token.

> **Warning:** Invalid tokens exit with status code `1`, including JSON mode.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `TOKEN_TYPE` | arg | string | required | Token target: `github` or `jira`. |
| `--token` | option | string | prompted | Token value to validate. Input is hidden when prompted. |
| `--email` | option | string | empty | Jira Cloud email to send with Jira token validation. |

Example:

```bash
rootcoz validate-token github --token "$GITHUB_TOKEN"
```

Return value/effect: Prints a valid or invalid status line. In JSON mode, it prints the validation response object.

### `push-reportportal`
Pushes stored classifications into Report Portal.

> **Note:** Alias: `push-rp`.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_ID` | arg | string | required | Analysis job ID to push. |
| `--child-job-name` | option | string | unset | Child job name for pipeline child pushes. |
| `--child-build-number` | option | integer | unset | Child build number for pipeline child pushes. |

Example:

```bash
rootcoz push-reportportal job-123 --child-job-name my-child --child-build-number 42
```

Return value/effect: Prints the number of pushed classifications, the launch ID when returned, any unmatched tests, and any error count/details.

## Authentication and Admin Commands
> **Tip:** See [Managing Users and API Keys](managing-users-and-api-keys.html) and [Updating Your Profile and Notifications](updating-your-profile-and-notifications.html) for workflow guidance.

### `auth login`
Validates admin credentials.

> **Note:** This command does not persist a login session across later CLI invocations. Persistent admin access is normally provided by the global `--api-key`, `ROOTCOZ_API_KEY`, or a profile `api_key`.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--username`, `-u` | option | string | required | Admin username to validate. |
| `--api-key`, `-k` | option | string | required | Admin API key to validate. |

Example:

```bash
rootcoz auth login --username admin --api-key "$ROOTCOZ_ADMIN_KEY"
```

Return value/effect: Prints the returned username, role, and admin flag.

### `auth logout`
Sends the logout request.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz auth logout
```

Return value/effect: Sends the logout request and exits successfully when the server accepts it.

### `auth whoami`
Shows the current authenticated user record.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz auth whoami
```

Return value/effect: Prints a table with `username`, `role`, and `is_admin`.

### `admin users list`
Lists known users.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz admin users list
```

Return value/effect: Prints a table with `username`, `role`, `created_at`, and `last_seen`.

### `admin users create`
Creates a new admin account.

> **Warning:** The generated API key is shown once. Save it before the command ends.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `USERNAME` | arg | string | required | Username for the new admin account. |

Example:

```bash
rootcoz admin users create release-admin
```

Return value/effect: Creates the account and prints the returned API key.

### `admin users delete`
Removes an admin account by username.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `USERNAME` | arg | string | required | Admin username to remove. |
| `--force`, `-f` | option | boolean | `false` | Skip the confirmation prompt. |

Example:

```bash
rootcoz admin users delete release-admin --force
```

Return value/effect: Deletes the named account. Without `--force`, the CLI asks for confirmation first.

### `admin users rotate-key`
Rotates an admin user's API key.

> **Warning:** The new API key is shown once. Save it before the command ends.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `USERNAME` | arg | string | required | Admin username whose key should be rotated. |

Example:

```bash
rootcoz admin users rotate-key release-admin
```

Return value/effect: Rotates the key and prints the newly issued API key.

### `admin users change-role`
Changes a user's role.

> **Warning:** Promoting a user to `admin` can return a new API key. Save it before the command ends.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `USERNAME` | arg | string | required | Username whose role should change. |
| `ROLE` | arg | string | required | New role: `admin` or `user`. |

Example:

```bash
rootcoz admin users change-role alice admin
```

Return value/effect: Updates the user's role and prints the returned role. When the server returns a new admin key, the CLI prints it.

### `admin token-usage`
Shows AI token usage and cost data.

Mode selection

| Trigger | Effect |
| --- | --- |
| no filters and no `--job-id` | Summary dashboard for `today`, `this_week`, and `this_month`, plus top models |
| `--job-id` | Per-call records for one analysis job |
| any `--period`, `--start-date`, `--end-date`, `--provider`, `--model`, `--call-type`, or `--group-by` | Aggregated totals, plus a breakdown when grouping is present |

> **Note:** `--json` overrides `--format`.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--period` | option | string | unset | Preset date range: `today`, `week`, `month`, or `all`. |
| `--start-date` | option | string | unset | Start date in `YYYY-MM-DD` format. |
| `--end-date` | option | string | unset | End date in `YYYY-MM-DD` format. |
| `--provider` | option | string | unset | Filter by AI provider. |
| `--model` | option | string | unset | Filter by AI model. |
| `--call-type` | option | string | unset | Filter by call type. |
| `--group-by` | option | string | unset | Group by `provider`, `model`, `call_type`, `day`, `week`, `month`, or `job`. |
| `--job-id` | option | string | unset | Show per-call usage for one analysis job ID. |
| `--format` | option | string | `table` | Output format: `table`, `json`, or `csv`. |

Example:

```bash
rootcoz admin token-usage --group-by provider --format csv
```

Return value/effect: In text mode, prints either a dashboard summary, aggregated totals, or per-job call records. CSV mode prints CSV rows. JSON mode prints the raw response object.

## Metadata Commands
### `metadata list`
Lists stored job metadata.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--team` | option | string | empty | Filter by team. |
| `--tier` | option | string | empty | Filter by tier. |
| `--version` | option | string | empty | Filter by version. |
| `--label`, `-l` | option | string[] | empty | Repeatable label filter. |

Example:

```bash
rootcoz metadata list --team platform --label nightly
```

Return value/effect: Prints a table with `job_name`, `team`, `tier`, `version`, and `labels`.

### `metadata get`
Shows metadata for one job.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_NAME` | arg | string | required | Job name to fetch. |

Example:

```bash
rootcoz metadata get ocp-e2e-aws
```

Return value/effect: Prints one row with `job_name`, `team`, `tier`, `version`, and `labels`.

### `metadata set`
Creates or updates metadata for one job.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_NAME` | arg | string | required | Job name to update. |
| `--team` | option | string | empty | Team value to store. |
| `--tier` | option | string | empty | Tier value to store. |
| `--version` | option | string | empty | Version value to store. |
| `--label`, `-l` | option | string[] | empty | Repeatable labels to store. |

Example:

```bash
rootcoz metadata set ocp-e2e-aws --team platform --tier critical --label nightly
```

Return value/effect: Updates the job metadata and prints a confirmation line naming the job.

### `metadata delete`
Deletes metadata for one job.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_NAME` | arg | string | required | Job name whose metadata should be deleted. |

Example:

```bash
rootcoz metadata delete ocp-e2e-aws
```

Return value/effect: Deletes the metadata record and prints a confirmation line naming the job.

### `metadata import`
Bulk imports job metadata from a local file.

> **Warning:** `.yaml` and `.yml` files are parsed as YAML. Any other extension is parsed as JSON. The file must contain an array of metadata objects.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `FILE_PATH` | arg | string | required | Path to a JSON or YAML file containing metadata items. |

Example:

```bash
rootcoz metadata import ./metadata.yaml
```

Return value/effect: Reads the local file, uploads the parsed items, and prints the number of updated metadata entries.

### `metadata rules`
Lists configured metadata auto-assignment rules.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz metadata rules
```

Return value/effect: Prints the rules file path when present, then each rule's pattern and assigned fields. If no rules are configured, it prints an empty-state message.

### `metadata preview`
Shows what metadata rules would assign to one job name.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `JOB_NAME` | arg | string | required | Job name to test against the configured rules. |

Example:

```bash
rootcoz metadata preview test-smoke
```

Return value/effect: Prints the matched metadata fields for that job name, or an empty-state message when no rule matches.

## Configuration Commands
> **Note:** `rootcoz config` runs `rootcoz config show` when no subcommand is given.
>


> **Note:** Configuration commands do not require a server connection.
>


> **Tip:** See [Configuration Reference](configuration-reference.html) for field-by-field config details.

### `config show`
Shows the current config file summary.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz config show
```

Return value/effect: Prints the config file path, default server name, and configured servers. If no config file exists, it prints the expected path and a starter file snippet.

### `config completion`
Prints shell-completion setup instructions.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `SHELL` | arg | `bash` \| `zsh` | `zsh` | Target shell. |

Example:

```bash
rootcoz config completion bash
```

Return value/effect: Prints the shell RC snippet that calls `rootcoz --show-completion SHELL`. Unsupported shell names exit with an error.

### `config servers`
Lists configured server profiles.

| Name | Kind | Type | Default | Description |
| --- | --- | --- | --- | --- |
| none | — | — | — | No command-specific parameters. |

Example:

```bash
rootcoz config servers
```

Return value/effect: Prints a table with `name`, `url`, `username`, `no_verify_ssl`, and a default-server marker. JSON mode returns an object keyed by server name.

## Related Pages

- [Automating Common Tasks with the CLI](automating-common-tasks-with-the-cli.html)
- [Configuration Reference](configuration-reference.html)
- [API Endpoint Reference](api-endpoint-reference.html)
- [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html)
- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)