# CLI Command Reference

All commands below also accept the global options in this page. For request and response bodies, see [REST API Reference](rest-api-reference.html).

## Command groups
| Group | Commands |
| --- | --- |
| Top level | `health`, `analyze`, `re-analyze`, `status`, `classify`, `capabilities`, `jira-projects`, `jira-security-levels`, `ai-configs`, `mentionable-users`, `mentions`, `mentions-mark-read`, `mentions-mark-all-read`, `preview-issue`, `create-issue`, `validate-token`, `push-reportportal`, `override-classification` |
| `results` | `list`, `dashboard`, `show`, `delete`, `review-status`, `set-reviewed`, `enrich-comments` |
| `history` | `test`, `search`, `stats`, `failures` |
| `comments` | `list`, `add`, `delete` |
| `classifications` | `list` |
| `metadata` | `list`, `get`, `set`, `delete`, `import`, `rules`, `preview` |
| `auth` | `login`, `logout`, `whoami` |
| `admin users` | `list`, `create`, `delete`, `rotate-key`, `change-role` |
| `admin` | `token-usage` |
| `config` | `show`, `servers`, `completion` |

## Global options
### `jji [GLOBAL OPTIONS] COMMAND`
Applies connection, authentication, SSL, and JSON behavior to the invoked command.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--server`, `-s` | string | profile default or required | Server profile name or full server URL. Env: `JJI_SERVER`. |
| `--json` | boolean | `false` | Pretty-print JSON instead of text output. |
| `--user` | string | profile `username` or empty | Username used for comments and review actions. Env: `JJI_USERNAME`. |
| `--api-key` | string | profile `api_key` or empty | Bearer token for admin-authenticated commands. Env: `JJI_API_KEY`. |
| `--no-verify-ssl` | boolean | profile `no_verify_ssl` or `false` | Disable TLS certificate verification. Env: `JJI_NO_VERIFY_SSL`. |
| `--verify-ssl` | boolean | unset | Force TLS verification on, overriding profile SSL settings. |
| `--insecure` | boolean | `false` | Alias for `--no-verify-ssl`. |

```bash
jji --server prod --api-key "$JJI_API_KEY" results dashboard
```

**Return value/effect:** Applies the selected server, auth, SSL, and output mode to the command that follows.

> **Note:** `--json` works both before the command (`jji --json health`) and after the leaf command (`jji health --json`).
>


> **Note:** If `--server` is a full `http://` or `https://` URL, profile defaults are not loaded for that invocation.
>


> **Warning:** `--verify-ssl` and `--insecure` cannot be used together.

## Output modes
### `--json`
Prints the full JSON payload for the selected command.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--json` | boolean | `false` | Switch from table/plain-text output to pretty-printed JSON. |

```bash
jji results show job-123 --json
```

**Return value/effect:** Returns the raw command payload as formatted JSON.

### `jji admin token-usage --format csv`
Enables CSV output for token usage reporting.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--format` | string | `table` | Output format for `admin token-usage`: `table`, `json`, or `csv`. |

```bash
jji admin token-usage --group-by provider --format csv
```

**Return value/effect:** Prints CSV headers and rows for token-usage data.

> **Note:** `--format csv` is only supported by `jji admin token-usage`.
>


> **Note:** Global `--json` overrides `--format`.

## Profile-driven usage
### Config file layout
Profiles are loaded from `config.toml`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `CONFIG_FILE` | path | `$XDG_CONFIG_HOME/jji/config.toml` or `~/.config/jji/config.toml` | CLI profile file location. |
| `[default].server` | string | unset | Default profile name used when `--server` is omitted. |
| `[defaults]` | table | unset | Values merged into every named profile. |
| `[servers.<name>]` | table | required per profile | Named server profile. |
| `XDG_CONFIG_HOME` | environment variable | unset | Overrides the base config directory. |

```toml
[default]
server = "dev"

[defaults]
jenkins_url = "https://jenkins.example.com"
ai_provider = "claude"
ai_model = "opus-4"

[servers.dev]
url = "http://localhost:8000"
username = "alice"

[servers.prod]
url = "https://jji.example.com"
username = "alice"
api_key = "prod-api-key"
```

**Return value/effect:** Commands can use a profile name with `--server`, or use the default profile automatically.

> **Note:** CLI flags and env vars override profile values.
>


> **Tip:** See [Configuration and Environment Reference](configuration-and-environment-reference.html) for the full configuration surface.

### Profile keys: connection and auth
These keys are read directly by the CLI.

| Key | Type | Default | Used by | Description |
| --- | --- | --- | --- | --- |
| `url` | string | required | all commands | Base URL for the server profile. |
| `username` | string | `""` | global `--user` fallback | Default CLI username. |
| `no_verify_ssl` | boolean | `false` | global SSL handling | Default TLS verification behavior. |
| `api_key` | string | `""` | global `--api-key` fallback | Default admin bearer token. |

```bash
jji --server prod health
```

**Return value/effect:** Resolves the server connection without repeating the full URL or admin key on every command.

### Profile keys: analysis defaults
These keys are merged into `jji analyze` when the matching CLI option is omitted.

| Key | Type | Default | Used by | Description |
| --- | --- | --- | --- | --- |
| `jenkins_url` | string | `""` | `analyze` | Jenkins base URL. |
| `jenkins_user` | string | `""` | `analyze` | Jenkins username. |
| `jenkins_password` | string | `""` | `analyze` | Jenkins password or token. |
| `jenkins_ssl_verify` | boolean/null | unset | `analyze` | Jenkins TLS verification override. |
| `jenkins_timeout` | integer | `0` | `analyze` | Jenkins request timeout override. `0` means no CLI override. |
| `tests_repo_url` | string | `""` | `analyze` | Tests repository URL. |
| `ai_provider` | string | `""` | `analyze` | Default AI provider. |
| `ai_model` | string | `""` | `analyze` | Default AI model. |
| `ai_cli_timeout` | integer | `0` | `analyze` | AI CLI timeout override in minutes. `0` means no CLI override. |
| `enable_jira` | boolean/null | unset | `analyze` | Default Jira enable/disable state. |
| `peers` | string | `""` | `analyze` | Peer AI list in `provider:model,provider:model` format. |
| `peer_analysis_max_rounds` | integer | `0` | `analyze` | Peer analysis round override. `0` means no CLI override. |
| `additional_repos` | string | `""` | `analyze` | Extra repo list in `name:url`, optional `:ref`, optional trailing `@token` format. |
| `wait_for_completion` | boolean/null | unset | `analyze` | Default wait behavior before analysis starts. |
| `poll_interval_minutes` | integer | `0` | `analyze` | Poll interval override. `0` means no CLI override. |
| `max_wait_minutes` | integer | `0` | `analyze` | Max wait override. `0` means no CLI override. |
| `force` | boolean/null | unset | `analyze` | Default force-analysis behavior. |

```bash
jji analyze --job-name periodic-e2e --build-number 274
```

**Return value/effect:** Sends profile defaults together with the required job name and build number.

### Profile keys: Jira and GitHub defaults
These keys are used by issue-preview, issue-creation, and Jira lookup commands.

| Key | Type | Default | Used by | Description |
| --- | --- | --- | --- | --- |
| `jira_url` | string | `""` | `analyze` | Jira base URL for analysis-time matching. |
| `jira_email` | string | `""` | `analyze`, `jira-projects`, `jira-security-levels`, Jira issue commands | Jira email value used when required. |
| `jira_api_token` | string | `""` | `analyze` | Jira Cloud API token for analysis-time matching. |
| `jira_pat` | string | `""` | `analyze` | Jira Server/DC PAT for analysis-time matching. |
| `jira_token` | string | `""` | `jira-projects`, `jira-security-levels`, Jira issue commands | Jira token fallback for CLI lookup and issue commands. |
| `jira_project_key` | string | `""` | `analyze`, Jira issue commands | Default Jira project key. |
| `jira_security_level` | string | `""` | Jira issue commands | Default Jira security level name. |
| `jira_ssl_verify` | boolean/null | unset | `analyze` | Jira TLS verification override. |
| `jira_max_results` | integer | `0` | `analyze` | Jira search limit override. `0` means no CLI override. |
| `github_token` | string | `""` | `analyze`, GitHub issue commands | GitHub token fallback. |
| `github_repo_url` | string | `""` | GitHub issue commands | Default GitHub repository URL override. |

```bash
jji preview-issue job-123 --test tests.e2e.test_login --type jira
```

**Return value/effect:** Uses profile tracker defaults when matching CLI options are not supplied.

## Server and discovery
Global options are omitted from the tables below unless they change command behavior.

### `jji health`
Checks server health.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji health
```

**Return value/effect:** Default output prints `Status`, optional component `Checks`, and optional error-rate information. JSON mode returns the full health payload.

### `jji capabilities`
Shows server support for post-analysis automation.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji capabilities
```

**Return value/effect:** Prints the capability object, including GitHub/Jira automation flags.

### `jji ai-configs`
Lists provider/model pairs from completed analyses.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji ai-configs
```

**Return value/effect:** Default output prints a two-column table of provider/model pairs. If no completed analyses have recorded AI settings, the command prints `No AI configurations found from completed analyses.`

### `jji jira-projects`
Lists Jira projects available to the supplied Jira credentials.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--query` | string | `""` | Filter the project list by a search string. |
| `--jira-token` | string | profile `jira_token` or empty | Jira token override. |
| `--jira-email` | string | profile `jira_email` or empty | Jira email override. |

```bash
jji jira-projects --query platform
```

**Return value/effect:** Default output prints project `KEY` and `NAME`. JSON mode returns the full project array.

### `jji jira-security-levels PROJECT_KEY`
Lists Jira issue security levels for a project.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `PROJECT_KEY` | string | — | Jira project key to inspect. |
| `--jira-token` | string | profile `jira_token` or empty | Jira token override. |
| `--jira-email` | string | profile `jira_email` or empty | Jira email override. |

```bash
jji jira-security-levels PROJ
```

**Return value/effect:** Default output prints security level names and descriptions. If none are returned, the command prints `No security levels found.`

## Analysis jobs
### `jji analyze`
Queues a Jenkins build for analysis.

**Required options**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--job-name`, `-j` | string | — | Jenkins job name. |
| `--build-number`, `-b` | integer | — | Build number to analyze. Must be greater than `0`. |

**AI and context options**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--provider` | string | profile `ai_provider` or omitted | AI provider, for example `claude`, `gemini`, or `cursor`. |
| `--model` | string | profile `ai_model` or omitted | AI model name. |
| `--raw-prompt` | string | omitted | Extra prompt text appended to the analysis request. |
| `--peers` | string | profile `peers` or omitted | Peer AIs in `provider:model,provider:model` format. |
| `--peer-analysis-max-rounds` | integer | profile `peer_analysis_max_rounds` or omitted | Debate rounds for peer analysis. Valid range: `1` to `10`. |
| `--additional-repos` | string | profile `additional_repos` or omitted | Extra repos in `name:url`, optional `:ref`, optional trailing `@token` format. |

**Jenkins, Jira, and GitHub options**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--jira/--no-jira` | boolean | profile `enable_jira` or omitted | Enable or disable Jira matching for this run. |
| `--jenkins-url` | string | profile `jenkins_url` or env `JENKINS_URL` | Jenkins base URL. |
| `--jenkins-user` | string | profile `jenkins_user` or env `JENKINS_USER` | Jenkins username. |
| `--jenkins-password` | string | profile `jenkins_password` or env `JENKINS_PASSWORD` | Jenkins password or token. |
| `--jenkins-ssl-verify/--no-jenkins-ssl-verify` | boolean | profile `jenkins_ssl_verify` or omitted | Jenkins TLS verification override. |
| `--jenkins-timeout` | integer | profile `jenkins_timeout` or omitted | Jenkins API timeout in seconds. Must be greater than `0`. |
| `--jenkins-artifacts-max-size-mb` | integer | omitted | Artifact size cap in MB. Must be greater than `0`. |
| `--get-job-artifacts/--no-get-job-artifacts` | boolean | omitted | Download or skip Jenkins artifacts for AI context. |
| `--tests-repo-url` | string | profile `tests_repo_url` or env `TESTS_REPO_URL` | Tests repository URL. |
| `--jira-url` | string | profile `jira_url` or env `JIRA_URL` | Jira base URL. |
| `--jira-email` | string | profile `jira_email` or env `JIRA_EMAIL` | Jira Cloud email. |
| `--jira-api-token` | string | profile `jira_api_token` or env `JIRA_API_TOKEN` | Jira Cloud API token. |
| `--jira-pat` | string | profile `jira_pat` or env `JIRA_PAT` | Jira Server/DC PAT. |
| `--jira-project-key` | string | profile `jira_project_key` or env `JIRA_PROJECT_KEY` | Jira project key. |
| `--jira-ssl-verify/--no-jira-ssl-verify` | boolean | profile `jira_ssl_verify` or omitted | Jira TLS verification override. |
| `--jira-max-results` | integer | profile `jira_max_results` or omitted | Jira search limit. Must be greater than `0`. |
| `--github-token` | string | profile `github_token` or env `GITHUB_TOKEN` | GitHub token. |
| `--ai-cli-timeout` | integer | profile `ai_cli_timeout` or omitted | AI CLI timeout in minutes. Must be greater than `0`. |

**Monitoring options**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--wait/--no-wait` | boolean | profile `wait_for_completion` or omitted | Wait for Jenkins completion before analysis. |
| `--poll-interval` | integer | profile `poll_interval_minutes` or omitted | Poll interval in minutes. Must be greater than `0`. |
| `--max-wait` | integer | profile `max_wait_minutes` or omitted | Max wait in minutes. Must be `0` or greater. |
| `--force/--no-force` | boolean | profile `force` or omitted | Force analysis even when the build succeeded. |

```bash
jji analyze \
  --job-name periodic-e2e \
  --build-number 274 \
  --provider claude \
  --model opus-4 \
  --wait \
  --poll-interval 2 \
  --additional-repos "infra:https://github.com/org/infra:main"
```

**Return value/effect:** Default output prints the queued `job_id`, queued `status`, and poll URL. JSON mode returns the full queue response.

> **Warning:** `--peer-analysis-max-rounds` must be between `1` and `10`.
>


> **Warning:** Invalid `--peers` and `--additional-repos` strings exit with an error.
>


> **Tip:** See [Copy Common Analysis Recipes](copy-common-analysis-recipes.html) for copy-ready command combinations built from these flags.

### `jji re-analyze JOB_ID`
Queues a new analysis using the settings from a previous analysis.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Existing analysis job ID. |

```bash
jji re-analyze job-123
```

**Return value/effect:** Default output prints the new queued `job_id`, queued `status`, and poll URL. JSON mode returns the full queue response.

### `jji status JOB_ID`
Shows the status of an analysis job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID to inspect. |

```bash
jji status job-123
```

**Return value/effect:** Default output prints only `job_id` and `status`. JSON mode returns the full stored result payload.

### `jji results list`
Lists recent analysis jobs.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--limit`, `-l` | integer | `50` | Maximum number of jobs to return. |

```bash
jji results list --limit 20
```

**Return value/effect:** Default output prints a table with job ID, status, Jenkins URL, and creation time. JSON mode returns the full array.

### `jji results dashboard`
Lists analysis jobs with dashboard summary fields.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji results dashboard
```

**Return value/effect:** Default output prints job name, build number, status, failure count, reviewed count, comment count, and creation time. JSON mode returns the full array.

### `jji results show JOB_ID`
Shows the stored result for a job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID to display. |
| `--full`, `-f` | boolean | `false` | Print the full JSON result without requiring global `--json`. |

```bash
jji results show job-123 --full
```

**Return value/effect:** Default output prints a short summary. `--full` and `--json` print the complete stored result.

### `jji results delete [JOB_ID ...]`
Deletes one or more stored jobs.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID ...` | string list | empty | One or more job IDs to delete. |
| `--all` | boolean | `false` | Delete all jobs returned by the dashboard. |
| `--confirm` | boolean | `false` | Required together with `--all`. |

```bash
jji results delete job-123 job-124
```

**Return value/effect:** One job prints `Deleted job ...`. Multiple jobs print a deleted count and any failed IDs. JSON mode returns the raw delete response.

> **Warning:** `--all` cannot be combined with explicit job IDs.
>


> **Warning:** `--all` requires `--confirm`.

### `jji results review-status JOB_ID`
Shows review counters for a stored analysis.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |

```bash
jji results review-status job-123
```

**Return value/effect:** Default output prints `total_failures`, `reviewed_count`, and `comment_count`. JSON mode returns the full response.

### `jji results set-reviewed JOB_ID`
Sets or clears the reviewed state for one failure.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |
| `--test`, `-t` | string | — | Failure test name. |
| `--reviewed/--not-reviewed` | boolean | — | Mark the failure reviewed or not reviewed. |
| `--child-job` | string | `""` | Child job name for pipeline child failures. |
| `--child-build` | integer | `0` | Child build number. |

```bash
jji results set-reviewed job-123 --test tests.e2e.test_login --reviewed
```

**Return value/effect:** Default output prints `Marked as reviewed` or `Marked as not reviewed`, including the reviewer when returned. JSON mode returns the raw response.

> **Warning:** `--child-build` must be `0` or greater.
>


> **Warning:** A positive `--child-build` requires `--child-job`.

### `jji results enrich-comments JOB_ID`
Refreshes comment-linked ticket and PR status data.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |

```bash
jji results enrich-comments job-123
```

**Return value/effect:** Default output prints `Enriched N comment(s).` JSON mode returns the raw enrichment response.

## Failure history and classification
### `jji history test TEST_NAME`
Shows history for one test.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `TEST_NAME` | string | — | Fully qualified test name. |
| `--limit`, `-l` | integer | `20` | Maximum recent runs to include. |
| `--job-name`, `-j` | string | `""` | Restrict history to one Jenkins job name. |
| `--exclude-job-id` | string | `""` | Exclude one analysis job ID from the lookup. |

```bash
jji history test tests.e2e.test_login --limit 10
```

**Return value/effect:** Default output prints top-level history fields and optional `Recent runs` and `Comments` tables. JSON mode returns the full history object.

### `jji history search --signature SIGNATURE`
Finds failures that share one error signature.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--signature`, `-s` | string | — | Error signature hash to search for. |
| `--exclude-job-id` | string | `""` | Exclude one analysis job ID from the lookup. |

```bash
jji history search --signature 8b9f4d...
```

**Return value/effect:** Default output prints total occurrences, unique test count, and a table of matching tests. JSON mode returns the full search object.

### `jji history stats JOB_NAME`
Shows aggregate failure statistics for one Jenkins job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_NAME` | string | — | Jenkins job name. |
| `--exclude-job-id` | string | `""` | Exclude one analysis job ID from the lookup. |

```bash
jji history stats periodic-e2e
```

**Return value/effect:** Default output prints analyzed build counts, failure rate, and an optional `Most common failures` table. JSON mode returns the full stats object.

### `jji history failures`
Lists paginated failure history.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--limit`, `-l` | integer | `50` | Page size. |
| `--offset`, `-o` | integer | `0` | Starting offset. |
| `--search`, `-s` | string | `""` | Test-name substring filter. |
| `--classification`, `-c` | string | `""` | Classification filter. |
| `--job-name`, `-j` | string | `""` | Jenkins job-name filter. |

```bash
jji history failures --classification "PRODUCT BUG" --limit 25
```

**Return value/effect:** Default output prints a total/offset line and a table of matching failures. JSON mode returns the paginated payload.

### `jji classify TEST_NAME`
Creates a manual classification for one failure.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `TEST_NAME` | string | — | Fully qualified test name. |
| `--type`, `-t` | string | — | Classification value: `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, `KNOWN_BUG`, or `INTERMITTENT`. |
| `--job-id` | string | — | Analysis job ID the classification applies to. |
| `--reason`, `-r` | string | `""` | Free-text reason. |
| `--job-name`, `-j` | string | `""` | Job name override. |
| `--references` | string | `""` | Bug URLs or ticket keys. |
| `--child-job` | string | `""` | Child job name. |
| `--child-build` | integer | `0` | Child build number. |

```bash
jji classify tests.e2e.test_login --type REGRESSION --job-id job-123 --reason "fails after merge"
```

**Return value/effect:** Default output prints the created classification ID. JSON mode returns the full creation response.

### `jji override-classification JOB_ID`
Overrides the analysis classification on one failure.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |
| `--test`, `-t` | string | — | Failure test name. |
| `--classification`, `-c` | string | — | Override value: `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE`. |
| `--child-job` | string | `""` | Child job name. |
| `--child-build` | integer | `0` | Child build number. |

```bash
jji override-classification job-123 --test tests.e2e.test_login --classification "PRODUCT BUG"
```

**Return value/effect:** Default output prints the new classification. JSON mode returns the full response.

### `jji classifications list`
Lists stored classifications.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--job-id` | string | `""` | Filter by job ID. |
| `--test-name`, `-t` | string | `""` | Filter by test name. |
| `--type`, `-c` | string | `""` | Filter by classification. |
| `--job-name`, `-j` | string | `""` | Filter by job name. |
| `--parent-job-name` | string | `""` | Filter by parent job name. |

```bash
jji classifications list --job-name periodic-e2e --type REGRESSION
```

**Return value/effect:** Default output prints a table of classification records. If none match, the command prints `No classifications found.`

## Comments and mentions
### `jji comments list JOB_ID`
Lists comments for a stored analysis.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |

```bash
jji comments list job-123
```

**Return value/effect:** Default output prints a table of comments. JSON mode returns the full response object, including `comments` and `reviews`.

### `jji comments add JOB_ID`
Adds a comment to one failure.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |
| `--test`, `-t` | string | — | Failure test name. |
| `--message`, `-m` | string | — | Comment text. |
| `--child-job` | string | `""` | Child job name. |
| `--child-build` | integer | `0` | Child build number. |

```bash
jji comments add job-123 --test tests.e2e.test_login --message "tracking in PROJ-456"
```

**Return value/effect:** Default output prints the created comment ID. JSON mode returns the full creation response.

### `jji comments delete JOB_ID COMMENT_ID`
Deletes one comment.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |
| `COMMENT_ID` | integer | — | Comment ID to delete. |

```bash
jji comments delete job-123 42
```

**Return value/effect:** Default output prints `Comment deleted.` JSON mode returns the raw delete response.

### `jji mentionable-users`
Lists usernames that can be mentioned in comments.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji mentionable-users
```

**Return value/effect:** Default output prints one username per line. JSON mode returns `{ "usernames": [...] }`.

### `jji mentions`
Lists the current user's mentions.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--limit`, `-l` | integer | `50` | Maximum mentions to return. |
| `--offset`, `-o` | integer | `0` | Pagination offset. |
| `--unread` | boolean | `false` | Restrict output to unread mentions. |

```bash
jji mentions --unread
```

**Return value/effect:** Default output prints a summary line and a mentions table. JSON mode returns the full paginated mentions payload.

### `jji mentions-mark-read --ids IDS`
Marks selected mentions as read.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--ids` | string | — | Comma-separated positive integer comment IDs. |

```bash
jji mentions-mark-read --ids 10,11,12
```

**Return value/effect:** Marks the requested mentions as read. Use `--json` to inspect the server response.

> **Warning:** Every ID must be a positive integer.

### `jji mentions-mark-all-read`
Marks all mentions as read.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji mentions-mark-all-read
```

**Return value/effect:** Marks all mentions as read. Use `--json` to inspect the server response.

## Issues and integrations
### `jji preview-issue JOB_ID`
Previews generated issue content for GitHub or Jira.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |
| `--test`, `-t` | string | — | Failure test name. |
| `--type` | string | — | Issue target: `github` or `jira`. |
| `--child-job` | string | `""` | Child job name. |
| `--child-build` | integer | `0` | Child build number. |
| `--include-links` | boolean | `false` | Include full URLs in the generated body. |
| `--ai-provider` | string | `""` | AI provider override for issue text generation. |
| `--ai-model` | string | `""` | AI model override for issue text generation. |
| `--github-token` | string | profile `github_token` or empty | GitHub token override. Used only with `--type github`. |
| `--github-repo-url` | string | profile `github_repo_url` or empty | GitHub repository URL override. Used only with `--type github`. |
| `--jira-token` | string | profile `jira_token` or empty | Jira token override. Used only with `--type jira`. |
| `--jira-email` | string | profile `jira_email` or empty | Jira email override. Used only with `--type jira`. |
| `--jira-project-key` | string | profile `jira_project_key` or empty | Jira project key override. Used only with `--type jira`. |
| `--jira-security-level` | string | profile `jira_security_level` or empty | Jira security level name override. Used only with `--type jira`. |

```bash
jji preview-issue job-123 --test tests.e2e.test_login --type github --include-links
```

**Return value/effect:** Default output prints the generated title, body, and any returned similar issues. JSON mode returns the full preview object.

> **Note:** GitHub credential options are ignored for `--type jira`. Jira credential options are ignored for `--type github`.

### `jji create-issue JOB_ID`
Creates a GitHub issue or Jira bug from a stored failure.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |
| `--test`, `-t` | string | — | Failure test name. |
| `--type` | string | — | Issue target: `github` or `jira`. |
| `--title` | string | — | Issue title. |
| `--body` | string | — | Issue body. |
| `--child-job` | string | `""` | Child job name. |
| `--child-build` | integer | `0` | Child build number. |
| `--github-token` | string | profile `github_token` or empty | GitHub token override. Used only with `--type github`. |
| `--github-repo-url` | string | profile `github_repo_url` or empty | GitHub repository URL override. Used only with `--type github`. |
| `--jira-token` | string | profile `jira_token` or empty | Jira token override. Used only with `--type jira`. |
| `--jira-email` | string | profile `jira_email` or empty | Jira email override. Used only with `--type jira`. |
| `--jira-project-key` | string | profile `jira_project_key` or empty | Jira project key override. Used only with `--type jira`. |
| `--jira-security-level` | string | profile `jira_security_level` or empty | Jira security level name override. Used only with `--type jira`. |

```bash
jji create-issue \
  job-123 \
  --test tests.e2e.test_login \
  --type jira \
  --title "tests.e2e.test_login fails on periodic-e2e" \
  --body "Failure details..."
```

**Return value/effect:** Default output prints the created issue key/number, URL, and any created JJI comment ID. JSON mode returns the full creation response.

### `jji validate-token TOKEN_TYPE`
Validates a GitHub or Jira token.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `TOKEN_TYPE` | string | — | Token type: `github` or `jira`. |
| `--token` | string | prompt | Token value. The CLI prompts and hides input when omitted. |
| `--email` | string | `""` | Jira email value, used with Jira validation when needed. |

```bash
jji validate-token github --token "$GITHUB_TOKEN"
```

**Return value/effect:** Prints `Valid` on success. Invalid tokens exit non-zero. JSON mode returns the full validation payload.

### `jji push-reportportal JOB_ID`
Pushes JJI classifications into Report Portal.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_ID` | string | — | Analysis job ID. |
| `--child-job-name` | string | unset | Child job name for pipeline child pushes. |
| `--child-build-number` | integer | unset | Child build number for pipeline child pushes. |

```bash
jji push-reportportal job-123
```

**Return value/effect:** Default output prints pushed count, optional launch ID, unmatched tests, and error count/details. JSON mode returns the full push response.

> **Note:** Hidden alias: `jji push-rp JOB_ID`.

## Auth and admin
### `jji auth login`
Validates admin credentials against the server.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--username`, `-u` | string | — | Admin username. |
| `--api-key`, `-k` | string | — | Admin API key. |

```bash
jji auth login --username admin --api-key "$JJI_API_KEY"
```

**Return value/effect:** Default output prints username, role, and admin status. JSON mode returns the full auth response.

> **Note:** This command does not persist credentials to `config.toml`.

### `jji auth logout`
Calls the server logout endpoint.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji auth logout
```

**Return value/effect:** Logs out the current server session. Use `--json` to inspect the server response.

### `jji auth whoami`
Shows the current authenticated user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji auth whoami
```

**Return value/effect:** Default output prints `username`, `role`, and `is_admin`. JSON mode returns the full auth payload.

### `jji admin users list`
Lists all known users.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji admin users list
```

**Return value/effect:** Default output prints a user table with role, creation time, and last-seen time. JSON mode returns the full user list payload.

### `jji admin users create USERNAME`
Creates a new admin user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `USERNAME` | string | — | Username for the new admin user. |

```bash
jji admin users create newadmin
```

**Return value/effect:** Default output prints the created username and the new API key. JSON mode returns the full response.

> **Warning:** The new API key is only shown when the command runs.

### `jji admin users delete USERNAME`
Deletes an admin user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `USERNAME` | string | — | Username to delete. |
| `--force`, `-f` | boolean | `false` | Skip the confirmation prompt. |

```bash
jji admin users delete oldadmin --force
```

**Return value/effect:** Default output prints `Deleted admin user: ...`. JSON mode returns the raw delete response.

### `jji admin users rotate-key USERNAME`
Rotates one admin user's API key.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `USERNAME` | string | — | Username whose key should be rotated. |

```bash
jji admin users rotate-key myuser
```

**Return value/effect:** Default output prints the username and new API key. JSON mode returns the full rotation response.

> **Warning:** The rotated API key is only shown when the command runs.

### `jji admin users change-role USERNAME ROLE`
Changes a user's role.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `USERNAME` | string | — | Username to modify. |
| `ROLE` | string | — | New role: `admin` or `user`. |

```bash
jji admin users change-role myuser admin
```

**Return value/effect:** Default output prints the updated role. Promoting to `admin` also prints a newly generated API key when returned. JSON mode returns the full response.

> **Warning:** Promotion-generated API keys are only shown when the command runs.

### `jji admin token-usage`
Reports AI token and cost usage.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--period` | string | unset | Preset range: `today`, `week`, `month`, or `all`. |
| `--start-date` | string | unset | Start date in `YYYY-MM-DD` format. |
| `--end-date` | string | unset | End date in `YYYY-MM-DD` format. |
| `--provider` | string | unset | Filter by AI provider. |
| `--model` | string | unset | Filter by AI model. |
| `--call-type` | string | unset | Filter by call type. |
| `--group-by` | string | unset | Group by `provider`, `model`, `call_type`, `day`, `week`, `month`, or `job`. |
| `--job-id` | string | unset | Switch to per-job token usage for one analysis job. |
| `--format` | string | `table` | Output format: `table`, `json`, or `csv`. |

```bash
jji admin token-usage --group-by provider
```

**Return value/effect:** With no filters, the command prints a summary dashboard (`Today`, `This Week`, `This Month`, and top models). With filters or a period, it prints aggregated totals and an optional breakdown. With `--job-id`, it prints per-call records for one job.

> **Note:** `--job-id` switches to per-job mode.
>


> **Note:** `--period today|week|month` sets the start date automatically.
>


> **Warning:** Invalid `--period` or `--format` values exit with an error.

## Metadata
### `jji metadata list`
Lists job metadata records.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--team` | string | `""` | Filter by team. |
| `--tier` | string | `""` | Filter by tier. |
| `--version` | string | `""` | Filter by version. |
| `--label`, `-l` | string list | empty | Filter by one or more labels. Repeat the option for multiple labels. |

```bash
jji metadata list --team platform --label nightly
```

**Return value/effect:** Default output prints a metadata table with job name, team, tier, version, and labels. JSON mode returns the full array.

### `jji metadata get JOB_NAME`
Shows metadata for one job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_NAME` | string | — | Job name to look up. |

```bash
jji metadata get periodic-e2e
```

**Return value/effect:** Default output prints the metadata row for the selected job. JSON mode returns the full object.

### `jji metadata set JOB_NAME`
Creates or updates metadata for one job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_NAME` | string | — | Job name to update. |
| `--team` | string | `""` | Team value. |
| `--tier` | string | `""` | Tier value. |
| `--version` | string | `""` | Version value. |
| `--label`, `-l` | string list | empty | Label values. Repeat to set multiple labels. |

```bash
jji metadata set periodic-e2e --team platform --tier critical --label nightly
```

**Return value/effect:** Default output prints `Metadata set for ...`. JSON mode returns the full stored metadata object.

### `jji metadata delete JOB_NAME`
Deletes metadata for one job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_NAME` | string | — | Job name to delete metadata for. |

```bash
jji metadata delete periodic-e2e
```

**Return value/effect:** Default output prints `Metadata deleted for ...`. JSON mode returns the raw delete response.

### `jji metadata import FILE_PATH`
Bulk imports metadata from a JSON or YAML file.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `FILE_PATH` | string | — | Path to a JSON file or a `.yaml`/`.yml` file containing an array of metadata objects. |

```bash
jji metadata import ./job-metadata.yaml
```

**Return value/effect:** Default output prints `Imported N metadata entries.` JSON mode returns the raw bulk-update response.

> **Note:** JSON is used for non-YAML extensions.
>


> **Warning:** The file must contain an array of objects.

### `jji metadata rules`
Lists configured metadata auto-assignment rules.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only global options. |

```bash
jji metadata rules
```

**Return value/effect:** Default output prints the rules file path when returned, plus a numbered rule list. JSON mode returns the full rules object.

### `jji metadata preview JOB_NAME`
Previews metadata rule matching for one job name.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JOB_NAME` | string | — | Job name to test against the configured rules. |

```bash
jji metadata preview test-smoke
```

**Return value/effect:** Default output prints the matched metadata or `No rules matched ...`. JSON mode returns the full preview result.

## Config commands
### `jji config` / `jji config show`
Shows the current CLI config file and configured profiles.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only config file state. |

```bash
jji config
```

**Return value/effect:** Prints the config file path, default server, and configured server list. If the config file does not exist, the command prints the expected path and a starter snippet.

### `jji config servers`
Lists configured server profiles.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| none | — | — | Uses only config file state. |

```bash
jji config servers
```

**Return value/effect:** Default output prints a table of profile names, URLs, usernames, SSL behavior, and default-marker state. JSON mode returns an object keyed by server name.

### `jji config completion [SHELL]`
Prints shell-completion setup instructions.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `SHELL` | string | `zsh` | Shell type: `bash` or `zsh`. |

```bash
jji config completion bash
```

**Return value/effect:** Prints the shell snippet that evaluates `jji --show-completion ...` for the selected shell.

> **Warning:** Only `bash` and `zsh` are supported.

## Related Pages

- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Investigate Failure History](investigate-failure-history.html)
- [Copy Common Analysis Recipes](copy-common-analysis-recipes.html)
- [REST API Reference](rest-api-reference.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)