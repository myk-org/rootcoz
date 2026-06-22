# Configuring Integrations

Connect rootcoz to your CI systems, issue trackers, and quality platforms so failure analysis can pull build data, search for existing bugs, create new issues, and push classifications back to your test management tools.

- A running rootcoz server (see [Quickstart](quickstart.html))
- Operator or admin role for submitting analyses; reviewer or above for using tracker features

## Jenkins

### Quick Start

Set three environment variables and rootcoz can analyze any Jenkins job:

```bash
export JENKINS_URL="https://jenkins.example.com"
export JENKINS_USER="ci-reader"
export JENKINS_PASSWORD="your-api-token"
```

Then submit an analysis:

```bash
rootcoz analyze --source jenkins --job-name my-pipeline --build-number 42
```

### Authentication

rootcoz uses Jenkins username + API token authentication. Generate your API token from **Jenkins → Your User → Configure → API Token**.

| Environment Variable | Description | Default |
|---|---|---|
| `JENKINS_URL` | Jenkins server base URL | *(required)* |
| `JENKINS_USER` | Jenkins username | *(required)* |
| `JENKINS_PASSWORD` | Jenkins API token (not your login password) | *(required)* |

### SSL Configuration

By default, rootcoz verifies Jenkins SSL certificates. For Jenkins instances with self-signed certificates:

```bash
export JENKINS_SSL_VERIFY=false
```

### Timeouts and Artifacts

| Environment Variable | Description | Default |
|---|---|---|
| `JENKINS_TIMEOUT` | HTTP request timeout in seconds | `30` |
| `JENKINS_ARTIFACTS_MAX_SIZE_MB` | Maximum artifact download size in MB | `500` |
| `GET_JOB_ARTIFACTS` | Download build artifacts for AI context | `true` |

### Waiting for Builds

rootcoz can wait for an in-progress build to finish before analyzing it:

| Environment Variable | Description | Default |
|---|---|---|
| `WAIT_FOR_COMPLETION` | Wait for build to complete before analyzing | `true` |
| `POLL_INTERVAL_MINUTES` | Minutes between status checks | `2` |
| `MAX_WAIT_MINUTES` | Maximum wait time (0 = unlimited) | `0` |

### Per-Request Overrides

Every Jenkins setting can be overridden on a per-request basis through the CLI or API. This is useful when you work with multiple Jenkins instances or need different credentials for specific jobs.

**CLI flags:**

```bash
rootcoz analyze --source jenkins \
  --job-name my-job --build-number 100 \
  --jenkins-url https://other-jenkins.example.com \
  --jenkins-user other-user \
  --jenkins-password other-token \
  --no-jenkins-ssl-verify \
  --jenkins-timeout 60
```

**Config file** (`~/.config/rootcoz/config.toml`):

```toml
[defaults]
jenkins_url = "https://jenkins.example.com"
jenkins_user = "ci-reader"
jenkins_password = "default-token"

[servers.staging]
url = "https://rootcoz.example.com"
jenkins_url = "https://staging-jenkins.example.com"
jenkins_user = "staging-reader"
jenkins_password = "staging-token"
jenkins_ssl_verify = false
```

> **Tip:** CLI flags override config file values, which override environment variables. See [Setting Up the CLI](cli-setup.html) for full config file details.

## Jira

Jira integration searches for existing bug reports matching failures classified as PRODUCT BUG, helping teams avoid filing duplicates.

### Quick Start

```bash
# Jira Cloud
export JIRA_URL="https://yourteam.atlassian.net"
export JIRA_EMAIL="you@example.com"
export JIRA_API_TOKEN="your-api-token"
export JIRA_PROJECT_KEY="PROJ"
```

```bash
# Jira Server / Data Center
export JIRA_URL="https://jira.internal.example.com"
export JIRA_PAT="your-personal-access-token"
export JIRA_PROJECT_KEY="PROJ"
```

rootcoz auto-detects Cloud vs. Server/DC based on whether `JIRA_EMAIL` is set.

### Cloud vs. Server/DC Authentication

| Deployment | Required Variables | Auth Method |
|---|---|---|
| **Cloud** | `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Basic auth (email + API token), REST API v3 |
| **Server/DC** | `JIRA_URL`, `JIRA_PAT` | Bearer token, REST API v2 |

> **Note:** When both `JIRA_API_TOKEN` and `JIRA_PAT` are set, the detection rule is: if `JIRA_EMAIL` is present → Cloud mode (prefers `JIRA_API_TOKEN`); if `JIRA_EMAIL` is absent → Server/DC mode (prefers `JIRA_PAT`).

Generate a Jira Cloud API token at [https://id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens). For Server/DC, create a PAT under **Profile → Personal Access Tokens**.

### Jira Settings

| Environment Variable | Description | Default |
|---|---|---|
| `JIRA_URL` | Jira instance URL | *(required)* |
| `JIRA_EMAIL` | Email address (Cloud only; triggers Cloud detection) | |
| `JIRA_API_TOKEN` | Cloud API token or Server/DC fallback token | |
| `JIRA_PAT` | Server/DC personal access token | |
| `JIRA_PROJECT_KEY` | Scope bug searches to this project | *(required)* |
| `JIRA_SSL_VERIFY` | Verify SSL certificates | `true` |
| `JIRA_MAX_RESULTS` | Max search results returned | `5` |
| `ENABLE_JIRA` | Explicitly enable/disable Jira integration | Auto-detected |

When `ENABLE_JIRA` is not set, Jira integration is automatically enabled if `JIRA_URL`, valid credentials, and `JIRA_PROJECT_KEY` are all configured.

### Enabling or Disabling Per Request

Disable Jira for a single analysis:

```bash
rootcoz analyze --source jenkins \
  --job-name my-job --build-number 100 \
  --no-jira
```

Override Jira credentials for a single analysis:

```bash
rootcoz analyze --source jenkins \
  --job-name my-job --build-number 100 \
  --jira-url https://other-jira.example.com \
  --jira-pat "other-token" \
  --jira-project-key OTHER
```

### User-Level Jira Tokens

Individual users can save their own Jira credentials in the web UI under their profile settings. When a user creates a bug from analysis results, their personal token is used instead of the server-level token, so the issue is attributed to them.

> **Tip:** Verify your Jira token works by using the built-in token validation in the web UI settings, or with `rootcoz config validate-token --type jira --token YOUR_TOKEN`.

### Jira Bug Creation

When Jira integration is enabled, rootcoz also supports creating Jira bugs directly from analysis results. This is controlled by a separate toggle:

| Environment Variable | Description | Default |
|---|---|---|
| `ENABLE_JIRA_ISSUES` | Enable Jira bug creation | `true` (when Jira is configured) |

See [Creating Bug Reports from Analysis](creating-issues.html) for the full bug creation workflow.

## GitHub

GitHub integration serves two purposes: creating issues from analysis results and enriching comments with PR/issue status information.

### Quick Start

```bash
export GITHUB_TOKEN="ghp_your-personal-access-token"
export TESTS_REPO_URL="https://github.com/your-org/your-test-repo"
```

### GitHub Settings

| Environment Variable | Description | Default |
|---|---|---|
| `GITHUB_TOKEN` | GitHub personal access token | |
| `TESTS_REPO_URL` | Repository URL for issue creation and AI context | |
| `TESTS_REPO_TOKEN` | Token for cloning private test repos (if different from `GITHUB_TOKEN`) | |
| `ENABLE_GITHUB_ISSUES` | Enable GitHub issue creation | Auto-detected |

GitHub issue creation is auto-enabled when both `GITHUB_TOKEN` and `TESTS_REPO_URL` are configured. Set `ENABLE_GITHUB_ISSUES=false` to explicitly disable it.

### Token Permissions

Your GitHub token needs these scopes:

- **`repo`** — for creating issues and accessing private repositories
- **`read:org`** — if your repository is in a GitHub Organization

### Comment Enrichment

When `GITHUB_TOKEN` is set, rootcoz automatically enriches user comments that contain GitHub PR or issue links with their current status (open, closed, merged). This works on any GitHub link, not just links to the configured test repo.

### User-Level GitHub Tokens

Like Jira, users can save their own GitHub token in the web UI. When creating issues, the user's token is used so the issue shows as created by them.

### Per-Request Overrides

```bash
rootcoz analyze --source jenkins \
  --job-name my-job --build-number 100 \
  --github-token ghp_different-token \
  --tests-repo-url https://github.com/other-org/other-repo \
  --tests-repo-token ghp_private-repo-token
```

## Report Portal

Report Portal integration lets you push rootcoz classifications back into your Report Portal launches, updating defect types and adding analysis links.

### Quick Start

```bash
export REPORTPORTAL_URL="https://reportportal.example.com"
export REPORTPORTAL_API_TOKEN="your-rp-api-token"
export REPORTPORTAL_PROJECT="your-project"
export ENABLE_REPORTPORTAL=true
```

### Report Portal Settings

| Environment Variable | Description | Default |
|---|---|---|
| `REPORTPORTAL_URL` | Report Portal server URL | |
| `REPORTPORTAL_API_TOKEN` | API token for authentication | |
| `REPORTPORTAL_PROJECT` | Project name in Report Portal | |
| `REPORTPORTAL_VERIFY_SSL` | Verify SSL certificates | `true` |
| `ENABLE_REPORTPORTAL` | Enable the integration | Auto-detected |

When `ENABLE_REPORTPORTAL` is not set, it's auto-enabled if all three required settings (`REPORTPORTAL_URL`, `REPORTPORTAL_API_TOKEN`, `REPORTPORTAL_PROJECT`) are configured.

### Classification Mapping

rootcoz maps its classifications to Report Portal defect types:

| rootcoz Classification | Report Portal Defect Type |
|---|---|
| PRODUCT BUG | Product Bug |
| CODE ISSUE | Automation Bug |
| INFRASTRUCTURE | System Issue |

### Pushing Classifications

After analyzing a Jenkins job, push the results to Report Portal:

```bash
rootcoz push-reportportal <job-id>
```

For pipeline child jobs:

```bash
rootcoz push-reportportal <job-id> \
  --child-job-name child-pipeline \
  --child-build-number 5
```

rootcoz matches RP test items to analyzed failures by test name (exact match, then dotted-suffix match). Each matched item gets:

- The mapped defect type locator
- A comment linking back to the rootcoz analysis report
- Jira issue links (if Jira matches were found during analysis)

> **Warning:** Report Portal integration requires that the Jenkins build URL appears in the RP launch description. rootcoz uses this URL to find the matching launch.

### SSL for Self-Signed Certificates

```bash
export REPORTPORTAL_VERIFY_SSL=false
```

## Config File Reference

All integration settings can be stored in `~/.config/rootcoz/config.toml` to avoid passing flags on every CLI invocation. The `[defaults]` section applies to all servers, and `[servers.<name>]` sections override per server.

```toml
[defaults]
jenkins_user = "ci-reader"
jira_url = "https://jira.example.com"
jira_pat = "default-jira-token"
jira_project_key = "PROJ"
github_token = "ghp_default-token"

[servers.production]
url = "https://rootcoz.example.com"
jenkins_url = "https://jenkins.example.com"
jenkins_password = "prod-jenkins-token"
tests_repo_url = "https://github.com/org/tests"

[servers.staging]
url = "https://rootcoz-staging.example.com"
jenkins_url = "https://staging-jenkins.example.com"
jenkins_password = "staging-token"
jenkins_ssl_verify = false
jira_ssl_verify = false
enable_jira = false
```

See [Setting Up the CLI](cli-setup.html) for full config file documentation.

## Advanced Usage

### Server Settings UI

Admins can modify integration settings at runtime through the **Server Settings** page in the web UI without restarting the server. Navigate to **Admin → Server Settings** and update values for Jenkins, Jira, GitHub, or Report Portal categories.

> **Note:** Some settings (like VAPID keys) require a server restart to take effect. The UI indicates which settings need a restart.

### Override Priority

Settings are resolved in this order (highest priority first):

1. **Per-request** — CLI flags or API body fields
2. **Config file** — `[servers.<name>]` section, then `[defaults]`
3. **Server Settings DB** — values set via the admin Server Settings page
4. **Environment variables** — set at deployment time

### Additional Repositories

Provide extra source code repositories for the AI to reference during analysis:

```bash
export ADDITIONAL_REPOS="infra:https://github.com/org/infra-repo,product:https://github.com/org/product"
```

With a specific branch and authentication token:

```bash
export ADDITIONAL_REPOS="infra:https://github.com/org/infra-repo:develop@ghp_token123"
```

Or per request via CLI:

```bash
rootcoz analyze --source jenkins \
  --job-name my-job --build-number 100 \
  --additional-repos "infra:https://github.com/org/infra,product:https://github.com/org/product"
```

### Disabling SSL Verification

For environments with self-signed certificates, each integration has its own SSL toggle:

| Integration | Environment Variable | CLI Flag |
|---|---|---|
| Jenkins | `JENKINS_SSL_VERIFY=false` | `--no-jenkins-ssl-verify` |
| Jira | `JIRA_SSL_VERIFY=false` | `--no-jira-ssl-verify` |
| Report Portal | `REPORTPORTAL_VERIFY_SSL=false` | *(server-level only)* |

> **Warning:** Disabling SSL verification exposes connections to man-in-the-middle attacks. Only use this for development or when connecting to hosts with self-signed certificates behind trusted networks.

### Sensitive Data Handling

All credential fields (passwords, API tokens) are encrypted at rest when stored in the database. They are never returned in API responses or logged. The encryption key is controlled by the `ROOTCOZ_ENCRYPTION_KEY` environment variable.

See [Environment Variables and Configuration](environment-variables.html) for the complete list of all settings, and [Managing Users and Roles](managing-users.html) for authentication and user management.

## Troubleshooting

**"Jenkins connection failed" or timeout errors**
- Verify `JENKINS_URL` is reachable from the rootcoz server
- Check that `JENKINS_USER` has read access to the job
- Increase `JENKINS_TIMEOUT` for slow networks (default: 30 seconds)
- For SSL errors, try `JENKINS_SSL_VERIFY=false`

**Jira integration shows as disabled**
- All three are required: `JIRA_URL`, credentials (`JIRA_API_TOKEN` or `JIRA_PAT`), and `JIRA_PROJECT_KEY`
- For Cloud, `JIRA_EMAIL` must also be set
- Check server logs for specific warnings about missing configuration

**Report Portal push finds no matching launch**
- The RP launch description must contain the full Jenkins build URL
- Ensure `REPORTPORTAL_PROJECT` matches the exact project name in RP
- If multiple launches match, rootcoz raises an ambiguous launch error — ensure each build URL is unique across launches

**GitHub issue creation not available**
- Both `GITHUB_TOKEN` and `TESTS_REPO_URL` must be configured
- The token must have the `repo` scope
- Verify the token with: `rootcoz config validate-token --type github --token YOUR_TOKEN`

## Related Pages

- [Environment Variables and Configuration](environment-variables.html)
- [Setting Up the CLI](cli-setup.html)
- [Analyzing Test Failures](analyzing-failures.html)
- [Creating Bug Reports from Analysis](creating-issues.html)
- [Deployment Recipes](deployment-recipes.html)