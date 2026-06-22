# CLI Command Reference

Complete reference for all `rootcoz` CLI commands and subcommands. For installation and initial setup, see [Setting Up the CLI](cli-setup.html). For copy-paste workflow examples, see [CLI Recipes](cli-recipes.html).

## Global Options

These options apply to all commands and must appear **before** the subcommand.

| Option | Type | Default | Env Var | Description |
|--------|------|---------|---------|-------------|
| `--server`, `-s` | string | *(from config)* | `ROOTCOZ_SERVER` | Server name from config file or full URL |
| `--json` | flag | `false` | — | Output as JSON instead of table |
| `--user` | string | `""` | `ROOTCOZ_USERNAME` | Username for authenticated actions |
| `--api-key` | string | `""` | `ROOTCOZ_API_KEY` | API key for Bearer token authentication |
| `--no-verify-ssl` | flag | *(from config)* | `ROOTCOZ_NO_VERIFY_SSL` | Disable SSL certificate verification |
| `--verify-ssl` | flag | — | — | Force SSL verification on (overrides config) |
| `--insecure` | flag | `false` | — | Alias for `--no-verify-ssl` |

> **Note:** `--verify-ssl` and `--insecure` are mutually exclusive.

```bash
# Use a named server from config
rootcoz -s production results list

# Use a direct URL
rootcoz -s https://rootcoz.example.com results list

# JSON output with API key
rootcoz --json --api-key sk-abc123 results list
```

Server resolution priority (highest to lowest):
1. CLI flags / environment variables
2. Config file (`~/.config/rootcoz/config.toml`)

---

## analyze

Submit an analysis job (Jenkins build or JUnit XML file). Requires **operator** role or higher.

```
rootcoz analyze [OPTIONS]
```

### Source Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--source` | string | `jenkins` | Analysis source: `jenkins` or `file` |
| `--job-name`, `-j` | string | — | Jenkins job name (required for `--source jenkins`) |
| `--build-number`, `-b` | int | — | Build number (required for `--source jenkins`) |
| `--file`, `-f` | string | — | Path to JUnit XML file (required for `--source file`) |
| `--name`, `-n` | string | *(job_name)* | Display name on the dashboard |
| `--tag` | string | *(repeatable)* | Tag for categorization (can be specified multiple times) |

### AI Options

| Option | Type | Default | Env Var | Description |
|--------|------|---------|---------|-------------|
| `--provider` | string | *(server default)* | — | AI provider (`claude`, `gemini`, `cursor`) |
| `--model` | string | *(server default)* | — | AI model to use |
| `--ai-call-timeout` | int | *(server default)* | — | AI call timeout in minutes |
| `--max-concurrent` | int | `0` (server default) | — | Max concurrent AI calls |
| `--raw-prompt` | string | `""` | — | Additional AI instructions appended to prompt |
| `--issue-prompt` | string | `""` | — | Custom issue generation prompt |

### Peer Analysis Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--peers` | string | *(config)* | Peer AI configs as `provider:model,provider:model` |
| `--peer-analysis-max-rounds` | int | `3` | Maximum debate rounds (1–10) |

### Jenkins Options

| Option | Type | Default | Env Var | Description |
|--------|------|---------|---------|-------------|
| `--jenkins-url` | string | *(config)* | `JENKINS_URL` | Jenkins server URL |
| `--jenkins-user` | string | *(config)* | `JENKINS_USER` | Jenkins username |
| `--jenkins-password` | string | *(config)* | `JENKINS_PASSWORD` | Jenkins password or API token |
| `--jenkins-ssl-verify` / `--no-jenkins-ssl-verify` | bool | *(server default)* | — | Jenkins SSL verification |
| `--jenkins-timeout` | int | *(server default)* | — | Jenkins API timeout in seconds |
| `--jenkins-artifacts-max-size-mb` | int | *(server default)* | — | Max artifacts size in MB |
| `--get-job-artifacts` / `--no-get-job-artifacts` | bool | *(server default)* | — | Download build artifacts for AI context |
| `--wait` / `--no-wait` | bool | *(server default)* | — | Wait for Jenkins job to complete |
| `--poll-interval` | int | *(server default)* | — | Minutes between Jenkins polls |
| `--max-wait` | int | *(server default)* | — | Max minutes to wait for completion |
| `--force` / `--no-force` | bool | *(server default)* | — | Force analysis on successful builds |

### Repository Options

| Option | Type | Default | Env Var | Description |
|--------|------|---------|---------|-------------|
| `--tests-repo-url` | string | *(config)* | `TESTS_REPO_URL` | Tests repository URL |
| `--tests-repo-token` | string | *(config)* | `TESTS_REPO_TOKEN` | Token for private test repos |
| `--additional-repos` | string | *(config)* | — | Additional repos as `name:url,name:url` |

### Jira Options

| Option | Type | Default | Env Var | Description |
|--------|------|---------|---------|-------------|
| `--jira` / `--no-jira` | bool | *(server default)* | — | Enable/disable Jira integration |
| `--jira-url` | string | *(config)* | `JIRA_URL` | Jira instance URL |
| `--jira-email` | string | *(config)* | `JIRA_EMAIL` | Jira Cloud email |
| `--jira-api-token` | string | *(config)* | `JIRA_API_TOKEN` | Jira Cloud API token |
| `--jira-pat` | string | *(config)* | `JIRA_PAT` | Jira Server/DC personal access token |
| `--jira-project-key` | string | *(config)* | `JIRA_PROJECT_KEY` | Jira project key for searches |
| `--jira-ssl-verify` / `--no-jira-ssl-verify` | bool | *(server default)* | — | Jira SSL verification |
| `--jira-max-results` | int | *(server default)* | — | Max Jira search results |

### GitHub Option

| Option | Type | Default | Env Var | Description |
|--------|------|---------|---------|-------------|
| `--github-token` | string | *(config)* | `GITHUB_TOKEN` | GitHub API token |

```bash
# Analyze a Jenkins build
rootcoz analyze --source jenkins --job-name my-pipeline --build-number 42

# Analyze a JUnit XML file
rootcoz analyze --source file --file results.xml --name "nightly-run"

# Analyze with specific AI provider and peer review
rootcoz analyze -j my-pipeline -b 42 \
  --provider claude --model claude-sonnet-4-20250514 \
  --peers "gemini:gemini-2.5-pro,cursor:gpt-5.4-xhigh"

# Analyze with tags and Jira enabled
rootcoz analyze -j my-pipeline -b 42 --tag nightly --tag release --jira
```

---

## re-analyze

Re-analyze a previously analyzed job using the same settings. Requires **operator** role or higher.

```
rootcoz re-analyze JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID of the analysis to re-run |

```bash
rootcoz re-analyze abc123-def456
```

---

## status

Check the current analysis status for a job.

```
rootcoz status JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID to check |

```bash
rootcoz status abc123-def456
```

---

## abort

Abort a running or waiting analysis. Requires **operator** role or higher.

```
rootcoz abort JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID to abort |

```bash
rootcoz abort abc123-def456
```

---

## health

Check server health status.

```
rootcoz health [OPTIONS]
```

```bash
rootcoz health
# Status: healthy
#
# Checks:
#   database: healthy
#   sidecar: healthy
#
# Error rate: 0.0% (0/150 in 300s window)

rootcoz health --json
```

---

## register

Register a new user and receive a one-time API key.

```
rootcoz register USERNAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `USERNAME` | string | Username to register |

> **Warning:** The API key is displayed only once at registration. Save it immediately.

```bash
rootcoz register myuser
# Registered: myuser
# ⚠️  Save this API key — you won't see it again!
# API Key: sk-abc123...
```

---

## results

Manage analysis results. All subcommands require authentication.

### results list

List recent analyzed jobs.

```
rootcoz results list [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit`, `-l` | int | `50` | Max results to return |

```bash
rootcoz results list --limit 10
```

### results dashboard

List analysis jobs with dashboard metadata (failure counts, review progress).

```
rootcoz results dashboard [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--label`, `-l` | string | *(repeatable)* | Filter by label |
| `--exclude-tag` | string | *(repeatable)* | Exclude results with this tag |

```bash
rootcoz results dashboard
rootcoz results dashboard --label team:platform --exclude-tag experimental
```

### results show

Show the analysis result for a specific job.

```
rootcoz results show JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID to show |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--full`, `-f` | flag | `false` | Show complete JSON result |

```bash
rootcoz results show abc123
rootcoz results show abc123 --full
```

### results delete

Delete one or more jobs and all related data. Requires **operator** role (own jobs) or **admin** role (any job).

```
rootcoz results delete [JOB_IDS...] [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_IDS` | string(s) | One or more job IDs to delete |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--all` | flag | `false` | Delete all jobs |
| `--confirm` | flag | `false` | Confirm deletion (required with `--all`) |

> **Warning:** `--all` cannot be combined with explicit job IDs.

```bash
rootcoz results delete abc123
rootcoz results delete abc123 def456 ghi789
rootcoz results delete --all --confirm
```

### results review-status

Show review status for an analysis.

```
rootcoz results review-status JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

```bash
rootcoz results review-status abc123
```

### results set-reviewed

Set or clear the reviewed state for a test failure. Requires **reviewer** role or higher.

```
rootcoz results set-reviewed JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--test`, `-t` | string | *(required)* | Fully qualified test name |
| `--reviewed` / `--not-reviewed` | bool | *(required)* | Mark as reviewed or not |
| `--child-job` | string | `""` | Child job name |
| `--child-build` | int | `0` | Child build number |

```bash
rootcoz results set-reviewed abc123 \
  --test com.example.MyTest#testMethod --reviewed

rootcoz results set-reviewed abc123 \
  --test com.example.MyTest#testMethod --not-reviewed
```

### results enrich-comments

Enrich comments with live PR/ticket statuses.

```
rootcoz results enrich-comments JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

```bash
rootcoz results enrich-comments abc123
# Enriched 3 comment(s).
```

---

## history

Query failure history for tests and jobs.

### history test

Show failure history for a specific test.

```
rootcoz history test TEST_NAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `TEST_NAME` | string | Fully qualified test name |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit`, `-l` | int | `20` | Max results |
| `--job-name`, `-j` | string | `""` | Filter by job name |
| `--exclude-job-id` | string | `""` | Exclude results from this job ID |

```bash
rootcoz history test com.example.MyTest#testMethod --limit 10
rootcoz history test com.example.MyTest#testMethod --job-name my-pipeline
```

### history search

Find tests that failed with the same error signature.

```
rootcoz history search [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--signature`, `-s` | string | *(required)* | Error signature hash (SHA-256) |
| `--exclude-job-id` | string | `""` | Exclude results from this job ID |

```bash
rootcoz history search --signature a1b2c3d4e5f6...
```

### history stats

Show aggregate statistics for a Jenkins job.

```
rootcoz history stats JOB_NAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_NAME` | string | Jenkins job name |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--exclude-job-id` | string | `""` | Exclude results from this job ID |

```bash
rootcoz history stats my-pipeline
# Job: my-pipeline
# Builds analyzed: 45
# Builds with failures: 12
# Failure rate: 26.7%
```

### history failures

List paginated failure history.

```
rootcoz history failures [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit`, `-l` | int | `50` | Max results per page |
| `--offset`, `-o` | int | `0` | Pagination offset |
| `--search`, `-s` | string | `""` | Search test names |
| `--classification`, `-c` | string | `""` | Filter by classification |
| `--job-name`, `-j` | string | `""` | Filter by job name |

```bash
rootcoz history failures --search "MyTest" --classification "PRODUCT BUG"
rootcoz history failures --limit 100 --offset 50
```

---

## classify

Classify a test failure. Requires **reviewer** role or higher.

```
rootcoz classify TEST_NAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `TEST_NAME` | string | Fully qualified test name |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--type`, `-t` | string | *(required)* | Classification: `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, `KNOWN_BUG`, or `INTERMITTENT` |
| `--job-id` | string | *(required)* | Job ID this classification applies to |
| `--reason`, `-r` | string | `""` | Reason for classification |
| `--job-name`, `-j` | string | `""` | Job name |
| `--references` | string | `""` | Bug URLs or ticket keys |
| `--child-job` | string | `""` | Child job name |
| `--child-build` | int | `0` | Child build number |

```bash
rootcoz classify com.example.MyTest#testMethod \
  --type KNOWN_BUG --job-id abc123 \
  --reason "Tracked in JIRA-456" --references "JIRA-456"
```

---

## classifications

List test classifications.

### classifications list

```
rootcoz classifications list [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--job-id` | string | `""` | Filter by job ID |
| `--test-name`, `-t` | string | `""` | Filter by test name |
| `--type`, `-c` | string | `""` | Filter by classification type |
| `--job-name`, `-j` | string | `""` | Filter by job name |
| `--parent-job-name` | string | `""` | Filter by parent job name |

```bash
rootcoz classifications list --job-id abc123
rootcoz classifications list --type FLAKY --job-name my-pipeline
```

---

## comments

Manage comments on test failures. Requires **reviewer** role or higher.

### comments list

List comments for a job.

```
rootcoz comments list JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

```bash
rootcoz comments list abc123
```

### comments add

Add a comment to a test failure.

```
rootcoz comments add JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--test`, `-t` | string | *(required)* | Test name to comment on |
| `--message`, `-m` | string | *(required)* | Comment text |
| `--child-job` | string | `""` | Child job name |
| `--child-build` | int | `0` | Child build number |

```bash
rootcoz comments add abc123 \
  --test com.example.MyTest#testMethod \
  --message "Fixed in PR #123"
```

### comments delete

Delete a comment.

```
rootcoz comments delete JOB_ID COMMENT_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |
| `COMMENT_ID` | int | Comment ID to delete |

```bash
rootcoz comments delete abc123 42
```

---

## failure

Look up and re-analyze individual failures.

### failure show

Show a failure analysis by its UUID.

```
rootcoz failure show FAILURE_UUID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `FAILURE_UUID` | string | UUID of the failure |

```bash
rootcoz failure show 550e8400-e29b-41d4-a716-446655440000
```

### failure re-analyze

Re-analyze a single failure by its UUID. Requires **operator** role or higher.

```
rootcoz failure re-analyze FAILURE_UUID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `FAILURE_UUID` | string | UUID of the failure to re-analyze |

```bash
rootcoz failure re-analyze 550e8400-e29b-41d4-a716-446655440000
```

---

## override-classification

Override the AI-assigned classification of a failure. Requires **reviewer** role or higher.

```
rootcoz override-classification JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--test`, `-t` | string | *(required)* | Test name |
| `--classification`, `-c` | string | *(required)* | `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE` |
| `--child-job` | string | `""` | Child job name |
| `--child-build` | int | `0` | Child build number |

```bash
rootcoz override-classification abc123 \
  --test com.example.MyTest#testMethod \
  --classification "PRODUCT BUG"
```

---

## override-pattern

Override the failure pattern of a test. Requires **reviewer** role or higher.

```
rootcoz override-pattern JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--test`, `-t` | string | *(required)* | Test name |
| `--pattern`, `-p` | string | *(required)* | `NEW`, `REGRESSION`, `FLAKY`, `INTERMITTENT`, `KNOWN_BUG`, or `PERSISTENT` |
| `--child-job` | string | `""` | Child job name |
| `--child-build` | int | `0` | Child build number |

```bash
rootcoz override-pattern abc123 \
  --test com.example.MyTest#testMethod \
  --pattern FLAKY
```

---

## analyze-comment-intent

Analyze whether a comment suggests a failure has been reviewed or resolved.

```
rootcoz analyze-comment-intent COMMENT [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `COMMENT` | string | Comment text to analyze |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--job-id` | string | `""` | Job ID to resolve AI config from |
| `--ai-provider` | string | `""` | AI provider |
| `--ai-model` | string | `""` | AI model |

```bash
rootcoz analyze-comment-intent "Fixed in PR #456, merging now"
# Suggests reviewed: True
# Reason: Comment indicates a fix has been implemented
```

---

## chat

Chat with AI about analyzed jobs. Requires **reviewer** role or higher.

### chat init

Initialize chat workspace and clone repos.

```
rootcoz chat init JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID to initialize chat for |

```bash
rootcoz chat init abc123
# Ready: True
# Repos: tests-repo, infra-repo
```

### chat send

Send a chat message and wait for the AI response. Polls for up to ~2 minutes.

```
rootcoz chat send JOB_ID MESSAGE [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID to chat about |
| `MESSAGE` | string | Message to send |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--provider`, `-p` | string | *(server default)* | AI provider |
| `--model`, `-m` | string | *(server default)* | AI model |

```bash
rootcoz chat send abc123 "Why does testLogin fail with timeout?"
```

### chat history

Show chat history for an analyzed job.

```
rootcoz chat history JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit`, `-l` | int | `200` | Maximum messages to return |

```bash
rootcoz chat history abc123
```

### chat abort

Abort the currently processing chat message.

```
rootcoz chat abort JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

```bash
rootcoz chat abort abc123
```

### chat clear

Clear all chat history for a job.

```
rootcoz chat clear JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

```bash
rootcoz chat clear abc123
# Deleted 15 message(s) for job abc123.
```

### chat close

Signal that you left the chat page (frees server resources).

```
rootcoz chat close JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

```bash
rootcoz chat close abc123
```

---

## metadata

Manage job metadata for filtering and reporting. See [Tracking Failure History and Reports](tracking-history.html) for usage context.

### metadata list

List job metadata with optional filters.

```
rootcoz metadata list [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--team` | string | `""` | Filter by team |
| `--tier` | string | `""` | Filter by tier |
| `--version` | string | `""` | Filter by version |
| `--label`, `-l` | string | *(repeatable)* | Filter by label |

```bash
rootcoz metadata list --team platform
rootcoz metadata list --label env:staging --label product:core
```

### metadata get

Show metadata for a specific job.

```
rootcoz metadata get JOB_NAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_NAME` | string | Job name |

```bash
rootcoz metadata get my-pipeline
```

### metadata set

Set or update metadata for a job.

```
rootcoz metadata set JOB_NAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_NAME` | string | Job name |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--team` | string | `""` | Team owning this job |
| `--tier` | string | `""` | Service tier |
| `--version` | string | `""` | Version label |
| `--label`, `-l` | string | *(repeatable)* | Label to set |

```bash
rootcoz metadata set my-pipeline --team platform --tier tier1 --label env:prod
```

### metadata delete

Delete metadata for a job.

```
rootcoz metadata delete JOB_NAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_NAME` | string | Job name |

```bash
rootcoz metadata delete my-pipeline
```

### metadata import

Bulk import metadata from a JSON or YAML file.

```
rootcoz metadata import FILE_PATH [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `FILE_PATH` | string | Path to JSON or YAML file |

The file must contain an array of objects with fields: `job_name`, `team`, `tier`, `version`, `labels`.

```bash
# metadata.yaml
# - job_name: pipeline-a
#   team: platform
#   tier: tier1
#   labels: ["env:prod"]
# - job_name: pipeline-b
#   team: infra
#   tier: tier2

rootcoz metadata import metadata.yaml
# Imported 2 metadata entries.
```

### metadata rules

List configured metadata rules for auto-assignment.

```
rootcoz metadata rules [OPTIONS]
```

```bash
rootcoz metadata rules
# Rules file: /etc/rootcoz/metadata-rules.yaml
# 3 rule(s):
#   1. pattern='.*-nightly', team='platform', tier='tier1'
#   2. pattern='smoke-.*', team='qa'
```

### metadata preview

Preview what metadata rules would assign to a job name.

```
rootcoz metadata preview JOB_NAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_NAME` | string | Job name to test rules against |

```bash
rootcoz metadata preview my-nightly-pipeline
# Match for 'my-nightly-pipeline':
#   team: platform
#   tier: tier1
```

---

## Bug Creation Commands

For details on issue creation workflows, see [Creating Bug Reports from Analysis](creating-issues.html).

### preview-issue

Preview AI-generated issue content (GitHub or Jira) before creating.

```
rootcoz preview-issue JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--test`, `-t` | string | *(required)* | Test name |
| `--type` | string | *(required)* | `github` or `jira` |
| `--child-job` | string | `""` | Child job name |
| `--child-build` | int | `0` | Child build number |
| `--include-links` | flag | `false` | Include full URLs as clickable links |
| `--ai-provider` | string | `""` | AI provider for content generation |
| `--ai-model` | string | `""` | AI model for content generation |
| `--github-token` | string | *(config)* | GitHub PAT |
| `--github-repo-url` | string | *(config)* | GitHub repository URL |
| `--jira-token` | string | *(config)* | Jira token |
| `--jira-email` | string | *(config)* | Jira email for Cloud auth |
| `--jira-project-key` | string | *(config)* | Jira project key |
| `--jira-security-level` | string | *(config)* | Jira security level name |
| `--issue-prompt` | string | `""` | Additional AI instructions |

```bash
rootcoz preview-issue abc123 \
  --test com.example.MyTest#testMethod \
  --type github
```

### create-issue

Create a GitHub issue or Jira bug from a failure analysis.

```
rootcoz create-issue JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--test`, `-t` | string | *(required)* | Test name |
| `--type` | string | *(required)* | `github` or `jira` |
| `--title` | string | *(required)* | Issue title |
| `--body` | string | *(required)* | Issue body |
| `--child-job` | string | `""` | Child job name |
| `--child-build` | int | `0` | Child build number |
| `--github-token` | string | *(config)* | GitHub PAT |
| `--github-repo-url` | string | *(config)* | GitHub repository URL |
| `--jira-token` | string | *(config)* | Jira token |
| `--jira-email` | string | *(config)* | Jira email |
| `--jira-project-key` | string | *(config)* | Jira project key |
| `--jira-security-level` | string | *(config)* | Jira security level name |
| `--jira-issue-type` | string | `Bug` | Jira issue type (e.g. Bug, Story, Task) |

```bash
rootcoz create-issue abc123 \
  --test com.example.MyTest#testMethod \
  --type github \
  --title "testMethod fails with NullPointerException" \
  --body "## Description\n\nTest fails intermittently..."
```

### get-issue-prompt

Get the issue prompt from the test repo associated with a job.

```
rootcoz get-issue-prompt JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID |

```bash
rootcoz get-issue-prompt abc123
```

---

## validate-token

Validate a GitHub or Jira token. The token is prompted securely (hidden input).

```
rootcoz validate-token TOKEN_TYPE [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `TOKEN_TYPE` | string | `github` or `jira` |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--token` | string | *(prompted)* | Token value to validate |
| `--email` | string | `""` | Email for Jira Cloud auth |

```bash
rootcoz validate-token github --token ghp_abc123
# ✓ Valid — Token has repo scope

rootcoz validate-token jira --token jira-token --email user@example.com
# ✗ Invalid — Authentication failed
```

---

## push-reportportal

Push rootcoz classifications into Report Portal test items. Requires Report Portal to be enabled on the server. See [Configuring Integrations](configuring-integrations.html) for setup.

```
rootcoz push-reportportal JOB_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `JOB_ID` | string | Job ID to push classifications for |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--child-job-name` | string | — | Child job name (for pipeline child push) |
| `--child-build-number` | int | — | Child build number |

> **Tip:** `push-rp` is a hidden alias for `push-reportportal`.

```bash
rootcoz push-reportportal abc123
# Pushed 12 classification(s) to Report Portal
# Launch ID: 456
```

---

## capabilities

Show which post-analysis automation features the server supports.

```
rootcoz capabilities [OPTIONS]
```

```bash
rootcoz capabilities --json
# {"github_issues": true, "jira_bugs": true, "reportportal": false}
```

---

## ai-models

List available AI models by provider.

```
rootcoz ai-models [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--provider`, `-p` | string | *(all)* | Filter by provider (`cursor`, `claude`, `gemini`) |

```bash
rootcoz ai-models
# cursor (3 models):
# MODEL ID              DISPLAY NAME
# gpt-5.4-xhigh        GPT-5.4 XHigh
# ...

rootcoz ai-models --provider claude
```

---

## jira-projects

List available Jira projects.

```
rootcoz jira-projects [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--query` | string | `""` | Search query to filter projects |
| `--jira-token` | string | *(config)* | Jira token |
| `--jira-email` | string | *(config)* | Jira email |

```bash
rootcoz jira-projects --query "platform"
```

---

## jira-security-levels

List security levels for a Jira project.

```
rootcoz jira-security-levels PROJECT_KEY [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `PROJECT_KEY` | string | Jira project key |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--jira-token` | string | *(config)* | Jira token |
| `--jira-email` | string | *(config)* | Jira email |

```bash
rootcoz jira-security-levels MYPROJ
```

---

## Mentions

### mentionable-users

List users that can be @mentioned in comments.

```
rootcoz mentionable-users [OPTIONS]
```

```bash
rootcoz mentionable-users
# alice
# bob
# charlie
```

### mentions

List your @mentions across all reports.

```
rootcoz mentions [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit`, `-l` | int | `50` | Max mentions to return |
| `--offset`, `-o` | int | `0` | Pagination offset |
| `--unread` | flag | `false` | Show only unread mentions |

```bash
rootcoz mentions --unread
```

### mentions-mark-read

Mark specific mentions as read.

```
rootcoz mentions-mark-read [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--ids` | string | *(required)* | Comma-separated comment IDs |

```bash
rootcoz mentions-mark-read --ids "42,43,44"
```

### mentions-mark-all-read

Mark all mentions as read.

```
rootcoz mentions-mark-all-read [OPTIONS]
```

```bash
rootcoz mentions-mark-all-read
```

---

## reports

Analytics reports. See [Tracking Failure History and Reports](tracking-history.html) for usage context.

All report subcommands share the same filter options:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--team` | string | `""` | Filter by team |
| `--tier` | string | `""` | Filter by tier |
| `--version` | string | `""` | Filter by version |
| `--from` | string | `""` | From date (`YYYY-MM-DD`) |
| `--to` | string | `""` | To date (`YYYY-MM-DD`) |
| `--status` | string | `""` | Filter by job status |
| `--tags` | string | `""` | Comma-separated tags filter |
| `--exclude-tags` | string | `""` | Comma-separated tags to exclude |

### reports totals

Show aggregate totals: jobs, failures, reviewed.

```
rootcoz reports totals [OPTIONS]
```

```bash
rootcoz reports totals --team platform --from 2026-06-01
# Total jobs: 45  Failures: 120  Reviewed: 98
```

### reports overrides

Show classification overrides grouped by original→new classification.

```
rootcoz reports overrides [OPTIONS]
```

```bash
rootcoz reports overrides --from 2026-06-01
# Total overrides: 15
#   CODE ISSUE → PRODUCT BUG: 8
#   INFRASTRUCTURE → CODE ISSUE: 7
```

### reports issues

Show GitHub/Jira issues created from analyses.

```
rootcoz reports issues [OPTIONS]
```

```bash
rootcoz reports issues --team platform --from 2026-06-01
# Total issues created: 12
```

---

## auth

Authentication commands. See [Managing Users and Roles](managing-users.html) for role details.

### auth login

Validate credentials. This does **not** persist a session — use `api_key` in config or `--api-key` / `ROOTCOZ_API_KEY` for persistent auth.

```
rootcoz auth login [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--username`, `-u` | string | *(required)* | Username |
| `--api-key`, `-k` | string | *(required)* | API key |

```bash
rootcoz auth login -u myuser -k sk-abc123
# Logged in as myuser (role: reviewer, admin: False)
```

### auth rotate-key

Rotate your own API key. The new key is displayed once.

```
rootcoz auth rotate-key [OPTIONS]
```

> **Warning:** The new key replaces the old one immediately. Save it before closing the terminal.

```bash
rootcoz auth rotate-key
# Key rotated for: myuser
# ⚠️  Save this API key — you won't see it again!
# API Key: sk-newkey...
```

### auth whoami

Show current authenticated user info.

```
rootcoz auth whoami [OPTIONS]
```

```bash
rootcoz auth whoami
# USERNAME  ROLE      IS ADMIN
# myuser    reviewer  False
```

### auth logout

Logout (clear session).

```
rootcoz auth logout [OPTIONS]
```

```bash
rootcoz auth logout
```

---

## admin

Admin management commands. All admin commands require **admin** role. See [Managing Users and Roles](managing-users.html) for role management details.

### admin users list

List all users (admin and regular).

```
rootcoz admin users list [OPTIONS]
```

```bash
rootcoz admin users list
```

### admin users create

Create a new user with the specified role.

```
rootcoz admin users create USERNAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `USERNAME` | string | Username for the new user |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--role` | string | *(required)* | `viewer`, `reviewer`, `operator`, or `admin` |

```bash
rootcoz admin users create alice --role operator
```

### admin users delete

Delete a user.

```
rootcoz admin users delete USERNAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `USERNAME` | string | Username to delete |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--force`, `-f` | flag | `false` | Skip confirmation prompt |

```bash
rootcoz admin users delete alice --force
```

### admin users rotate-key

Rotate a user's API key.

```
rootcoz admin users rotate-key USERNAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `USERNAME` | string | Username to rotate key for |

```bash
rootcoz admin users rotate-key alice
```

### admin users change-role

Change a user's role. Promoting to admin generates an API key.

```
rootcoz admin users change-role USERNAME ROLE [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `USERNAME` | string | Username to change role for |
| `ROLE` | string | New role: `viewer`, `reviewer`, `operator`, or `admin` |

```bash
rootcoz admin users change-role alice admin
```

### admin users pending

List users awaiting admin approval.

```
rootcoz admin users pending [OPTIONS]
```

```bash
rootcoz admin users pending
```

### admin users approve

Approve a pending user registration.

```
rootcoz admin users approve USERNAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `USERNAME` | string | Username to approve |

```bash
rootcoz admin users approve alice
```

### admin users reject

Reject a pending user registration.

```
rootcoz admin users reject USERNAME [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `USERNAME` | string | Username to reject |

```bash
rootcoz admin users reject spammer
```

---

## admin settings

Manage server settings at runtime. See [Environment Variables and Configuration](environment-variables.html) for the full list of settings.

### admin settings list

List all server settings with current values and sources.

```
rootcoz admin settings list [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--category`, `-c` | string | *(all)* | Filter by category |
| `--reveal` | flag | `false` | Show sensitive values (default: masked) |

```bash
rootcoz admin settings list --category jenkins
rootcoz admin settings list --reveal
```

### admin settings set

Set a server setting (persisted to DB, overrides env var).

```
rootcoz admin settings set KEY VALUE [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `KEY` | string | Setting key (e.g. `jenkins_url` or `JENKINS_URL`) |
| `VALUE` | string | New value |

```bash
rootcoz admin settings set jenkins_url https://jenkins.example.com
```

### admin settings reset

Reset a server setting to its environment variable or default value (removes DB override).

```
rootcoz admin settings reset KEY [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `KEY` | string | Setting key to reset |

```bash
rootcoz admin settings reset jenkins_url
```

### admin settings history

Show server settings change history.

```
rootcoz admin settings history [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--key`, `-k` | string | *(all)* | Filter by setting key |
| `--limit`, `-l` | int | `50` | Max entries to return |

```bash
rootcoz admin settings history --key jenkins_url --limit 10
```

---

## admin token-usage

View AI token usage and costs. Admin only.

```
rootcoz admin token-usage [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--period` | string | — | `today`, `week`, `month`, or `all` |
| `--start-date` | string | — | Start date (`YYYY-MM-DD`) |
| `--end-date` | string | — | End date (`YYYY-MM-DD`) |
| `--provider` | string | — | Filter by AI provider |
| `--model` | string | — | Filter by AI model |
| `--call-type` | string | — | Filter by call type |
| `--group-by` | string | — | Group by: `provider`, `model`, `call_type`, `day`, `week`, `month`, `job` |
| `--job-id` | string | — | Get usage for a specific job |
| `--format` | string | `table` | Output format: `table`, `json`, or `csv` |

When invoked with no options, shows a dashboard summary (today, this week, this month).

```bash
# Dashboard summary
rootcoz admin token-usage

# Usage for this week, grouped by model
rootcoz admin token-usage --period week --group-by model

# CSV export for a date range
rootcoz admin token-usage --start-date 2026-06-01 --end-date 2026-06-22 --format csv

# Per-job token usage
rootcoz admin token-usage --job-id abc123
```

---

## admin-chat

Admin server-wide chat with AI. The admin chat has access to database queries and report generation tools. Requires **admin** role.

### admin-chat send

Send a message to the admin server chat. Polls for up to ~2 minutes for the AI response.

```
rootcoz admin-chat send MESSAGE [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `MESSAGE` | string | Message to send |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--provider`, `-p` | string | *(server default)* | AI provider |
| `--model`, `-m` | string | *(server default)* | AI model |

```bash
rootcoz admin-chat send "What are the top 5 flakiest tests this month?"
```

### admin-chat history

Show admin chat history.

```
rootcoz admin-chat history [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--limit`, `-l` | int | `200` | Maximum messages to return |

```bash
rootcoz admin-chat history
```

### admin-chat clear

Clear admin chat history.

```
rootcoz admin-chat clear [OPTIONS]
```

```bash
rootcoz admin-chat clear
# Cleared 24 messages.
```

### admin-chat save-artifact

Save an HTML file as an admin chat report artifact.

```
rootcoz admin-chat save-artifact HTML_FILE [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `HTML_FILE` | path | Path to the HTML file to upload |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--filename`, `-f` | string | *(input file name)* | Artifact filename |

```bash
rootcoz admin-chat save-artifact report.html --filename "weekly-summary.html"
```

### admin-chat download-artifact

Download an admin chat report artifact.

```
rootcoz admin-chat download-artifact ARTIFACT_ID [OPTIONS]
```

| Argument | Type | Description |
|----------|------|-------------|
| `ARTIFACT_ID` | string | Artifact UUID to download |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--output`, `-o` | string | `report-<id>.html` | Output file path |

```bash
rootcoz admin-chat download-artifact a1b2c3d4 -o summary.html
# Downloaded to summary.html (15234 bytes)
```

---

## config

Manage rootcoz CLI configuration. Does not require a server connection.

### config show

Show current configuration (default when running `rootcoz config` with no subcommand).

```
rootcoz config show
```

```bash
rootcoz config show
# Config file: /home/user/.config/rootcoz/config.toml
# Default server: dev
#
# Servers (2):
#   dev *: http://localhost:8000 user=myuser
#   prod: https://rootcoz.example.com (no-verify-ssl)
```

### config servers

List configured servers.

```
rootcoz config servers [OPTIONS]
```

```bash
rootcoz config servers
rootcoz config servers --json
```

### config completion

Show shell completion setup instructions.

```
rootcoz config completion [SHELL]
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `SHELL` | string | `zsh` | Shell type: `bash` or `zsh` |

```bash
rootcoz config completion zsh
# Add to ~/.zshrc:
# if command -v rootcoz &> /dev/null; then
#   eval "$(rootcoz --show-completion zsh)"
# fi
```

---

## Configuration File Reference

The CLI reads configuration from `$XDG_CONFIG_HOME/rootcoz/config.toml` (defaults to `~/.config/rootcoz/config.toml`). See [Setting Up the CLI](cli-setup.html) for setup instructions.

### File Structure

```toml
[default]
server = "dev"                    # Default server name

[defaults]                         # Shared settings for all servers
username = "myuser"
ai_provider = "claude"

[servers.dev]
url = "http://localhost:8000"
api_key = "sk-abc123"

[servers.prod]
url = "https://rootcoz.example.com"
no_verify_ssl = true
username = "produser"
api_key = "sk-prod456"
```

### Per-Server Config Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | *(required)* | Server URL |
| `username` | string | `""` | Username |
| `api_key` | string | `""` | API key for authentication |
| `no_verify_ssl` | bool | `false` | Disable SSL verification |
| `ai_provider` | string | `""` | Default AI provider |
| `ai_model` | string | `""` | Default AI model |
| `ai_call_timeout` | int | `0` | AI call timeout (0 = server default) |
| `max_concurrent_ai_calls` | int | `0` | Max concurrent AI calls (0 = server default) |
| `jenkins_url` | string | `""` | Jenkins server URL |
| `jenkins_user` | string | `""` | Jenkins username |
| `jenkins_password` | string | `""` | Jenkins password/token |
| `jenkins_ssl_verify` | bool | — | Jenkins SSL verification |
| `jenkins_timeout` | int | `0` | Jenkins API timeout (0 = server default) |
| `tests_repo_url` | string | `""` | Tests repository URL |
| `tests_repo_token` | string | `""` | Private test repo token |
| `jira_url` | string | `""` | Jira instance URL |
| `jira_email` | string | `""` | Jira Cloud email |
| `jira_api_token` | string | `""` | Jira Cloud API token |
| `jira_pat` | string | `""` | Jira Server/DC PAT |
| `jira_token` | string | `""` | Jira token (generic) |
| `jira_project_key` | string | `""` | Jira project key |
| `jira_security_level` | string | `""` | Jira security level name |
| `jira_ssl_verify` | bool | — | Jira SSL verification |
| `jira_max_results` | int | `0` | Max Jira search results (0 = server default) |
| `enable_jira` | bool | — | Enable/disable Jira integration |
| `github_token` | string | `""` | GitHub API token |
| `github_repo_url` | string | `""` | GitHub repository URL |
| `peers` | string | `""` | Peer configs: `provider:model,provider:model` |
| `peer_analysis_max_rounds` | int | `0` | Max peer debate rounds (0 = server default) |
| `additional_repos` | string | `""` | Additional repos: `name:url,name:url` |
| `wait_for_completion` | bool | — | Wait for Jenkins job completion |
| `poll_interval_minutes` | int | `0` | Jenkins poll interval (0 = server default) |
| `max_wait_minutes` | int | `0` | Max wait time (0 = server default) |
| `force` | bool | — | Force analysis on successful builds |

> **Note:** Fields in the `[defaults]` section apply to all servers unless overridden in a specific `[servers.<name>]` section. The `server` field is only valid in `[default]`, not in `[defaults]`.

---

## Output Formats

All commands support `--json` for machine-readable output. Without `--json`, output is displayed as aligned text tables.

```bash
# Table output (default)
rootcoz results list
# JOB ID    STATUS     JENKINS URL                    CREATED
# abc123    completed  https://jenkins.example.com/…  2026-06-22T10:30:00

# JSON output
rootcoz results list --json
# [{"job_id": "abc123", "status": "completed", ...}]
```

The `admin token-usage` command additionally supports `--format csv` for CSV export.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Error (API error, validation failure, connection failure) |

On HTTP 401 errors, the CLI prints a hint about API key authentication. On HTTP 403 errors, it suggests contacting an administrator for role elevation.

---

## Role Requirements

Commands require minimum roles as defined in the RBAC system. See [Managing Users and Roles](managing-users.html) for full details.

| Role | Permitted Actions |
|------|------------------|
| **viewer** | `results list`, `results show`, `results dashboard`, `status`, `health`, `history *`, `classifications list`, `comments list`, `config *` |
| **reviewer** | All viewer actions, plus: `comments add/delete`, `classify`, `override-classification`, `override-pattern`, `results set-reviewed`, `chat *`, `mentions *` |
| **operator** | All reviewer actions, plus: `analyze`, `re-analyze`, `abort`, `results delete` (own jobs), `failure re-analyze` |
| **admin** | All operator actions, plus: `admin *`, `admin-chat *`, `results delete` (any job) |

## Related Pages

- [Setting Up the CLI](cli-setup.html)
- [CLI Recipes](cli-recipes.html)
- [REST API Reference](api-reference.html)
- [Analyzing Test Failures](analyzing-failures.html)
- [Managing Users and Roles](managing-users.html)