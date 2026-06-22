# Setting Up the CLI

Get the `rootcoz` command-line tool installed, connected to your server, and authenticated so you can submit analyses, query results, and manage jobs without leaving your terminal.

**Prerequisites:**

- A running rootcoz server (see [Quickstart](quickstart.html))
- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) package manager (recommended) or pip

## Quick example

```bash
uv tool install rootcoz
export ROOTCOZ_SERVER=http://localhost:8000
rootcoz health
```

If you see `Status: healthy`, you're ready to go.

## Installing the CLI

The recommended way to install is with `uv tool`, which gives you an isolated `rootcoz` command:

```bash
uv tool install rootcoz
```

Alternatively, install with pip:

```bash
pip install rootcoz
```

Verify the installation:

```bash
rootcoz --help
```

This prints all available commands and global options.

## Connecting to a server

The CLI needs to know which rootcoz server to talk to. You have three options, from quickest to most permanent.

### Option 1: Environment variable

```bash
export ROOTCOZ_SERVER=http://localhost:8000
rootcoz health
```

### Option 2: CLI flag

```bash
rootcoz --server http://localhost:8000 health
```

### Option 3: Config file (recommended)

Create `~/.config/rootcoz/config.toml` to store server connections permanently:

```bash
mkdir -p ~/.config/rootcoz
cat > ~/.config/rootcoz/config.toml << 'EOF'
[default]
server = "dev"

[servers.dev]
url = "http://localhost:8000"
username = "myuser"
EOF
```

Now every command uses the `dev` server automatically:

```bash
rootcoz health
```

> **Tip:** The config file location follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/). Set `XDG_CONFIG_HOME` to change the base path.

Verify your config with:

```bash
rootcoz config show
```

## Authenticating

Most operations require authentication. Register a user account on the server to get an API key:

```bash
rootcoz register myuser
```

Output:

```
Registered: myuser

⚠️  Save this API key — you won't see it again!
API Key: rcz_a1b2c3d4e5f6...
```

> **Warning:** Copy the API key immediately. It is only shown once and cannot be retrieved later.

### Storing your API key

Add the key to your config file so you don't have to pass it on every command:

```toml
[servers.dev]
url = "http://localhost:8000"
username = "myuser"
api_key = "rcz_a1b2c3d4e5f6..."
```

Alternatively, use an environment variable or CLI flag:

```bash
# Environment variable
export ROOTCOZ_API_KEY=rcz_a1b2c3d4e5f6...

# CLI flag
rootcoz --api-key rcz_a1b2c3d4e5f6... results list
```

### Verifying authentication

```bash
rootcoz auth whoami
```

This shows your username, role, and admin status.

## Running your first analysis

Submit a Jenkins job for analysis:

```bash
rootcoz analyze --job-name my-pipeline --build-number 42
```

Output:

```
Job queued: a1b2c3d4-e5f6-...
Status: queued
Poll: http://localhost:8000/results/a1b2c3d4-e5f6-...
```

Check the status:

```bash
rootcoz status a1b2c3d4-e5f6-...
```

View the result once it completes:

```bash
rootcoz results show a1b2c3d4-e5f6-...
```

For the full JSON response, add `--full`:

```bash
rootcoz results show a1b2c3d4-e5f6-... --full
```

### Analyzing a JUnit XML file

You can also analyze a local JUnit XML file without Jenkins:

```bash
rootcoz analyze --source file --file test-results.xml
```

For more analysis options, see [Analyzing Test Failures](analyzing-failures.html).

## Global options

Every command supports these flags:

| Flag | Env Variable | Description |
|------|-------------|-------------|
| `--server`, `-s` | `ROOTCOZ_SERVER` | Server name from config or URL |
| `--user` | `ROOTCOZ_USERNAME` | Username for comments and reviews |
| `--api-key` | `ROOTCOZ_API_KEY` | API key for authentication |
| `--json` | — | Output as JSON instead of table |
| `--no-verify-ssl` | `ROOTCOZ_NO_VERIFY_SSL` | Disable SSL certificate verification |
| `--verify-ssl` | — | Force SSL verification on (overrides config) |
| `--insecure` | — | Alias for `--no-verify-ssl` |

> **Note:** `--verify-ssl` and `--insecure` are mutually exclusive.

## Config file reference

The config file uses [TOML](https://toml.io/) format with three sections:

```toml
# Set the default server for all commands
[default]
server = "dev"

# Shared settings inherited by all servers
[defaults]
jenkins_url = "https://jenkins.example.com"
jenkins_user = "ci-bot"
jenkins_password = "api-token"
ai_provider = "claude"
ai_model = "opus-4"
tests_repo_url = "https://github.com/org/tests"

# Per-server configuration
[servers.dev]
url = "http://localhost:8000"
username = "myuser"
api_key = "rcz_..."

[servers.prod]
url = "https://rootcoz.example.com"
username = "prod-user"
no_verify_ssl = false
# Override defaults for this server
ai_provider = "gemini"
ai_model = "gemini-2.5-pro"
```

The `[defaults]` section is optional. Any field set there is inherited by every server entry. Per-server values override defaults.

### Available config fields

| Field | Type | Description |
|-------|------|-------------|
| `url` | string | **Required.** Server URL |
| `username` | string | Username for the session |
| `api_key` | string | API key for authentication |
| `no_verify_ssl` | boolean | Disable SSL verification |
| `ai_provider` | string | AI provider (e.g. `claude`, `gemini`, `cursor`) |
| `ai_model` | string | AI model name |
| `ai_call_timeout` | integer | AI timeout in minutes |
| `max_concurrent_ai_calls` | integer | Max parallel AI calls |
| `jenkins_url` | string | Jenkins server URL |
| `jenkins_user` | string | Jenkins username |
| `jenkins_password` | string | Jenkins API token |
| `jenkins_ssl_verify` | boolean | Verify Jenkins SSL certificates |
| `jenkins_timeout` | integer | Jenkins API timeout in seconds |
| `tests_repo_url` | string | Test source repository URL |
| `tests_repo_token` | string | Token for cloning private test repos |
| `jira_url` | string | Jira instance URL |
| `jira_email` | string | Jira Cloud email |
| `jira_api_token` | string | Jira Cloud API token |
| `jira_pat` | string | Jira Server/DC personal access token |
| `jira_token` | string | Jira token (config-only, used for bug creation) |
| `jira_project_key` | string | Jira project key |
| `jira_security_level` | string | Jira security level name |
| `jira_ssl_verify` | boolean | Verify Jira SSL certificates |
| `jira_max_results` | integer | Max Jira search results |
| `enable_jira` | boolean | Enable/disable Jira integration |
| `github_token` | string | GitHub API token |
| `github_repo_url` | string | GitHub repository URL |
| `peers` | string | Peer AI configs (`"provider:model,provider:model"`) |
| `peer_analysis_max_rounds` | integer | Max peer debate rounds (1–10) |
| `additional_repos` | string | Extra repos for AI context (`"name:url,name:url"`) |
| `wait_for_completion` | boolean | Wait for Jenkins job to finish before analyzing |
| `poll_interval_minutes` | integer | Minutes between Jenkins polls |
| `max_wait_minutes` | integer | Max wait time for Jenkins completion |
| `force` | boolean | Analyze even if the build succeeded |

### Priority order

Settings are resolved with this priority (highest wins):

1. **CLI flags** (e.g. `--provider claude`)
2. **Environment variables** (e.g. `ROOTCOZ_API_KEY`)
3. **Per-server config** (e.g. `[servers.dev]`)
4. **Global defaults** (e.g. `[defaults]`)
5. **Server-side defaults**

## Switching between servers

Use `--server` to target a specific server by config name:

```bash
rootcoz --server prod results list
rootcoz --server dev analyze --job-name my-job --build-number 1
```

Or pass a URL directly to skip config resolution:

```bash
rootcoz --server https://rootcoz.example.com health
```

List all configured servers:

```bash
rootcoz config servers
```

## Shell completion

Enable tab completion for faster command entry:

```bash
# Show setup instructions
rootcoz config completion zsh    # or: bash
```

Follow the printed instructions to add the completion to your shell config.

## Advanced Usage

### JSON output for scripting

Every command supports `--json` for machine-readable output. This makes it easy to pipe into `jq` or other tools:

```bash
rootcoz results list --json | jq '.[].job_id'
rootcoz --json results show a1b2c3d4-... | jq '.result.failures | length'
```

### Rotating your API key

If your key is compromised, rotate it immediately:

```bash
rootcoz auth rotate-key
```

The new key is printed once — update your config file with the replacement.

### Self-signed certificates

When connecting to servers with self-signed SSL certificates, disable verification per-server in your config:

```toml
[servers.internal]
url = "https://rootcoz.internal.local"
no_verify_ssl = true
```

Or per-command:

```bash
rootcoz --insecure --server https://rootcoz.internal.local health
```

> **Warning:** Disabling SSL verification reduces security. Only use this for trusted internal servers.

### Multiple config profiles

Define separate profiles for different environments, teams, or credentials:

```toml
[default]
server = "team-a"

[defaults]
ai_provider = "claude"

[servers.team-a]
url = "https://rootcoz-a.example.com"
username = "alice"
api_key = "rcz_aaa..."
jira_project_key = "TEAMA"

[servers.team-b]
url = "https://rootcoz-b.example.com"
username = "alice"
api_key = "rcz_bbb..."
jira_project_key = "TEAMB"
ai_provider = "gemini"
```

## Troubleshooting

**"No server specified"** — Set `ROOTCOZ_SERVER`, use `--server`, or create a config file. Run `rootcoz config show` to check your current configuration.

**"HTTP 401" / "Use --api-key or set ROOTCOZ_API_KEY"** — Your API key is missing or invalid. Register with `rootcoz register <username>` and save the key to your config file.

**"HTTP 403" / "requires a higher role"** — Your user role doesn't have permission for this action. Contact an admin to upgrade your role. See [Managing Users and Roles](managing-users.html) for the role hierarchy.

**"Cannot connect to ..."** — The server is unreachable. Verify the URL is correct and the server is running with `rootcoz health`.

**"Request timed out"** — The server took too long to respond. This can happen during large analyses. Increase the timeout or check server health.

**SSL errors connecting to Jenkins/Jira** — If the target service uses a self-signed certificate, disable verification for that integration:

```bash
rootcoz analyze --job-name my-job --build-number 1 --no-jenkins-ssl-verify
```

Or set it permanently in config:

```toml
[servers.dev]
url = "http://localhost:8000"
jenkins_ssl_verify = false
jira_ssl_verify = false
```

For the full command reference, see [CLI Command Reference](cli-reference.html). For ready-to-use workflow recipes, see [CLI Recipes](cli-recipes.html).

## Related Pages

- [Quickstart](quickstart.html)
- [CLI Command Reference](cli-reference.html)
- [CLI Recipes](cli-recipes.html)
- [Analyzing Test Failures](analyzing-failures.html)
- [Managing Users and Roles](managing-users.html)