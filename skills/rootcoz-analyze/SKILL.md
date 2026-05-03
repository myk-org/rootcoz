---
name: rootcoz-analyze
description: Use when the user asks to analyze a Jenkins job, run failure analysis, check analysis status, or interact with the rootcoz server via the rcz CLI
---

# Analyze Jenkins Jobs with rcz

## Overview

Analyze Jenkins job failures using AI via the rcz CLI. The CLI connects to a rootcoz server that fetches Jenkins build data, clones the test repo, and uses AI to classify failures.

## Prerequisites (MANDATORY - check before anything else)

### 1. rcz CLI installed

```bash
rcz --help
```

If not found: `uv tool install rootcoz` or `uv pip install rootcoz`

### 2. Server is reachable

```bash
rcz --server <server> health
```

If health check fails:
- Check config: `rcz config show`
- Set up a profile: create `~/.config/rootcoz/config.toml` (see `config.example.toml` in the repo root)

## Configuration

rcz supports multiple server profiles via `~/.config/rootcoz/config.toml` (`$XDG_CONFIG_HOME/rootcoz/config.toml`):

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
url = "https://rootcoz.example.com"
```

Priority: CLI flags / environment variables > config file.

## Workflow

### Phase 1: Determine Server

Check if the user specified a server. If not, check if config has a default:

```bash
rcz config show
```

### Phase 2: Analyze a Job

**Always ask the user for job name and build number — NEVER assume:**

```bash
rcz --server <server> analyze \
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
rcz --server <server> status <job_id>
```

Or open the web UI: `http://<server>/results/<job_id>` (waiting/running jobs redirect to `/status/<job_id>`)

### Phase 4: Review Results

```bash
rcz --server <server> results show <job_id>
rcz --server <server> results dashboard
rcz --server <server> results review-status <job_id>
```

## Key Commands Reference

| Command | Purpose |
|---------|---------|
| `rcz analyze` | Submit a Jenkins job for analysis |
| `rcz status <job_id>` | Check analysis status |
| `rcz results dashboard` | List all analysis runs |
| `rcz results show <job_id>` | Get full analysis result |
| `rcz results delete <job_id>` | Delete an analysis |
| `rcz results review-status <job_id>` | Show review progress |
| `rcz results set-reviewed <job_id>` | Mark test as reviewed |
| `rcz results enrich-comments <job_id>` | Refresh comment enrichments |
| `rcz health` | Check server health |
| `rcz capabilities` | Show server automation features |
| `rcz ai-configs` | List known AI provider/model pairs |
| `rcz history search` | Search failure history |
| `rcz history test <name>` | Get test failure history |
| `rcz history stats` | Get failure statistics |
| `rcz classify` | Classify a test failure |
| `rcz classifications list` | List test classifications |
| `rcz override-classification` | Override a failure classification |
| `rcz comments add <job_id>` | Add a comment to a failure |
| `rcz comments list <job_id>` | List comments for a job |
| `rcz comments delete <job_id> <id>` | Delete a comment |
| `rcz create-issue <job_id>` | Create GitHub issue or Jira bug |
| `rcz preview-issue <job_id>` | Preview issue content |
| `rcz config show` | Show current configuration |
| `rcz config servers` | List configured servers |
| `rcz config completion` | Show shell completion setup |

## Critical Mistakes to Avoid

- Never hardcode server URL — always use `--server` or config
- Never hardcode AI provider/model — always ask the user or use config defaults
- Always check health before operations
- Use `--no-wait` only if you know the Jenkins job is already finished
- Use `--no-jira` to explicitly disable Jira integration if not needed
