# Configuration and Environment Reference

Server configuration uses environment variables on the server, profile fields in the `jji` client config file, and per-request override fields on analysis endpoints.

## Configuration Precedence

### Server-side analysis resolution

| Source | Scope | Effect |
| --- | --- | --- |
| Request body override fields | `POST /analyze`, `POST /analyze-failures` | Overrides server defaults for one request only. |
| Server environment variables | FastAPI server process | Supplies the server-wide defaults used when a request omits an override. |
| Application defaults | Built into the code | Used only when neither the request nor the environment provides a value. |

```bash
export AI_PROVIDER=claude
export AI_MODEL=sonnet
export JENKINS_TIMEOUT=30
```

```json
{
  "job_name": "folder/job-name",
  "build_number": 1042,
  "ai_provider": "gemini",
  "ai_model": "gemini-2.5-pro",
  "jenkins_timeout": 60
}
```

### CLI resolution

| Source | Scope | Effect |
| --- | --- | --- |
| CLI flags and `JJI_*` environment variables | Current `jji` invocation | Highest-priority client-side settings. |
| `config.toml` selected server profile | Current `jji` invocation | Supplies defaults for that named server. |
| `config.toml` `[defaults]` | Current `jji` invocation | Supplies shared defaults merged into every named server. |
| Remote server configuration | Server-side | Still applies after the CLI sends the request. |

> **Note:** If `--server` or `JJI_SERVER` is a full `http://` or `https://` URL, `jji` does not load any profile defaults from `config.toml` for that connection.

```bash
export JJI_SERVER=prod
export JJI_USERNAME=alice
jji --api-key "jji_example_admin_key" results list
```

## Server Environment Variables

> **Note:** Optional string settings treat blank or whitespace-only values as unset.

### Storage and Runtime

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `DB_PATH` | path | `/data/results.db` | Filesystem location of the SQLite database file. | The directory must exist and be writable; health checks report a database error if access fails. |
| `JJI_ENCRYPTION_KEY` | string | auto-generated file key | Secret used for encrypting sensitive values at rest and for hashing delegated admin API keys. | If unset, JJI creates and reuses a persistent key file under `$XDG_DATA_HOME/jji/.encryption_key` or `~/.local/share/jji/.encryption_key`. |
| `PORT` | integer | `8000` | HTTP port for the FastAPI server. | Must be between `1` and `65535`; the same value is used for Uvicorn and internal self-calls made by the AI workflow. |
| `DEBUG` | boolean | `false` | Debug mode toggle. | When `true`, the Python entry point enables Uvicorn reload. |
| `LOG_LEVEL` | string | `INFO` | Log level passed to application loggers. | Controls server log verbosity across the app. |
| `DEV_MODE` | boolean | `false` | Container development-mode toggle. | In the container entrypoint, `true` starts the Vite dev server on `5173` and adds Uvicorn reload flags. |
| `PUBLIC_BASE_URL` | URL | unset | Trusted public origin for absolute links. | When unset, JJI emits relative links and does not trust request host headers. |
| `XDG_DATA_HOME` | path | `~/.local/share` | Base directory for generated persistent data files. | Used for the fallback encryption key file and fallback VAPID key file. |
| `XDG_CONFIG_HOME` | path | `~/.config` | Base directory for user configuration files. | Sets the base path for `jji` config at `$XDG_CONFIG_HOME/jji/config.toml`. |

```bash
export DB_PATH="/srv/jji/state/results.sqlite3"
export JJI_ENCRYPTION_KEY="replace-with-a-stable-secret"
export PORT=8080
export PUBLIC_BASE_URL="https://jji.example.com"
export LOG_LEVEL=DEBUG
```

See [Copy Common Deployment Recipes](copy-common-deployment-recipes.html) for deployment examples.

### Access, Sessions, and Identity

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `ADMIN_KEY` | string | unset | Bootstrap secret for the built-in `admin` login path. | Enables initial admin sign-in with username `admin` at `/api/auth/login`. |
| `ALLOWED_USERS` | comma-separated string | unset | Allow list for users who can create or modify data. | Empty means open access; usernames are normalized to lowercase; admins bypass the list. |
| `SECURE_COOKIES` | boolean | `true` | Cookie security toggle. | Adds the `Secure` attribute to `jji_session` and `jji_username` cookies. |
| `TRUST_PROXY_HEADERS` | boolean | `false` | Reverse-proxy identity toggle. | Trusts `X-Forwarded-User` and mirrors it into the current request user and the `jji_username` cookie; `admin` is reserved and rejected from the proxy header. |

```bash
export ADMIN_KEY="replace-with-a-bootstrap-secret"
export ALLOWED_USERS="alice,bob,release-bot"
export SECURE_COOKIES=false
export TRUST_PROXY_HEADERS=true
```

### Jenkins Defaults

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `JENKINS_URL` | URL | unset | Default Jenkins base URL. | Used when `/analyze` omits `jenkins_url`. |
| `JENKINS_USER` | string | unset | Default Jenkins username. | Used when `/analyze` omits `jenkins_user`. |
| `JENKINS_PASSWORD` | string | unset | Default Jenkins password or API token. | Used when `/analyze` omits `jenkins_password`. |
| `JENKINS_SSL_VERIFY` | boolean | `true` | Jenkins TLS verification toggle. | `false` allows self-signed certificates. |
| `JENKINS_TIMEOUT` | integer | `30` | Jenkins API timeout in seconds. | Used for Jenkins API requests when the request omits `jenkins_timeout`. |
| `TESTS_REPO_URL` | string | unset | Default tests repository URL. | Used for repository context when the request omits `tests_repo_url`; accepts an optional `:ref` suffix. |
| `WAIT_FOR_COMPLETION` | boolean | `true` | Default wait behavior for `/analyze`. | When `true`, a queued analysis can remain in `waiting` until Jenkins finishes the build. |
| `POLL_INTERVAL_MINUTES` | integer | `2` | Default poll interval while waiting. | Controls how often JJI rechecks Jenkins status. |
| `MAX_WAIT_MINUTES` | integer | `0` | Default maximum wait duration. | `0` means no wait limit. |
| `FORCE_ANALYSIS` | boolean | `false` | Default force-analysis toggle. | Allows analysis to run even when Jenkins reports `SUCCESS`. |
| `GET_JOB_ARTIFACTS` | boolean | `true` | Default artifact-download toggle. | When `true`, JJI downloads build artifacts for analysis context. |
| `JENKINS_ARTIFACTS_MAX_SIZE_MB` | integer | `500` | Maximum total artifact download size, in MB. | Caps artifact collection per job. |

```bash
export JENKINS_URL="https://jenkins.example.com"
export JENKINS_USER="ci-bot"
export JENKINS_PASSWORD="replace-with-a-token"
export JENKINS_SSL_VERIFY=false
export TESTS_REPO_URL="https://gitlab.internal:8443/qa/tests:main"
export WAIT_FOR_COMPLETION=true
export POLL_INTERVAL_MINUTES=3
export MAX_WAIT_MINUTES=45
export GET_JOB_ARTIFACTS=true
export JENKINS_ARTIFACTS_MAX_SIZE_MB=750
```

### AI Defaults

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `AI_PROVIDER` | string | unset | Default AI provider. Valid values: `claude`, `gemini`, `cursor`. | Required unless the request body provides `ai_provider`. |
| `AI_MODEL` | string | unset | Default AI model identifier. | Required unless the request body provides `ai_model`. |
| `AI_CLI_TIMEOUT` | integer | `10` | Timeout for each provider CLI call, in minutes. | Applies to AI subprocess execution. |
| `PEER_AI_CONFIGS` | string | unset | Default peer set in `provider:model,provider:model` format. | Enables peer analysis when the request omits `peer_ai_configs`; blank disables the default. |
| `PEER_ANALYSIS_MAX_ROUNDS` | integer | `3` | Maximum peer debate rounds. | Must be between `1` and `10`. |
| `ADDITIONAL_REPOS` | string | unset | Default extra repo list in `name:url`, `name:url:ref`, or `name:url:ref@token` format. | Clones extra repositories beside the tests repo when the request omits `additional_repos`. Duplicate names are rejected. |

```bash
export AI_PROVIDER=claude
export AI_MODEL=sonnet
export AI_CLI_TIMEOUT=20
export PEER_AI_CONFIGS="gemini:gemini-2.5-pro,cursor:gpt-5.4-xhigh"
export PEER_ANALYSIS_MAX_ROUNDS=5
export ADDITIONAL_REPOS="product:https://github.com/acme/product:release-4.18,infra:https://github.com/acme/infra@ghp_example"
```

> **Note:** `PEER_AI_CONFIGS` and `ADDITIONAL_REPOS` are parsed when a request resolves them. Invalid values fail that analysis request instead of being ignored.

### Provider CLI Environment

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | string | unset | Authentication token for the Claude CLI. | Read by the external Claude CLI process. |
| `CLAUDE_CODE_USE_VERTEX` | string | unset | Vertex toggle for the Claude CLI. | Set to `1` to use Vertex-backed Claude auth. |
| `CLOUD_ML_REGION` | string | unset | Google Cloud region for Vertex-backed Claude auth. | Used only when the Claude CLI runs in Vertex mode. |
| `ANTHROPIC_VERTEX_PROJECT_ID` | string | unset | Google Cloud project ID for Vertex-backed Claude auth. | Used only when the Claude CLI runs in Vertex mode. |
| `GEMINI_API_KEY` | string | unset | Authentication token for the Gemini CLI. | Read by the external Gemini CLI process. |
| `CURSOR_API_KEY` | string | unset | Authentication token for the Cursor CLI. | Read by the external Cursor CLI process. |

```bash
export ANTHROPIC_API_KEY="replace-with-an-anthropic-key"
export GEMINI_API_KEY="replace-with-a-gemini-key"
export CURSOR_API_KEY="replace-with-a-cursor-key"
```

> **Note:** These variables are consumed by the provider CLIs that JJI launches. They are not modeled as FastAPI settings fields.

### Jira Integration

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `JIRA_URL` | URL | unset | Jira base URL. | Required for Jira enrichment and Jira helper APIs. |
| `JIRA_EMAIL` | string | unset | Jira Cloud email. | If set, Jira auth is treated as Cloud mode. |
| `JIRA_API_TOKEN` | string | unset | Jira Cloud API token. | Preferred credential in Cloud mode; fallback credential in Server/Data Center mode. |
| `JIRA_PAT` | string | unset | Jira personal access token. | Preferred credential in Server/Data Center mode; fallback credential in Cloud mode. |
| `JIRA_PROJECT_KEY` | string | unset | Default Jira project key. | Scopes Jira enrichment and default project selection. |
| `JIRA_SSL_VERIFY` | boolean | `true` | Jira TLS verification toggle. | `false` allows self-signed certificates. |
| `JIRA_MAX_RESULTS` | integer | `5` | Maximum Jira matches returned per search. | Caps enrichment search results. |
| `ENABLE_JIRA` | boolean | auto | Analysis-time Jira enrichment toggle. | `false` disables enrichment; unset enables it only when Jira URL, a usable credential, and a project key are present. |
| `ENABLE_JIRA_ISSUES` | boolean | `true` | Jira issue-creation feature toggle. | Independent of `ENABLE_JIRA`; `false` disables Jira issue creation. |

```bash
export JIRA_URL="https://jira.example.com"
export JIRA_EMAIL="alice@example.com"
export JIRA_API_TOKEN="replace-with-a-jira-token"
export JIRA_PROJECT_KEY="PROJ"
export JIRA_MAX_RESULTS=10
export ENABLE_JIRA=true
export ENABLE_JIRA_ISSUES=true
```

> **Note:** Auth selection is mode-aware: Cloud mode prefers `JIRA_API_TOKEN`; Server/Data Center mode prefers `JIRA_PAT`.

### GitHub Integration

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `GITHUB_TOKEN` | string | unset | Default GitHub token. | Used for GitHub-side lookups and as the server-side credential source for GitHub operations. |
| `ENABLE_GITHUB_ISSUES` | boolean | `true` | GitHub issue-creation feature toggle. | `false` disables GitHub issue creation; when enabled, the repo URL and token can still come from request-time inputs instead of server defaults. |

```bash
export GITHUB_TOKEN="replace-with-a-github-token"
export ENABLE_GITHUB_ISSUES=true
```

### Report Portal

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `REPORTPORTAL_URL` | URL | unset | Report Portal base URL. | Required to enable push integration. |
| `REPORTPORTAL_API_TOKEN` | string | unset | Report Portal API token. | Authenticates Report Portal API calls. |
| `REPORTPORTAL_PROJECT` | string | unset | Report Portal project name. | Target project used for launch matching and updates. |
| `REPORTPORTAL_VERIFY_SSL` | boolean | `true` | Report Portal TLS verification toggle. | `false` allows self-signed certificates. |
| `ENABLE_REPORTPORTAL` | boolean | auto | Report Portal feature toggle. | `false` disables the integration; unset enables it only when URL, token, and project are all present. |

```bash
export REPORTPORTAL_URL="https://reportportal.example.com"
export REPORTPORTAL_API_TOKEN="replace-with-an-rp-token"
export REPORTPORTAL_PROJECT="qe-gating"
export ENABLE_REPORTPORTAL=true
```

See [Push Classifications to Report Portal](push-classifications-to-report-portal.html) for usage details.

### Web Push and Alerting

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `VAPID_PUBLIC_KEY` | string | auto-generated | Public VAPID key for browser push subscriptions. | When both VAPID keys are unset, JJI generates and persists a key pair. |
| `VAPID_PRIVATE_KEY` | string | auto-generated | Private VAPID key for browser push subscriptions. | Must be provided together with `VAPID_PUBLIC_KEY` if you want fixed keys. |
| `VAPID_CLAIM_EMAIL` | string | `mailto:noreply@jji.local` | Contact email used in VAPID claims. | Used for both fixed and generated key pairs. |
| `SLACK_WEBHOOK_URL` | URL | unset | Slack incoming-webhook URL. | Enables Slack alert delivery; non-HTTPS values are flagged as configuration warnings. |
| `SMTP_HOST` | string | unset | SMTP host for email alerts. | Required for email alert delivery. |
| `SMTP_PORT` | integer | `587` | SMTP port for email alerts. | Port `587` enables `STARTTLS`. |
| `SMTP_USER` | string | unset | SMTP username. | Used for SMTP authentication when paired with `SMTP_PASSWORD`. |
| `SMTP_PASSWORD` | string | unset | SMTP password. | Used for SMTP authentication when paired with `SMTP_USER`. |
| `SMTP_FROM` | string | derived | Sender address for email alerts. | Defaults to `SMTP_USER`, or `jji@<SMTP_HOST>` when `SMTP_USER` is blank. |
| `ALERT_EMAIL_TO` | string | unset | Recipient address for email alerts. | Required together with `SMTP_HOST` to send email alerts. |

```bash
export VAPID_CLAIM_EMAIL="mailto:jji-admin@example.com"
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/example"
export SMTP_HOST="smtp.example.com"
export SMTP_PORT=587
export SMTP_USER="jji-alerts@example.com"
export SMTP_PASSWORD="replace-with-an-smtp-password"
export ALERT_EMAIL_TO="oncall@example.com"
```

> **Warning:** Set both `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` together if you want fixed keys. A partial pair is treated as invalid, and JJI falls back to generated keys.

### Metadata Rules File

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `METADATA_RULES_FILE` | path | unset | Path to a YAML or JSON rules file for automatic job metadata assignment. | Loaded once and cached for the process lifetime; file changes take effect after a server restart. |

```bash
export METADATA_RULES_FILE="/etc/jji/metadata-rules.yaml"
```

## `jji` Profile File: `config.toml`

`jji` looks for its profile file at ``$XDG_CONFIG_HOME/jji/config.toml`` or `~/.config/jji/config.toml` when `XDG_CONFIG_HOME` is unset.

See [CLI Command Reference](cli-command-reference.html) for command syntax.

### Top-Level Sections

| Section | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `[default]` | table | absent | Holds the default profile name. | Used when `--server` and `JJI_SERVER` are both absent. |
| `[defaults]` | table | absent | Shared profile values. | Merged into every `[servers.<name>]` entry before that server is used. |
| `[servers.<name>]` | table | absent | Named server profile. | Supplies a concrete server URL and client-side defaults. |

### `[default]` Fields

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `server` | string | unset | Name of the default profile. | Must be a non-empty trimmed string. |

```toml
[default]
server = "prod"
```

### `[defaults]` and `[servers.<name>]` Common Fields

> **Note:** `[defaults]` supports the same fields as `[servers.<name>]` except `server`. `url` can be defined in either place as long as each resolved profile ends up with a non-empty URL.

#### Connection and Auth

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `url` | string | unset | Base URL of the JJI server. | Required after defaults are merged. |
| `username` | string | `""` | Default username sent by `jji`. | Used for comments, reviews, and other user-attributed actions. |
| `no_verify_ssl` | boolean | `false` | Disable TLS verification for the CLI HTTP client. | Affects only the CLI connection to JJI, not Jenkins/Jira/Report Portal integration settings. |
| `api_key` | string | `""` | Default admin API key for the CLI. | Sent as the bearer token when a command needs admin access. |

#### Analysis Defaults

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `jenkins_url` | string | `""` | Default Jenkins base URL. | Sent only when the command does not pass `--jenkins-url`. |
| `jenkins_user` | string | `""` | Default Jenkins username. | Sent only when the command does not pass `--jenkins-user`. |
| `jenkins_password` | string | `""` | Default Jenkins password or API token. | Sent only when the command does not pass `--jenkins-password`. |
| `jenkins_ssl_verify` | boolean | unset | Default Jenkins TLS verification flag. | When omitted, the CLI leaves the server default unchanged. |
| `jenkins_timeout` | integer | `0` | Default Jenkins timeout override. | `0` means "do not send a value; let the server use its own default". |
| `tests_repo_url` | string | `""` | Default tests repository URL. | Supports the same `url:ref` format as the server environment variable. |
| `ai_provider` | string | `""` | Default AI provider. | Sent only when the command does not pass `--ai-provider`. |
| `ai_model` | string | `""` | Default AI model. | Sent only when the command does not pass `--ai-model`. |
| `ai_cli_timeout` | integer | `0` | Default AI CLI timeout override. | `0` means "do not send a value; let the server use its own default". |
| `peers` | string | `""` | Default peer config list. | Uses the same `provider:model,provider:model` format as `PEER_AI_CONFIGS`. |
| `peer_analysis_max_rounds` | integer | `0` | Default peer-round override. | `0` means "do not send a value"; non-zero values must be between `1` and `10` when used. |
| `additional_repos` | string | `""` | Default extra repo list. | Uses the same `name:url`, `name:url:ref`, or `name:url:ref@token` format as `ADDITIONAL_REPOS`. |
| `wait_for_completion` | boolean | unset | Default wait toggle for `jji analyze`. | When omitted, the CLI leaves the server default unchanged. |
| `poll_interval_minutes` | integer | `0` | Default poll-interval override. | `0` means "do not send a value". |
| `max_wait_minutes` | integer | `0` | Default max-wait override. | `0` means "do not send a value"; this is not the same as explicitly sending `0` in the API request body. |
| `force` | boolean | unset | Default force-analysis toggle. | When omitted, the CLI leaves the server default unchanged. |

#### Integration Defaults

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `jira_url` | string | `""` | Default Jira base URL. | Sent only when the command does not pass `--jira-url`. |
| `jira_email` | string | `""` | Default Jira Cloud email. | Sent only when the command does not pass `--jira-email`. |
| `jira_api_token` | string | `""` | Default Jira Cloud token. | Used for analysis requests that send Jira settings. |
| `jira_pat` | string | `""` | Default Jira personal access token. | Used for analysis requests that send Jira settings. |
| `jira_token` | string | `""` | Generic Jira token alias for CLI flows that accept a single Jira token field. | Used by Jira helper commands that do not split Cloud and Server/Data Center auth into separate options. |
| `jira_project_key` | string | `""` | Default Jira project key. | Used as the default project selection. |
| `jira_security_level` | string | `""` | Default Jira security level name for CLI Jira operations that accept it. | Used only by commands that support Jira security-level input. |
| `jira_ssl_verify` | boolean | unset | Default Jira TLS verification flag. | When omitted, the CLI leaves the server default unchanged. |
| `jira_max_results` | integer | `0` | Default Jira max-results override. | `0` means "do not send a value". |
| `enable_jira` | boolean | unset | Default Jira enrichment toggle. | When omitted, the CLI leaves the server default unchanged. |
| `github_token` | string | `""` | Default GitHub token. | Used for analysis-time GitHub lookups and GitHub CLI flows that need a token. |
| `github_repo_url` | string | `""` | Default GitHub repository URL for CLI GitHub operations that need a target repo. | Used only by commands that accept a GitHub repository URL. |

```toml
[default]
server = "prod"

[defaults]
username = "alice"
no_verify_ssl = false
jenkins_url = "https://jenkins.example.com"
ai_provider = "claude"
ai_model = "sonnet"
tests_repo_url = "https://github.com/acme/tests:main"
jira_url = "https://jira.example.com"
jira_project_key = "PROJ"

[servers.dev]
url = "http://localhost:8000"
no_verify_ssl = true
jenkins_ssl_verify = false

[servers.prod]
url = "https://jji.example.com"
api_key = "replace-with-an-admin-api-key"
github_token = "replace-with-a-github-token"
peers = "gemini:gemini-2.5-pro"
peer_analysis_max_rounds = 5
additional_repos = "product:https://github.com/acme/product:release-4.18"
```

### CLI Environment Variables and Global Flags

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `JJI_SERVER` / `--server` | string | unset | Server profile name or full JJI URL. | A full URL bypasses `config.toml` profile defaults for that connection. |
| `JJI_USERNAME` / `--user` | string | `""` | CLI username. | Overrides `config.toml` `username`. |
| `JJI_API_KEY` / `--api-key` | string | `""` | Admin bearer token. | Overrides `config.toml` `api_key`. |
| `JJI_NO_VERIFY_SSL` / `--no-verify-ssl` | boolean | unset | Disable TLS verification for the CLI HTTP client. | Overrides `config.toml` `no_verify_ssl`. |
| `--verify-ssl` | boolean | unset | Force TLS verification on. | Overrides `--no-verify-ssl`, `--insecure`, and `config.toml` `no_verify_ssl`. |
| `--insecure` | boolean | `false` | Alias for `--no-verify-ssl`. | Forces TLS verification off for the CLI HTTP client. |

```bash
export JJI_SERVER=prod
export JJI_USERNAME=alice
export JJI_API_KEY="replace-with-an-admin-api-key"

jji --verify-ssl health
jji --server https://jji.example.com --user release-bot results list
```

## Analysis Request Override Fields

This section documents the configuration-bearing request fields for `POST /analyze` and `POST /analyze-failures`.

See [REST API Reference](rest-api-reference.html) for required non-configuration fields such as `job_name`, `build_number`, `failures`, and `raw_xml`.

> **Note:** For `force`, `wait_for_completion`, `poll_interval_minutes`, `max_wait_minutes`, and `peer_analysis_max_rounds`, omitting the JSON key keeps the server value. Sending the key applies the request value, even if that value matches the schema default.

### Shared Override Fields

| Name | Type | Default When Omitted | Description | Effect |
| --- | --- | --- | --- | --- |
| `tests_repo_url` | string | server default | Tests repository URL. | Overrides `TESTS_REPO_URL` for one request; supports an optional `:ref` suffix. |
| `ai_provider` | `claude \| gemini \| cursor` | server default | AI provider for this request. | Overrides `AI_PROVIDER`; request fails if neither the request nor the server provides a provider. |
| `ai_model` | string | server default | AI model for this request. | Overrides `AI_MODEL`; request fails if neither the request nor the server provides a model. |
| `enable_jira` | boolean | server auto-detection | Analysis-time Jira enrichment toggle. | Overrides the server decision for this request only. |
| `ai_cli_timeout` | integer `> 0` | server default | AI CLI timeout in minutes. | Overrides `AI_CLI_TIMEOUT`. |
| `jira_url` | string | server default | Jira base URL. | Overrides `JIRA_URL`. |
| `jira_email` | string | server default | Jira Cloud email. | Overrides `JIRA_EMAIL`. |
| `jira_api_token` | string | server default | Jira Cloud API token. | Overrides `JIRA_API_TOKEN`. |
| `jira_pat` | string | server default | Jira personal access token. | Overrides `JIRA_PAT`. |
| `jira_project_key` | string | server default | Jira project key. | Overrides `JIRA_PROJECT_KEY`. |
| `jira_ssl_verify` | boolean | server default | Jira TLS verification flag. | Overrides `JIRA_SSL_VERIFY`. |
| `jira_max_results` | integer `> 0` | server default | Maximum Jira matches returned per search. | Overrides `JIRA_MAX_RESULTS`. |
| `raw_prompt` | string | none | Extra prompt text for this request. | Appended to the analysis prompt for this request only. |
| `github_token` | string | server default | GitHub token for this request. | Overrides `GITHUB_TOKEN` for GitHub-side lookups used during analysis. |
| `peer_ai_configs` | array of `AiConfigEntry` | server default | Peer review configuration list. | Overrides `PEER_AI_CONFIGS`; send `[]` to disable inherited peers. |
| `peer_analysis_max_rounds` | integer `1..10` | server default | Maximum debate rounds. | Overrides `PEER_ANALYSIS_MAX_ROUNDS` only when the key is explicitly present. |
| `additional_repos` | array of `AdditionalRepo` | server default | Extra repositories for analysis context. | Overrides `ADDITIONAL_REPOS`; send `[]` to disable inherited extra repos. |

#### `peer_ai_configs[]`

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `ai_provider` | `claude \| gemini \| cursor` | none | Peer provider name. | Selects the provider for that peer reviewer. |
| `ai_model` | string | none | Peer model identifier. | Must be non-blank after trimming. |

#### `additional_repos[]`

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `name` | string | none | Logical repo name. | Used as the cloned directory name; must be unique within the array and cannot contain path separators, `..`, or a leading `.`. |
| `url` | URL | none | Repository URL to clone. | Must be a valid absolute URL. |
| `ref` | string | `""` | Branch or tag name. | Empty means the remote default branch. |
| `token` | string or `null` | `null` | Token for cloning a private repo. | Used only for repository access; stored encrypted when request parameters are persisted. |

```json
{
  "job_name": "folder/job-name",
  "build_number": 1042,
  "ai_provider": "claude",
  "ai_model": "sonnet",
  "tests_repo_url": "https://github.com/acme/tests:main",
  "peer_ai_configs": [
    {
      "ai_provider": "gemini",
      "ai_model": "gemini-2.5-pro"
    }
  ],
  "peer_analysis_max_rounds": 5,
  "additional_repos": [
    {
      "name": "product",
      "url": "https://github.com/acme/product",
      "ref": "release-4.18"
    }
  ]
}
```

```json
{
  "raw_xml": "<testsuite>...</testsuite>",
  "ai_provider": "cursor",
  "ai_model": "gpt-5.4-xhigh",
  "peer_ai_configs": [],
  "additional_repos": []
}
```

### `POST /analyze`-Only Override Fields

| Name | Type | Default When Omitted | Description | Effect |
| --- | --- | --- | --- | --- |
| `jenkins_url` | string | server default | Jenkins base URL. | Overrides `JENKINS_URL`. |
| `jenkins_user` | string | server default | Jenkins username. | Overrides `JENKINS_USER`. |
| `jenkins_password` | string | server default | Jenkins password or API token. | Overrides `JENKINS_PASSWORD`. |
| `jenkins_ssl_verify` | boolean | server default | Jenkins TLS verification flag. | Overrides `JENKINS_SSL_VERIFY`. |
| `jenkins_timeout` | integer `> 0` | server default | Jenkins API timeout in seconds. | Overrides `JENKINS_TIMEOUT`. |
| `jenkins_artifacts_max_size_mb` | integer `> 0` | server default | Artifact size cap for this request. | Overrides `JENKINS_ARTIFACTS_MAX_SIZE_MB`. |
| `get_job_artifacts` | boolean | server default | Artifact-download toggle for this request. | Overrides `GET_JOB_ARTIFACTS`. |
| `force` | boolean | server default | Force-analysis toggle. | Overrides `FORCE_ANALYSIS` only when the key is explicitly present. |
| `wait_for_completion` | boolean | server default | Wait toggle for this request. | Overrides `WAIT_FOR_COMPLETION` only when the key is explicitly present. |
| `poll_interval_minutes` | integer `> 0` | server default | Poll interval while waiting. | Overrides `POLL_INTERVAL_MINUTES` only when the key is explicitly present. |
| `max_wait_minutes` | integer `>= 0` | server default | Maximum wait duration. | Overrides `MAX_WAIT_MINUTES` only when the key is explicitly present; explicit `0` means no wait limit. |

```json
{
  "job_name": "folder/job-name",
  "build_number": 1042,
  "jenkins_url": "https://jenkins.example.com",
  "jenkins_user": "ci-bot",
  "jenkins_password": "replace-with-a-token",
  "wait_for_completion": false,
  "force": true,
  "get_job_artifacts": false
}
```

## Metadata Rules File Format

The file referenced by `METADATA_RULES_FILE` can be either:

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| Top-level `metadata_rules` key | array of rule objects | none | Standard wrapper form for YAML or JSON. | JJI loads the array under `metadata_rules`. |
| Bare top-level array | array of rule objects | none | Shorthand form for YAML or JSON. | JJI treats the entire file as the rules list. |

### Rule Object Fields

| Name | Type | Default | Description | Effect |
| --- | --- | --- | --- | --- |
| `pattern` | string | none | Match expression for the Jenkins job name. | Required. Treated as a glob unless it contains a named regex capture group such as `(?P<version>...)`. |
| `team` | string | unset | Team value to assign. | First matching rule wins for this field. |
| `tier` | string | unset | Tier value to assign. | First matching rule wins for this field. |
| `version` | string | unset | Version value to assign. | First matching rule wins for this field. |
| `labels` | string or array of strings | unset | Labels to assign. | Labels accumulate across all matching rules and duplicates are removed. |

### Matching Rules

| Rule | Effect |
| --- | --- |
| Glob matching | Used for patterns that do not contain `(?P<...>)`. |
| Regex matching | Used only when the pattern contains at least one named capture group. |
| Named capture groups | Captured values become metadata fields with the same names. |
| Explicit field precedence | Explicit `team`, `tier`, or `version` values override regex-captured values from the same rule. |
| Scalar merge strategy | `team`, `tier`, and `version` use first-match-wins. |
| Label merge strategy | `labels` accumulate from every matching rule. |
| Reload behavior | Rules are cached for the process lifetime; restart the server to pick up file changes. |

> **Warning:** A pattern like `^job-.*$` is still treated as a glob. To force regex mode, include at least one named capture group such as `(?P<version>...)`.

```yaml
metadata_rules:
  - pattern: "release-*"
    labels: ["release"]

  - pattern: "console-t1-*"
    team: "console"
    tier: "t1"

  - pattern: "^console-(?P<version>\\d+\\.\\d+)-(?P<tier>t[12])$"
    team: "console"
    labels: ["versioned"]
```

See [REST API Reference](rest-api-reference.html) for metadata preview endpoints and [CLI Command Reference](cli-command-reference.html) for metadata-related CLI commands.

## Related Pages

- [Copy Common Deployment Recipes](copy-common-deployment-recipes.html)
- [Customize AI Analysis](customize-ai-analysis.html)
- [Manage Users, Access, and Token Usage](manage-users-access-and-token-usage.html)
- [CLI Command Reference](cli-command-reference.html)
- [REST API Reference](rest-api-reference.html)