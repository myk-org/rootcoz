# Connecting GitHub, Jira, and Report Portal

## Check Server Readiness
Use this when you want to see which integrations are already available before you start saving tokens or restarting the server.

```bash
export ROOTCOZ_SERVER="http://localhost:8000"

rootcoz capabilities --json
rootcoz health --json
```

`github_issues_enabled` and `jira_issues_enabled` are feature toggles, while `server_github_token`, `server_jira_token`, `server_jira_project_key`, `reportportal`, and `reportportal_project` show how much the server can do without your own credentials. `health` is also where Report Portal validation lives, because the server probes the RP API and reports that result under `checks.reportportal`.

> **Tip:** For the exact JSON fields, see [API Endpoint Reference](api-endpoint-reference.html).

## Validate a GitHub PAT
Use this when you want to confirm a GitHub token works before you depend on it for duplicate checks or issue submission.

```bash
export ROOTCOZ_SERVER="http://localhost:8000"

rootcoz validate-token github --token "ghp_EXAMPLE_TOKEN_REPLACE_ME"
```

The server validates the token with GitHub's `/user` endpoint and prints the authenticated login on success. This does not store the token anywhere, so it is the safest first smoke test before you save it in rootcoz or automation.

- GitHub tracker actions use a personal access token with `repo` access.
- A valid token alone is not enough for submission; rootcoz also needs a target GitHub repository from the analyzed result, `TESTS_REPO_URL`, or `github_repo_url`.

## Validate Jira Access
Use this when you want to confirm Jira access and discover the project key or security level rootcoz should use later.

```bash
export ROOTCOZ_SERVER="http://localhost:8000"
JIRA_TOKEN="ATATT3xFfGF0AbM93-example-token"
JIRA_EMAIL="alice@example.com"

rootcoz validate-token jira --token "$JIRA_TOKEN" --email "$JIRA_EMAIL"
rootcoz jira-projects --jira-token "$JIRA_TOKEN" --jira-email "$JIRA_EMAIL"
rootcoz jira-security-levels QE --jira-token "$JIRA_TOKEN" --jira-email "$JIRA_EMAIL"
```

The validation call uses the server's configured `JIRA_URL`, then the lookup commands show which projects and security levels that token can actually see. Provide `--email` for Jira Cloud; omit it for Jira Server/Data Center bearer-token auth.

- Prefer `JIRA_PAT` on the server side; rootcoz still accepts `JIRA_API_TOKEN`, but current auth resolution prefers `JIRA_PAT` when both exist.
- `jira-security-levels` returns an empty list when the project has no issue security or the token cannot read it.

## Save Personal Tracker Tokens
Use this when you want browser-based tracker actions to run under your own identity instead of only server-wide credentials.

```bash
ROOTCOZ_SERVER="http://localhost:8000"
USERNAME="alice"

curl -sS -b "rootcoz_username=$USERNAME" \
  "$ROOTCOZ_SERVER/api/dashboard" >/dev/null

curl -sS -X PUT "$ROOTCOZ_SERVER/api/user/tokens" \
  -H "Content-Type: application/json" \
  -b "rootcoz_username=$USERNAME" \
  -d '{
    "github_token": "ghp_EXAMPLE_TOKEN_REPLACE_ME",
    "jira_email": "alice@example.com",
    "jira_token": "ATATT3xFfGF0AbM93-example-token"
  }'

curl -sS -b "rootcoz_username=$USERNAME" \
  "$ROOTCOZ_SERVER/api/user/tokens"
```

The first request ensures the username exists in the database, and the token save call stores only the fields you send for that user. Rootcoz encrypts those saved tracker tokens at rest, and the same API lets you read them back to verify what is currently on the server.

> **Tip:** Send only the fields you want to change; omitted tracker fields keep their existing values.

## Store Tracker Defaults in CLI Config
Use this when you want repeatable CLI tracker workflows without retyping tokens, repo URLs, or project keys every time.

```bash
mkdir -p ~/.config/rootcoz

cat > ~/.config/rootcoz/config.toml <<'EOF'
[default]
server = "lab"

[servers.lab]
url = "http://localhost:8000"
username = "alice"
github_token = "ghp_EXAMPLE_TOKEN_REPLACE_ME"
github_repo_url = "https://github.com/acme/platform-tests"
jira_token = "ATATT3xFfGF0AbM93-example-token"
jira_email = "alice@example.com"
jira_project_key = "QE"
jira_security_level = "Internal"
EOF

rootcoz config show
```

The CLI reads `~/.config/rootcoz/config.toml` and uses these tracker fields as defaults when you omit flags. That keeps repeated Jira and issue commands short, while one-off CLI flags still override the config when you need a different token or target.

> **Warning:** The CLI config file is plain text on disk, so protect it the same way you protect any other local secret.


> **Tip:** See [CLI Command Reference](cli-command-reference.html) for the full set of commands that read config defaults.

## Enable Server-Side Issue Creation
Use this when you run your own rootcoz server and want GitHub and Jira issue actions available even before users add personal tokens.

```bash
export TESTS_REPO_URL="https://github.com/acme/platform-tests"
export GITHUB_TOKEN="ghp_EXAMPLE_SERVER_TOKEN"
export ENABLE_GITHUB_ISSUES=true

export JIRA_URL="https://acme.atlassian.net"
export JIRA_EMAIL="rootcoz-bot@acme.example"
export JIRA_PAT="ATATT3xFfGF0AbM93-server-example-token"
export JIRA_PROJECT_KEY="QE"
export ENABLE_JIRA_ISSUES=true

docker compose up -d --force-recreate rootcoz

export ROOTCOZ_SERVER="http://localhost:8000"
rootcoz capabilities --json
```

This gives the server its own GitHub and Jira defaults so previews, duplicate checks, and tracker submission can work without waiting for per-user setup. If you run rootcoz outside Docker Compose, restart that process with the same environment variables instead.

- On Jira Server/Data Center, drop `JIRA_EMAIL` and keep `JIRA_PAT`.
- Users can still override server defaults with their own GitHub or Jira tokens when they want issues filed under their own account.

## Enable Report Portal
Use this when you want rootcoz to expose Report Portal push controls and verify the RP token before the first push.

```bash
export REPORTPORTAL_URL="https://rp.acme.example"
export REPORTPORTAL_API_TOKEN="rp_api_token_for_rootcoz"
export REPORTPORTAL_PROJECT="platform-qe"
export PUBLIC_BASE_URL="https://rootcoz.acme.example"
export ENABLE_REPORTPORTAL=true

docker compose up -d --force-recreate rootcoz

export ROOTCOZ_SERVER="http://localhost:8000"
rootcoz health --json
rootcoz capabilities --json
```

Rootcoz only exposes Report Portal push when the URL, API token, and project are all configured, and the health check is where the server validates that token. `PUBLIC_BASE_URL` is required because the RP comment payload includes a link back to the rootcoz result page.

> **Warning:** Report Portal push cannot work with relative rootcoz URLs; set `PUBLIC_BASE_URL` to the externally reachable rootcoz origin.

- Set `REPORTPORTAL_VERIFY_SSL=false` if your RP instance uses a self-signed certificate.
- `capabilities` should show `reportportal: true` and the configured `reportportal_project`.

## Push a Result to Report Portal
Use this when you want rootcoz classifications copied into the matching Report Portal launch for the most recent result returned by the CLI.

```bash
export ROOTCOZ_SERVER="http://localhost:8000"

JOB_ID="$(rootcoz results list --json | python -c 'import json,sys; print(json.load(sys.stdin)[0]["job_id"])')"
rootcoz push-reportportal "$JOB_ID" --json
```

Rootcoz finds the matching launch from the Jenkins build URL stored in the RP launch description, matches failed items to rootcoz failures by test name, and updates defect types in one batch. When a failure already has Jira matches, rootcoz also sends them to Report Portal as external issues, so the response is the thing to inspect for `pushed`, `unmatched`, `errors`, and `launch_id`.

> **Warning:** If the matching RP launch does not contain the Jenkins build URL in its description, rootcoz cannot find it and the push returns a structured error instead of an update.

- To target a pipeline child analysis, add `--child-job-name "e2e-upgrade" --child-build-number 42`.
- If you want a specific job instead of the newest one, pick a different `job_id` from `rootcoz results list --json`.

## Related Pages

- [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html)
- [Updating Your Profile and Notifications](updating-your-profile-and-notifications.html)
- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)
- [Configuration Reference](configuration-reference.html)
- [CLI Command Reference](cli-command-reference.html)