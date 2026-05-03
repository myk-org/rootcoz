---
name: jji-analyze
description: Use when the user asks to analyze a Jenkins job, run failure analysis, check analysis status, or interact with the jenkins-job-insight server via the jji CLI
---

# Analyze Jenkins Jobs with jji

## Overview

Analyze Jenkins job failures using AI via the jji CLI. The CLI connects to a jenkins-job-insight server that fetches Jenkins build data, clones the test repo, and uses AI to classify failures.

## Prerequisites (MANDATORY - check before anything else)

### 1. jji CLI installed

```bash
jji --help
```

If not found: `uv tool install jenkins-job-insight` or `uv pip install jenkins-job-insight`

### 2. Server is reachable

```bash
jji --server <server> health
```

If health check fails:
- Check config: `jji config show`
- Set up a profile: create `~/.config/jji/config.toml` (see `config.example.toml` in the repo root)

## Configuration

jji supports multiple server profiles via `~/.config/jji/config.toml` (`$XDG_CONFIG_HOME/jji/config.toml`):

```toml
[default]
server = "dev"

[defaults]
jenkins_url = "https://jenkins.example.com"
jenkins_user = "user"
ai_provider = "claude"
ai_model = "claude-sonnet-4-20250514"
wait_for_completion = true
poll_interval_minutes = 2
max_wait_minutes = 0  # 0 = no limit (wait forever)

[servers.dev]
url = "http://localhost:8000"

[servers.prod]
url = "https://jji.example.com"
```

Priority: CLI flags / environment variables > config file.

## Workflow

### Phase 1: Determine Server

Check if the user specified a server. If not, check if config has a default:

```bash
jji config show
```

### Phase 2: Analyze a Job

**Always ask the user for job name and build number — NEVER assume:**

```bash
jji --server <server> analyze \
  --job-name <job_name> \
  --build-number <build_number> \
  --provider <ai_provider> \
  --model <ai_model> \
  --jira  # if Jira integration needed
```

The server will:
1. Check if the Jenkins job is still running (monitors until done by default)
2. Fetch build data and test results
3. Analyze failures with AI
4. Search Jira for matching bugs (if --jira enabled)
5. Return a result URL

### Phase 3: Check Status

```bash
jji --server <server> status <job_id>
```

Or open the web UI: `http://<server>/results/<job_id>` (waiting/running jobs redirect to `/status/<job_id>`)

### Phase 4: Review Results

```bash
jji --server <server> results show <job_id>
jji --server <server> results dashboard
jji --server <server> results review-status <job_id>
```

## Key Commands Reference

| Command | Purpose |
|---------|---------|
| `jji analyze` | Submit a Jenkins job for analysis |
| `jji status <job_id>` | Check analysis status |
| `jji results dashboard` | List all analysis runs |
| `jji results show <job_id>` | Get full analysis result |
| `jji results delete <job_id>` | Delete an analysis |
| `jji results review-status <job_id>` | Show review progress |
| `jji results set-reviewed <job_id>` | Mark test as reviewed |
| `jji results enrich-comments <job_id>` | Refresh comment enrichments |
| `jji health` | Check server health |
| `jji capabilities` | Show server automation features |
| `jji ai-configs` | List known AI provider/model pairs |
| `jji history search` | Search failure history |
| `jji history test <name>` | Get test failure history |
| `jji history stats` | Get failure statistics |
| `jji classify` | Classify a test failure |
| `jji classifications list` | List test classifications |
| `jji override-classification` | Override a failure classification |
| `jji comments add <job_id>` | Add a comment to a failure |
| `jji comments list <job_id>` | List comments for a job |
| `jji comments delete <job_id> <id>` | Delete a comment |
| `jji create-issue <job_id>` | Create GitHub issue or Jira bug |
| `jji preview-issue <job_id>` | Preview issue content |
| `jji config show` | Show current configuration |
| `jji config servers` | List configured servers |
| `jji config completion` | Show shell completion setup |

## Critical Mistakes to Avoid

- Never hardcode server URL — always use `--server` or config
- Never hardcode AI provider/model — always ask the user or use config defaults
- Always check health before operations
- Use `--no-wait` only if you know the Jenkins job is already finished
- Use `--no-jira` to explicitly disable Jira integration if not needed
