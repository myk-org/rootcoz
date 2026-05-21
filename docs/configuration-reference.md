# Configuration Reference

> **Note:** This page covers configuration keys and request-body overrides only. For CLI flag syntax that consumes them, see [CLI Command Reference](cli-command-reference.html).

## Server Environment Variables

> **Note:** Server settings are read from process environment and from `.env` when present.

### Core Process and Storage

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `PORT` | integer | `8000` | Port used by the built-in server entrypoint. Must be `1`-`65535`. |
| `DEBUG` | boolean | `false` | Enables autoreload when the built-in server entrypoint starts Uvicorn. |
| `LOG_LEVEL` | string | `INFO` | Log level used across server modules. |
| `DB_PATH` | path | `/data/results.db` | SQLite database file. The file is created if missing; the parent directory must already exist. |
| `ROOTCOZ_ENCRYPTION_KEY` | string | `unset` | Secret used for at-rest encryption and HMAC hashing of stored delegated admin API keys. When unset, rootcoz creates and reuses `$XDG_DATA_HOME/rootcoz/.encryption_key` or `~/.local/share/rootcoz/.encryption_key`. |
| `PUBLIC_BASE_URL` | URL | `unset` | Trusted external base URL used for `result_url`, issue-link rendering, and Report Portal push links. A trailing `/` is stripped. When unset, server-generated links stay relative. |
| `METADATA_RULES_FILE` | path | `unset` | YAML or JSON metadata-rules file. Missing or invalid files load as no rules. Rules are cached for the process lifetime. |

```bash
export PORT=8000
export DB_PATH=/var/lib/rootcoz/results.db
export ROOTCOZ_ENCRYPTION_KEY=change-me
export PUBLIC_BASE_URL=https://rootcoz.example.com
export LOG_LEVEL=INFO
export METADATA_RULES_FILE=/etc/rootcoz/metadata-rules.yaml
```

Effect: These variables set the server bind port, persistent data location, secret source, public link base, and metadata-rules source.

> **Warning:** Rotating `ROOTCOZ_ENCRYPTION_KEY` invalidates encrypted stored credentials and delegated admin API-key hashes. Re-save stored tokens and reissue delegated admin API keys after rotation.

### Jenkins and Repository Defaults

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JENKINS_URL` | URL | `unset` | Default Jenkins base URL for `POST /analyze`. |
| `JENKINS_USER` | string | `unset` | Default Jenkins username. |
| `JENKINS_PASSWORD` | string | `unset` | Default Jenkins password or API token. |
| `JENKINS_SSL_VERIFY` | boolean | `true` | Default TLS verification setting for Jenkins requests. |
| `JENKINS_TIMEOUT` | integer | `30` | Default Jenkins API timeout in seconds. |
| `JENKINS_ARTIFACTS_MAX_SIZE_MB` | integer | `500` | Maximum combined downloaded artifact size in MB. |
| `GET_JOB_ARTIFACTS` | boolean | `true` | Downloads Jenkins build artifacts into the AI analysis context. |
| `WAIT_FOR_COMPLETION` | boolean | `true` | Waits for the Jenkins build to finish before analysis begins. |
| `POLL_INTERVAL_MINUTES` | integer | `2` | Minutes between Jenkins status polls while waiting. |
| `MAX_WAIT_MINUTES` | integer | `0` | Maximum wait time for Jenkins completion. `0` means no limit. |
| `FORCE_ANALYSIS` | boolean | `false` | Analyzes successful builds instead of returning early. |
| `TESTS_REPO_URL` | string | `unset` | Default tests repository. Supports `url:ref` syntax such as `https://github.com/org/tests:develop`. |
| `TESTS_REPO_TOKEN` | string | `unset` | Default token for cloning a private tests repository. |

```bash
export JENKINS_URL=https://jenkins.example.com
export JENKINS_USER=ci-bot
export JENKINS_PASSWORD=your-jenkins-token
export JENKINS_SSL_VERIFY=true
export JENKINS_TIMEOUT=30
export JENKINS_ARTIFACTS_MAX_SIZE_MB=500
export GET_JOB_ARTIFACTS=true
export WAIT_FOR_COMPLETION=true
export POLL_INTERVAL_MINUTES=2
export MAX_WAIT_MINUTES=0
export FORCE_ANALYSIS=false
export TESTS_REPO_URL=https://github.com/acme/tests:develop
export TESTS_REPO_TOKEN=your-repo-token
```

Effect: These values become the default Jenkins connection, wait behavior, artifact behavior, and tests-repo context for `POST /analyze`.

### AI and Peer-Analysis Defaults

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `AI_PROVIDER` | string | `unset` | Default AI provider. Supported values are `claude`, `gemini`, and `cursor`. |
| `AI_MODEL` | string | `unset` | Default AI model identifier. |
| `AI_CLI_TIMEOUT` | integer | `10` | Default AI CLI timeout in minutes. |
| `MAX_CONCURRENT_AI_CALLS` | integer | `3` | Maximum concurrent AI calls for one analysis request. |
| `PEER_AI_CONFIGS` | string | `unset` | Default peer-analysis list in `provider:model,provider:model` format. |
| `PEER_ANALYSIS_MAX_ROUNDS` | integer | `3` | Maximum debate rounds for peer analysis. Valid range is `1`-`10`. |
| `ADDITIONAL_REPOS` | string | `unset` | Default extra repositories for AI context in `name:url[:ref][@token],...` format. Duplicate names are rejected. |

```bash
export AI_PROVIDER=claude
export AI_MODEL=claude-opus-4-6
export AI_CLI_TIMEOUT=10
export MAX_CONCURRENT_AI_CALLS=3
export PEER_AI_CONFIGS=cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro
export PEER_ANALYSIS_MAX_ROUNDS=3
export ADDITIONAL_REPOS=infra:https://github.com/acme/infra:develop@YOUR_TOKEN,product:https://github.com/acme/product
```

Effect: These values become the default provider/model pair, AI execution limits, peer-review configuration, and extra repository context for analysis requests.

> **Note:** If `AI_PROVIDER` or `AI_MODEL` is unset, the request body must supply the missing field.

### Jira Analysis and Jira Issue Settings

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `JIRA_URL` | URL | `unset` | Jira base URL. |
| `JIRA_EMAIL` | string | `unset` | Jira Cloud email. When set, rootcoz uses Cloud auth handling. |
| `JIRA_API_TOKEN` | string | `unset` | Jira Cloud API token. |
| `JIRA_PAT` | string | `unset` | Jira Server/Data Center personal access token. |
| `JIRA_PROJECT_KEY` | string | `unset` | Default Jira project key for duplicate search and Jira issue creation. |
| `JIRA_SSL_VERIFY` | boolean | `true` | TLS verification setting for Jira requests. |
| `JIRA_MAX_RESULTS` | integer | `5` | Maximum Jira duplicate-search results returned during analysis. |
| `ENABLE_JIRA` | boolean | `auto` | Analysis-time Jira toggle. When unset, Jira enrichment runs only when `JIRA_URL`, credentials, and `JIRA_PROJECT_KEY` are configured. |
| `ENABLE_JIRA_ISSUES` | boolean | `enabled` | Jira issue preview/create toggle. Independent of `ENABLE_JIRA`. Set `false` to disable Jira issue creation. |

```bash
export JIRA_URL=https://your-team.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=your-jira-token
export JIRA_PROJECT_KEY=PROJ
export JIRA_SSL_VERIFY=true
export JIRA_MAX_RESULTS=5
export ENABLE_JIRA=true
export ENABLE_JIRA_ISSUES=true
```

Effect: These values control Jira duplicate search during analysis and Jira issue preview/create availability.

> **Note:** Jira auth mode is selected from `JIRA_EMAIL`. When `JIRA_EMAIL` is set, rootcoz uses Cloud credentials and prefers `JIRA_API_TOKEN`, then `JIRA_PAT`. When `JIRA_EMAIL` is unset, rootcoz uses Server/Data Center credentials and prefers `JIRA_PAT`, then `JIRA_API_TOKEN`.

### GitHub and Report Portal Settings

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | string | `unset` | Server GitHub token for issue creation, feedback, and analysis-time GitHub lookups when a request does not override it. |
| `ENABLE_GITHUB_ISSUES` | boolean | `auto` | GitHub issue preview/create toggle. When unset, server-side GitHub issue creation is available when both `TESTS_REPO_URL` and `GITHUB_TOKEN` are configured. Set `false` to disable GitHub issue creation and feedback endpoints. |
| `REPORTPORTAL_URL` | URL | `unset` | Report Portal base URL. |
| `REPORTPORTAL_API_TOKEN` | string | `unset` | Report Portal API token. |
| `REPORTPORTAL_PROJECT` | string | `unset` | Report Portal project name. |
| `REPORTPORTAL_VERIFY_SSL` | boolean | `true` | TLS verification setting for Report Portal requests. |
| `ENABLE_REPORTPORTAL` | boolean | `auto` | Report Portal toggle. When unset, integration is enabled only when URL, token, and project are all configured. |

```bash
export GITHUB_TOKEN=ghp_your_token
export ENABLE_GITHUB_ISSUES=true
export REPORTPORTAL_URL=https://reportportal.example.com
export REPORTPORTAL_API_TOKEN=rp_token
export REPORTPORTAL_PROJECT=qe
export REPORTPORTAL_VERIFY_SSL=true
export ENABLE_REPORTPORTAL=true
```

Effect: These values control GitHub issue creation, feedback availability, and Report Portal push support.

> **Note:** Report Portal push also requires `PUBLIC_BASE_URL` so rootcoz can generate absolute report links.

### Access Control and Cookie Settings

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `ADMIN_KEY` | string | `unset` | Bootstrap superuser secret. The reserved `admin` user can log in with this value, and the same value is accepted in `Authorization: Bearer ...`. |
| `ALLOWED_USERS` | string | `empty` | Comma-separated usernames allowed to create or modify data. Empty keeps open access. Matching is case-insensitive, and admin users bypass the list. |
| `SECURE_COOKIES` | boolean | `true` | Sets the `Secure` cookie attribute on `rootcoz_session` and `rootcoz_username`. |
| `TRUST_PROXY_HEADERS` | boolean | `false` | Enables trusted-proxy user identification from `X-Forwarded-User` and syncs the `rootcoz_username` cookie from that header. |

```bash
export ADMIN_KEY=your-admin-bootstrap-key
export ALLOWED_USERS=alice,bob,carol
export SECURE_COOKIES=true
export TRUST_PROXY_HEADERS=false
```

Effect: These variables control bootstrap admin access, non-admin write permissions, and browser/session cookie behavior.

> **Warning:** Enable `TRUST_PROXY_HEADERS` only when `X-Forwarded-User` is injected by infrastructure you trust.

### Web Push and Alert Delivery

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `VAPID_PUBLIC_KEY` | string | `unset` | Web Push public key. |
| `VAPID_PRIVATE_KEY` | string | `unset` | Web Push private key. |
| `VAPID_CLAIM_EMAIL` | string | `mailto:noreply@rootcoz.local` | Contact email used in VAPID claims. |
| `SLACK_WEBHOOK_URL` | URL | `unset` | Slack incoming-webhook URL for monitoring alerts. |
| `SMTP_HOST` | string | `unset` | SMTP host for email alerts. |
| `SMTP_PORT` | integer | `587` | SMTP port for email alerts. |
| `SMTP_USER` | string | `unset` | SMTP username. |
| `SMTP_PASSWORD` | string | `unset` | SMTP password. |
| `SMTP_FROM` | string | `SMTP_USER` or `rootcoz@<SMTP_HOST>` | Sender address for alert email. |
| `ALERT_EMAIL_TO` | string | `unset` | Recipient address for alert email. |

```bash
export VAPID_PUBLIC_KEY=your-public-key
export VAPID_PRIVATE_KEY=your-private-key
export VAPID_CLAIM_EMAIL=admin@example.com
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587
export SMTP_USER=rootcoz@example.com
export SMTP_PASSWORD=mail-password
export ALERT_EMAIL_TO=alerts@example.com
```

Effect: These values enable browser push notifications and optional Slack/email alert delivery.

> **Note:** If both `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` are unset, rootcoz auto-generates and reuses `.vapid_keys.json` next to `DB_PATH`, or under `$XDG_DATA_HOME/rootcoz/` when `DB_PATH` is unset.


> **Warning:** Set both `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` together. A partial configuration is treated as invalid and the server falls back to generated keys.

## CLI `config.toml`

### File Location and Section Layout

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | path | `$XDG_CONFIG_HOME/rootcoz/config.toml` or `~/.config/rootcoz/config.toml` | Location of the CLI profile file. |
| `[default].server` | string | `unset` | Default profile name used when `--server` is omitted. |
| `[defaults]` | table | empty | Shared values merged into every `[servers.<name>]` entry. |
| `[servers.<name>]` | table | none | Named server profile. `url` must be present after `[defaults]` and profile values are merged. |

```toml
[default]
server = "prod"

[defaults]
username = "alice"
no_verify_ssl = false

[servers.dev]
url = "http://localhost:8000"

[servers.prod]
url = "https://rootcoz.example.com"
```

Effect: The CLI selects `[default].server` when no explicit profile is chosen, merges `[defaults]` into the selected server, and then uses the merged profile for matching commands.

> **Note:** `[defaults].server` is invalid. Only `[default].server` selects the default profile.

### Connection and Authentication Keys

> **Note:** The keys below are valid in both `[defaults]` and `[servers.<name>]`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | required after merge | RootCoz server base URL for the profile. |
| `username` | string | empty | Default CLI username used for comments, reviews, and other user-attributed requests. |
| `no_verify_ssl` | boolean | `false` | Disables TLS verification for the CLI's HTTP connection to the rootcoz server. |
| `api_key` | string | empty | Bearer token sent on CLI requests when `--api-key` is not provided. |

```toml
[servers.prod]
url = "https://rootcoz.example.com"
username = "alice"
no_verify_ssl = false
api_key = "rootcoz_example_admin_key"
```

Effect: These keys set the CLI's server target, default user identity, TLS behavior, and bearer token.

### Analysis Default Keys

> **Note:** The keys below are valid in both `[defaults]` and `[servers.<name>]`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `jenkins_url` | string | empty | Default `--jenkins-url` value for `rootcoz analyze`. |
| `jenkins_user` | string | empty | Default Jenkins username for `rootcoz analyze`. |
| `jenkins_password` | string | empty | Default Jenkins password or token for `rootcoz analyze`. |
| `jenkins_ssl_verify` | boolean | unset | Default Jenkins TLS verification override. |
| `jenkins_timeout` | integer | `0` | Default Jenkins timeout override. `0` means the CLI sends no override. |
| `tests_repo_url` | string | empty | Default tests repo URL for `rootcoz analyze`. |
| `tests_repo_token` | string | empty | Default tests repo token for `rootcoz analyze`. |
| `ai_provider` | string | empty | Default AI provider for `rootcoz analyze`. |
| `ai_model` | string | empty | Default AI model for `rootcoz analyze`. |
| `ai_cli_timeout` | integer | `0` | Default AI timeout override. `0` means the CLI sends no override. |
| `max_concurrent_ai_calls` | integer | `0` | Default concurrency override. `0` means the CLI sends no override. |
| `peers` | string | empty | Default peer-analysis list in `provider:model,provider:model` format. |
| `peer_analysis_max_rounds` | integer | `0` | Default peer-round override. `0` means the CLI sends no override. |
| `additional_repos` | string | empty | Default extra-repo list in `name:url[:ref][@token],...` format. |
| `wait_for_completion` | boolean | unset | Default wait behavior for `rootcoz analyze`. |
| `poll_interval_minutes` | integer | `0` | Default poll-interval override. `0` means the CLI sends no override. |
| `max_wait_minutes` | integer | `0` | Default max-wait override. `0` means the CLI sends no override. |
| `force` | boolean | unset | Default successful-build analysis override for `rootcoz analyze`. |

```toml
[defaults]
jenkins_url = "https://jenkins.example.com"
jenkins_user = "ci-bot"
tests_repo_url = "https://github.com/acme/tests"
ai_provider = "claude"
ai_model = "claude-opus-4-6"
peers = "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro"
additional_repos = "infra:https://github.com/acme/infra:develop@YOUR_TOKEN"
wait_for_completion = true

[servers.prod]
url = "https://rootcoz.example.com"
max_concurrent_ai_calls = 5
force = true
```

Effect: When `rootcoz analyze` runs without explicit flags, the CLI fills matching request-body fields from the selected profile. Explicit CLI flags still override the profile.

> **Note:** In `config.toml`, `0` means "do not send a CLI override" for `ai_cli_timeout`, `max_concurrent_ai_calls`, `jenkins_timeout`, `jira_max_results`, `poll_interval_minutes`, `max_wait_minutes`, and `peer_analysis_max_rounds`. This is different from request-body `max_wait_minutes: 0`, which means "no limit".

### Tracker Default Keys

> **Note:** The keys below are valid in both `[defaults]` and `[servers.<name>]`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `jira_url` | string | empty | Default Jira base URL for analysis and Jira-related CLI commands. |
| `jira_email` | string | empty | Default Jira Cloud email. |
| `jira_api_token` | string | empty | Default Jira Cloud API token for `rootcoz analyze`. |
| `jira_pat` | string | empty | Default Jira Server/Data Center token for `rootcoz analyze`. |
| `jira_token` | string | empty | Default generic Jira token for `rootcoz jira-projects`, `rootcoz jira-security-levels`, `rootcoz preview-issue`, and `rootcoz create-issue`. |
| `jira_project_key` | string | empty | Default Jira project key for analysis and Jira issue commands. |
| `jira_security_level` | string | empty | Default Jira security level name for Jira issue preview/create commands. |
| `jira_ssl_verify` | boolean | unset | Default Jira TLS verification override. |
| `jira_max_results` | integer | `0` | Default Jira duplicate-search override. `0` means the CLI sends no override. |
| `enable_jira` | boolean | unset | Default Jira-enrichment toggle for `rootcoz analyze`. |
| `github_token` | string | empty | Default GitHub token for analysis and GitHub issue commands. |
| `github_repo_url` | string | empty | Default GitHub repository URL override for GitHub issue preview/create commands. |

```toml
[servers.prod]
url = "https://rootcoz.example.com"
jira_url = "https://your-team.atlassian.net"
jira_email = "you@example.com"
jira_token = "your-jira-token"
jira_project_key = "PROJ"
jira_security_level = "Internal"
enable_jira = true
github_token = "ghp_your_token"
github_repo_url = "https://github.com/acme/tests"
```

Effect: These keys fill tracker-related CLI request fields when matching flags are omitted.

## Authentication Modes

> **Note:** `rootcoz_username` and `X-Forwarded-User` identify non-admin users. Admin privileges come from `rootcoz_session` or `Authorization: Bearer ...`.

### Username Cookie Mode

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `rootcoz_username` | cookie | absent | Non-admin username used for request attribution. The reserved username `admin` is ignored in this mode. |

```bash
curl https://rootcoz.example.com/api/auth/me \
  --cookie "rootcoz_username=alice"
```

Effect: The request runs as `alice` with `role=user` and `is_admin=false`. Admin-only routes still require session or Bearer auth, and non-admin write routes can still be limited by `ALLOWED_USERS`.

### Trusted-Proxy Header Mode

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `TRUST_PROXY_HEADERS` | server boolean | `false` | Enables trusted-proxy user identification. |
| `X-Forwarded-User` | request header | absent | Username supplied by the trusted proxy. The reserved username `admin` is ignored. |

```bash
curl https://rootcoz.example.com/api/auth/me \
  -H "X-Forwarded-User: alice"
```

Effect: When `TRUST_PROXY_HEADERS=true`, rootcoz sets `username` from `X-Forwarded-User`, returns `role=user`, and writes a matching `rootcoz_username` cookie on the response. Existing session auth takes precedence.

### Session Login Mode

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `username` | JSON string | required | Login username. `admin` with `ADMIN_KEY` uses the bootstrap superuser path; stored user or admin API keys can also be used with their matching username. |
| `api_key` | JSON string | required | API key for the matching user or bootstrap admin. |
| `rootcoz_session` | cookie | set on success | HTTP-only session cookie with fixed 8-hour TTL. |

```bash
curl -c cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","api_key":"'"$ADMIN_KEY"'"}' \
  https://rootcoz.example.com/api/auth/login
```

Effect: Successful login returns JSON with `username`, `role`, and `is_admin`, and sets `rootcoz_session` plus `rootcoz_username`. Active sessions are renewed when less than half of the 8-hour TTL remains.

### Bearer Token Mode

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `Authorization` | header | absent | `Bearer <token>`. Accepts `ADMIN_KEY` or any stored API key. |

```bash
curl https://rootcoz.example.com/api/auth/me \
  -H "Authorization: Bearer $ADMIN_KEY"
```

Effect: `ADMIN_KEY` or an admin user's API key gives admin access. A non-admin user's API key sets `username` and `role=user` without admin privileges.

## Per-request Analysis Overrides

> **Note:** Shared fields below are accepted by `POST /analyze`, `POST /analyze-failures`, and `POST /re-analyze/{job_id}`. On `POST /re-analyze/{job_id}`, omitted fields come from the original job's stored request parameters.

### Shared Override Fields

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `tests_repo_url` | string | `null` | Overrides `TESTS_REPO_URL` for this request. Supports `url:ref` syntax. |
| `tests_repo_token` | string | `null` | Overrides `TESTS_REPO_TOKEN` for this request. |
| `ai_provider` | string | `null` | Overrides `AI_PROVIDER`. Supported values are `claude`, `gemini`, and `cursor`. |
| `ai_model` | string | `null` | Overrides `AI_MODEL`. |
| `enable_jira` | boolean | `null` | Per-request Jira-enrichment toggle. `false` disables Jira duplicate search for this request even if the server is configured. |
| `ai_cli_timeout` | integer | `null` | Overrides `AI_CLI_TIMEOUT` in minutes. Must be greater than `0`. |
| `max_concurrent_ai_calls` | integer | `null` | Overrides `MAX_CONCURRENT_AI_CALLS`. Must be greater than `0`. |
| `jira_url` | string | `null` | Overrides `JIRA_URL` for this request. |
| `jira_email` | string | `null` | Overrides `JIRA_EMAIL` for this request. |
| `jira_api_token` | string | `null` | Overrides `JIRA_API_TOKEN` for this request. |
| `jira_pat` | string | `null` | Overrides `JIRA_PAT` for this request. |
| `jira_project_key` | string | `null` | Overrides `JIRA_PROJECT_KEY` for this request. |
| `jira_ssl_verify` | boolean | `null` | Overrides `JIRA_SSL_VERIFY` for this request. |
| `jira_max_results` | integer | `null` | Overrides `JIRA_MAX_RESULTS`. Must be greater than `0`. |
| `raw_prompt` | string | `null` | Additional AI instructions for this request. When set, this request's value replaces the repo-level `JOB_INSIGHT_PROMPT.md` default. |
| `github_token` | string | `null` | Overrides `GITHUB_TOKEN` for analysis-time GitHub lookups. |
| `peer_ai_configs` | array | `null` | Structured peer list. Omit to inherit the server default; send `[]` to disable peers for this request. |
| `peer_analysis_max_rounds` | integer | `3` | Peer-analysis debate rounds. Valid range is `1`-`10`. The server only treats it as an override when the field is explicitly present in the JSON body. |
| `additional_repos` | array | `null` | Structured extra-repo list. Omit to inherit the server default; send `[]` to disable extra repos for this request. |

#### `peer_ai_configs[]` Entries

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `ai_provider` | string | required | Peer provider. Supported values are `claude`, `gemini`, and `cursor`. |
| `ai_model` | string | required | Peer model identifier. |

#### `additional_repos[]` Entries

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Unique repository label used as the clone subdirectory name. Cannot contain `/`, `\`, `..`, a leading `.`, or reserved names. |
| `url` | URL | required | Repository clone URL. |
| `ref` | string | empty | Branch or tag to check out. |
| `token` | string | `null` | Per-repository clone token for private repositories. |

```json
{
  "ai_provider": "gemini",
  "ai_model": "gemini-2.5-pro",
  "enable_jira": false,
  "peer_ai_configs": [],
  "additional_repos": [
    {
      "name": "infra",
      "url": "https://github.com/acme/infra",
      "ref": "develop"
    }
  ]
}
```

Effect: These fields override shared analysis behavior for one request only. The same fields can be sent to `POST /re-analyze/{job_id}` to change selected settings while keeping the rest of the original job configuration.

> **Note:** `peer_ai_configs=[]` and `additional_repos=[]` explicitly turn off a server default for one request. Omitting the field keeps the server default.

### `POST /analyze`-only Fields

> **Note:** The schema defaults below are shown for lookup. `wait_for_completion`, `poll_interval_minutes`, `max_wait_minutes`, and `force` only override the server setting when the field is explicitly present in the JSON body.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `jenkins_url` | string | `null` | Overrides `JENKINS_URL` for this request. |
| `jenkins_user` | string | `null` | Overrides `JENKINS_USER` for this request. |
| `jenkins_password` | string | `null` | Overrides `JENKINS_PASSWORD` for this request. |
| `jenkins_ssl_verify` | boolean | `null` | Overrides `JENKINS_SSL_VERIFY` for this request. |
| `jenkins_timeout` | integer | `null` | Overrides `JENKINS_TIMEOUT` in seconds. Must be greater than `0`. |
| `jenkins_artifacts_max_size_mb` | integer | `null` | Overrides `JENKINS_ARTIFACTS_MAX_SIZE_MB`. Must be greater than `0`. |
| `get_job_artifacts` | boolean | `null` | Overrides `GET_JOB_ARTIFACTS` for this request. |
| `wait_for_completion` | boolean | `true` | Waits for Jenkins completion before analysis. Only overrides the server when the field is explicitly present. |
| `poll_interval_minutes` | integer | `2` | Poll interval while waiting. Must be greater than `0`. Only overrides the server when the field is explicitly present. |
| `max_wait_minutes` | integer | `0` | Maximum wait time. `0` means no limit. Only overrides the server when the field is explicitly present. |
| `force` | boolean | `false` | Overrides `FORCE_ANALYSIS` for this request. Only overrides the server when the field is explicitly present. |

```json
{
  "job_name": "folder/nightly-suite",
  "build_number": 123,
  "jenkins_url": "https://jenkins.example.com",
  "jenkins_user": "ci-bot",
  "jenkins_password": "YOUR_JENKINS_TOKEN",
  "wait_for_completion": true,
  "poll_interval_minutes": 5,
  "max_wait_minutes": 0,
  "force": true,
  "get_job_artifacts": true
}
```

Effect: These fields control Jenkins connectivity, artifact download, waiting behavior, and successful-build analysis for the queued job.

> **Note:** In request bodies, `max_wait_minutes: 0` means "no limit". This differs from `config.toml`, where `max_wait_minutes = 0` means "do not send a CLI override".

## Related Pages

- [CLI Command Reference](cli-command-reference.html)
- [API Endpoint Reference](api-endpoint-reference.html)
- [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html)
- [Connecting GitHub, Jira, and Report Portal](connecting-github-jira-and-report-portal.html)
- [Managing Users and API Keys](managing-users-and-api-keys.html)