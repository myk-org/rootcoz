# RootCoz

AI-powered CI failure analysis -- classifies test failures as code issues or product bugs. Supports Jenkins, Prow, and JUnit XML input.

**[Documentation](https://myk-org.github.io/rootcoz/)** -- configuration, API reference, integrations, and more.

## Prerequisites

Provider IDs and models are discovered from Pi-sidecar. Install and authenticate the corresponding provider CLI or API credentials; see [docs](https://myk-org.github.io/rootcoz/ai-provider-setup.html) for setup details.

## Quick Start

```bash
mkdir -p data
docker run -d -p 8000:8000 -v ./data:/data \
  -e JENKINS_URL=https://jenkins.example.com \
  -e JENKINS_USER=your-username \
  -e JENKINS_PASSWORD=your-api-token \
  -e PROW_URL=https://prow.example.com \
  -e GCS_BUCKET=your-gcs-bucket \
  -e AI_PROVIDER=claude \
  -e AI_MODEL=your-model-name \
  ghcr.io/myk-org/rootcoz:latest
```

For Prow-only deployments, set `PROW_URL` and `GCS_BUCKET` instead of (or in addition to) Jenkins credentials. Both can also be configured per-request or via Server Settings.

### Analysis Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONCURRENT_AI_CALLS` | `3` | Maximum concurrent AI CLI processes. Prevents OOM with heavy models. |

`MAX_CONCURRENT_AI_CALLS` can be set via any of the supported interfaces:
- Environment variable: `MAX_CONCURRENT_AI_CALLS`
- API request field: `max_concurrent_ai_calls`
- CLI flag: `--max-concurrent`
- Config file (`~/.config/rootcoz/config.toml`): `max_concurrent_ai_calls`

Example API override:

```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"type": "jenkins", "job_name": "my-job", "build_number": 42, "max_concurrent_ai_calls": 2}'
```

Prow analysis (requires a GCS bucket with public HTTPS read access for build artifacts):

```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"type": "prow", "prow_job_name": "periodic-ci-e2e-aws", "build_id": "1234567890", "prow_url": "https://prow.ci.openshift.org", "gcs_bucket": "test-platform-results", "gcs_prefix": "logs/periodic-ci-e2e-aws/1234567890"}'
```

Jobs can omit `gcs_prefix`; RootCoz auto-resolves it via prowjob.json or the Prow directory pointer (supports presubmit, postsubmit, and periodic jobs).

| Field | Env var | API / CLI | Config file |
|-------|---------|-----------|-------------|
| `prow_url` | `PROW_URL` | `--prow-url` | `prow_url` |
| `gcs_bucket` | `GCS_BUCKET` | `--gcs-bucket` | `gcs_bucket` |
| `gcs_prefix` | — | `--gcs-prefix` / payload | — |
| `prow_job_name` | — | `--job-name` (prow) | — |
| `build_id` | — | `--build-number` (prow) | — |

## Features

- **AI-Powered Failure Analysis** — Classifies test failures as code issues or product bugs
- **Multi-CI Support** — Analyzes builds from Jenkins, Prow (GCS artifacts), or raw JUnit XML input with a unified pipeline
- **Full Test Visibility** — Stores and displays all test outcomes (passed, skipped, failed) from CI sources. Zero-failure jobs are handled as a fast path with no AI overhead.
- **AI Token Usage Tracking** — Track token consumption, costs, and duration for all AI CLI calls. Admin dashboard shows usage by provider/model/time period with CSV export.
- **Public OpenAPI** — `/openapi.json`, `/docs`, and `/redoc` are available without authentication
- **Sparse result fields** — `GET /results/{job_id}?fields=status,result.summary,...` returns only allowlisted paths (full values). Discover paths via `GET /api/results/fields` or `rootcoz results fields`
- **Reports access flag** — Non-admins need `can_view_reports` (admin grant / `rootcoz admin users set-can-view-reports`) to call `/api/reports/*` and `rootcoz reports`
- **Analyze-time labels** — Pass `--label` / `-l` on `rootcoz analyze` (or `labels` in the API / config.toml) to merge job metadata labels at submit time

## Custom Analysis Agents

rootcoz supports user-provided analysis agents that extend or customize the AI failure analysis pipeline. Agents are defined as Markdown files and are automatically discovered and executed during analysis.

### How It Works

1. Place agent `.md` files in `.rootcoz/agents/` in your analyzed repository
2. When rootcoz clones the repo for analysis, it copies `.rootcoz/agents/` to the workspace `.pi/agents/`
3. The AI orchestrator loads the built-in `test-analyzer` agent body as the `system_prompt` for per-group AI calls (read via the `read` tool, not dispatched as a subagent)
4. Other project agents (non-`test-analyzer`) are read via the STEP 0 hard gate before analysis begins — the AI reads each agent file using the `read` tool to incorporate their guidance
5. To override the built-in analyzer, name your agent `test-analyzer` — user agents take precedence

### Creating an Agent

Create a Markdown file in `.rootcoz/agents/` with YAML frontmatter:

```markdown
---
name: my-analyzer
description: Brief description of what this agent does.
tools:
  - read
  - ls
  - find
  - grep
---

# My Custom Analyzer

Your agent instructions here...
```

### Requirements

- **Name**: Must match `^[A-Za-z0-9._-]{1,64}$` — prefer frontmatter `name:`; filename stem is used as fallback when it matches the same regex
- **Format**: Markdown file with YAML frontmatter. `name:` is preferred (filename stem is the fallback when it matches the safe regex). `description:` and `tools:` are recommended — see below
- **Tools**: Strongly recommended — list `tools: [read, ls, find, grep]` in frontmatter to restrict available tools. Without this, pi may grant default builtins including `bash`
- **Discovery**: Names that don't match the regex are silently skipped (not sanitized/rewritten)
- **Response** (test-analyzer or overrides): Return a valid JSON object matching the `AnalysisDetail` schema. Other STEP 0 agents return free-form text that gets incorporated into the analysis details:

```json
{
  "classification": "PRODUCT BUG | CODE ISSUE | INFRASTRUCTURE",
  "pattern": "NEW | REGRESSION | FLAKY | INTERMITTENT | KNOWN_BUG | PERSISTENT",
  "affected_tests": ["test_name_1"],
  "details": "Detailed analysis text",
  "artifacts_evidence": "Evidence from build artifacts"
}
```

For `CODE ISSUE`, include a `code_fix` object. For `PRODUCT BUG`, include a `product_bug_report` object.

### Example Agent

```markdown
---
name: k8s-analyzer
description: Analyzes Kubernetes-related test failures including pod scheduling, networking, storage, and RBAC issues.
tools:
  - read
  - ls
  - find
  - grep
---

# Kubernetes Failure Analyzer

You specialize in analyzing Kubernetes-related test failures.

## Instructions

1. Read the failure details file provided by the orchestrator
2. Look for Kubernetes-specific errors (pod scheduling, resource limits, network policies)
3. Check build artifacts for kubectl logs, pod descriptions, and event dumps
4. Return your findings as free-form text (STEP 0 advisory agents are not per-group classifiers)

## Focus Areas

- Pod scheduling failures (Insufficient CPU/memory, node affinity)
- Network connectivity issues (DNS resolution, service endpoints)
- Storage provisioning failures (PVC binding, StorageClass issues)
- RBAC and security context problems
```

### Agent Discovery

- Agents are discovered from `.rootcoz/agents/*.md` files
- The `name` field in frontmatter is preferred; filename stem is used as fallback
- User agents take precedence over built-in agents with the same name
- Agent names are validated against `^[A-Za-z0-9._-]{1,64}$` — non-matching names/stems are silently skipped

### Built-in Agents

rootcoz ships a built-in `test-analyzer` agent that handles the core failure analysis. It is automatically copied to the workspace and used by the orchestrator. User agents with the same name override it.

### Cross-Failure Patterns

When using orchestrated analysis (default, non-peer mode), the orchestrator AI sees ALL failure groups and can detect cross-failure patterns — correlations impossible with per-group isolation.

Patterns appear in the API response as `cross_failure_patterns` on the job result:

```json
{
  "cross_failure_patterns": [
    {
      "pattern": "15 failures share the same NFS mount timeout",
      "affected_tests": ["test_a", "test_b"],
      "suggested_root_cause": "NFS server infrastructure issue"
    }
  ]
}
```

On `GET /results/{job_id}`, the field lives at `result.cross_failure_patterns`. The frontend displays these patterns on the job report page between the summary and individual failure cards.

**Note:** Cross-failure patterns are only detected in orchestrated mode. When `peer_ai_configs` is set, the legacy per-group analysis path is used and no cross-failure patterns are generated.

## CLI

```bash
uv tool install rootcoz
export ROOTCOZ_SERVER=http://localhost:8000

rootcoz health
rootcoz analyze --job-name my-job --build-number 42
rootcoz analyze --job-name my-job --build-number 42 --label Nightly -l CNV
rootcoz analyze --source prow \
  --job-name periodic-ci-e2e-aws \
  --build-number 1234567890 \
  --prow-url https://prow.ci.openshift.org \
  --gcs-bucket test-platform-results \
  --gcs-prefix logs/periodic-ci-e2e-aws/1234567890
rootcoz results list
rootcoz results tests <job_id>         # List test entries (passed/skipped/failed)
rootcoz admin token-usage              # Summary dashboard
rootcoz admin token-usage --group-by model  # Grouped breakdown
rootcoz admin token-usage --job-id <uuid>   # Per-job usage
rootcoz admin token-usage --period month --format csv  # CSV export
```

Run `rootcoz --help` for all commands.

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/admin/token-usage` | Aggregated token usage with filters and grouping (admin only) |
| `GET /api/admin/token-usage/summary` | Dashboard summary: today/week/month stats (admin only) |
| `GET /api/admin/token-usage/{job_id}` | Per-job token usage breakdown (admin only) |

See the [API reference](https://myk-org.github.io/rootcoz/) for all endpoints.

## Web Push Notifications

Users can receive browser push notifications when @mentioned in comments. The server uses [VAPID](https://datatracker.ietf.org/doc/html/rfc8292) for Web Push authentication.

| Variable | Description |
|----------|-------------|
| `VAPID_PUBLIC_KEY` | VAPID public key (auto-generated with private key if not set) |
| `VAPID_PRIVATE_KEY` | VAPID private key |
| `VAPID_CLAIM_EMAIL` | Contact email included in VAPID claims |

Subscribe/unsubscribe is browser-only (managed via the web UI). To list users available for @mentions:

```bash
rootcoz mentionable-users
```

## OAuth Proxy / SSO Integration

When deployed behind an OAuth proxy (e.g., OpenShift `oauth-proxy`), RootCoz can automatically identify users from the `X-Forwarded-User` header set by the proxy, eliminating the need for manual registration.

### Configuration

Set the following environment variable on the RootCoz server:

| Variable | Default | Description |
|----------|---------|-------------|
| `TRUST_PROXY_HEADERS` | `false` | Trust `X-Forwarded-User` header for user identification |

> **Security:** Only enable `TRUST_PROXY_HEADERS` when RootCoz is behind a trusted reverse proxy that sets the `X-Forwarded-User` header. If enabled without a proxy, any client can spoof the header.

### Behavior

When `TRUST_PROXY_HEADERS=true` and `X-Forwarded-User` is present:

1. The header value is used as the RootCoz username
2. A `rootcoz_username` cookie is automatically set so all downstream code works unchanged
3. The `/login` page redirects to the dashboard (no manual registration needed)
4. Admin sessions and Bearer tokens still take precedence over the header

When the header is absent, the standard cookie-based registration flow is used (backward compatible).

### Example: OpenShift OAuth Proxy

```yaml
# In your Deployment, add to the RootCoz app container (not the oauth-proxy sidecar):
env:
  - name: TRUST_PROXY_HEADERS
    value: "true"
```

## Helm Chart Deployment (OpenShift / Kubernetes)

```bash
uv run python scripts/helm-setup.py
```

The interactive setup script prompts for cluster type, hostname, AI provider, and credentials, then runs `helm upgrade --install`. See [chart/README.md](chart/README.md) for manual install, upgrade instructions, and examples. See [values.yaml](chart/values.yaml) for the full values reference with inline documentation.

Validate the chart with `uvx --with tox-uv tox -e chart` (requires [Helm 3](https://helm.sh/docs/intro/install/)).

## Development

```bash
git clone https://github.com/myk-org/rootcoz.git
cd rootcoz
uvx --with tox-uv tox
```

See the [development guide](https://myk-org.github.io/rootcoz/development-and-testing.html) for full setup.

## License

MIT
