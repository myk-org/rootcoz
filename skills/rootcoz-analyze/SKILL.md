---
name: rootcoz-analyze
description: Use when the user asks to analyze a Jenkins job, run failure analysis, check analysis status, or interact with the rootcoz server via the rootcoz CLI
---

# Analyze Jenkins Jobs with rootcoz

## Overview

Analyze Jenkins job failures using AI via the rootcoz CLI. The CLI connects to a rootcoz server that fetches Jenkins build data, clones the test repo, and uses AI to classify failures.

## Prerequisites (MANDATORY - check before anything else)

### 1. rootcoz CLI installed

```bash
rootcoz --help
```

If not found: `uv tool install rootcoz` or `uv pip install rootcoz`

### 2. Server is reachable

```bash
rootcoz --server <server> health
```

If health check fails:
- Check config: `rootcoz config show`
- Set up a profile: create `~/.config/rootcoz/config.toml` (see `config.example.toml` in the repo root)

## Configuration

rootcoz supports multiple server profiles via `~/.config/rootcoz/config.toml` (`$XDG_CONFIG_HOME/rootcoz/config.toml`):

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
rootcoz config show
```

### Phase 2: Analyze a Job

**Always ask the user for job name and build number — NEVER assume:**

```bash
rootcoz --server <server> analyze \
  --job-name <job_name> \
  --build-number <build_number> \
  --provider <ai_provider> \
  --model <ai_model> \
  --jira  # if Jira integration needed
  # Optional: --label Nightly --label CNV  (merge into job metadata labels)
```

The server will:
1. Check if the Jenkins job is still running (monitors until done by default)
2. Fetch build data and test results
3. Analyze failures with AI
4. Search Jira for matching bugs (if --jira enabled)
5. Return a result URL

### Phase 3: Check Status

```bash
rootcoz --server <server> status <job_id>
```

Or open the web UI: `http://<server>/results/<job_id>` (waiting/running jobs redirect to `/status/<job_id>`)

### Phase 4: Review Results

```bash
rootcoz --server <server> results show <job_id>
rootcoz --server <server> results dashboard
rootcoz --server <server> results review-status <job_id>
```

## Key Commands Reference

| Command | Purpose |
|---------|---------|
| `rootcoz analyze` | Submit a Jenkins job for analysis |
| `rootcoz status <job_id>` | Check analysis status |
| `rootcoz results dashboard` | List all analysis runs |
| `rootcoz results show <job_id>` | Get full analysis result |
| `rootcoz results delete <job_id>` | Delete an analysis |
| `rootcoz results review-status <job_id>` | Show review progress |
| `rootcoz results set-reviewed <job_id>` | Mark test as reviewed |
| `rootcoz results enrich-comments <job_id>` | Refresh comment enrichments |
| `rootcoz health` | Check server health |
| `rootcoz capabilities` | Show server automation features |
| `rootcoz ai-models [provider]` | List available AI models (optionally filter by provider) |
| `rootcoz history search` | Search failure history |
| `rootcoz history test <name>` | Get test failure history |
| `rootcoz history stats` | Get failure statistics |
| `rootcoz classify` | Classify a test failure |
| `rootcoz classifications list` | List test classifications |
| `rootcoz override-classification` | Override a failure classification |
| `rootcoz comments add <job_id>` | Add a comment to a failure |
| `rootcoz comments list <job_id>` | List comments for a job |
| `rootcoz comments delete <job_id> <id>` | Delete a comment |
| `rootcoz create-issue <job_id>` | Create GitHub issue or Jira bug |
| `rootcoz preview-issue <job_id>` | Preview issue content |
| `rootcoz config show` | Show current configuration |
| `rootcoz config servers` | List configured servers |
| `rootcoz config completion` | Show shell completion setup |

## Critical Mistakes to Avoid

- Never hardcode server URL — always use `--server` or config
- Never hardcode AI provider/model — always ask the user or use config defaults
- Always check health before operations
- Use `--no-wait` only if you know the Jenkins job is already finished
- Use `--no-jira` to explicitly disable Jira integration if not needed
