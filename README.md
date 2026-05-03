# Jenkins Job Insight

AI-powered Jenkins failure analysis -- classifies test failures as code issues or product bugs.

**[Documentation](https://myk-org.github.io/jenkins-job-insight/)** -- configuration, API reference, integrations, and more.

## Prerequisites

An AI provider CLI must be installed and authenticated: [Claude](https://docs.anthropic.com/en/docs/claude-code), [Gemini](https://github.com/google-gemini/gemini-cli), or [Cursor](https://docs.cursor.com/agent). See [docs](https://myk-org.github.io/jenkins-job-insight/ai-provider-setup.html) for setup details.

## Quick Start

```bash
mkdir -p data
docker run -d -p 8000:8000 -v ./data:/data \
  -e JENKINS_URL=https://jenkins.example.com \
  -e JENKINS_USER=your-username \
  -e JENKINS_PASSWORD=your-api-token \
  -e AI_PROVIDER=claude \
  -e AI_MODEL=your-model-name \
  ghcr.io/myk-org/jenkins-job-insight:latest
```

### Analysis Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONCURRENT_AI_CALLS` | `3` | Maximum concurrent AI CLI processes. Prevents OOM with heavy models. |

`MAX_CONCURRENT_AI_CALLS` can be set via any of the supported interfaces:
- Environment variable: `MAX_CONCURRENT_AI_CALLS`
- API request field: `max_concurrent_ai_calls`
- CLI flag: `--max-concurrent`
- Config file (`~/.config/jji/config.toml`): `max_concurrent_ai_calls`

Example API override:

```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"job_name": "my-job", "build_number": 42, "max_concurrent_ai_calls": 2}'
```

## Features

- **AI-Powered Failure Analysis** — Classifies test failures as code issues or product bugs
- **AI Token Usage Tracking** — Track token consumption, costs, and duration for all AI CLI calls. Admin dashboard shows usage by provider/model/time period with CSV export.

## CLI

```bash
uv tool install jenkins-job-insight
export JJI_SERVER=http://localhost:8000

jji health
jji analyze --job-name my-job --build-number 42
jji results list
jji admin token-usage              # Summary dashboard
jji admin token-usage --group-by model  # Grouped breakdown
jji admin token-usage --job-id <uuid>   # Per-job usage
jji admin token-usage --period month --format csv  # CSV export
```

Run `jji --help` for all commands.

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/admin/token-usage` | Aggregated token usage with filters and grouping (admin only) |
| `GET /api/admin/token-usage/summary` | Dashboard summary: today/week/month stats (admin only) |
| `GET /api/admin/token-usage/{job_id}` | Per-job token usage breakdown (admin only) |

See the [API reference](https://myk-org.github.io/jenkins-job-insight/) for all endpoints.

## Web Push Notifications

Users can receive browser push notifications when @mentioned in comments. The server uses [VAPID](https://datatracker.ietf.org/doc/html/rfc8292) for Web Push authentication.

| Variable | Description |
|----------|-------------|
| `VAPID_PUBLIC_KEY` | VAPID public key (auto-generated with private key if not set) |
| `VAPID_PRIVATE_KEY` | VAPID private key |
| `VAPID_CLAIM_EMAIL` | Contact email included in VAPID claims |

Subscribe/unsubscribe is browser-only (managed via the web UI). To list users available for @mentions:

```bash
jji mentionable-users
```

## OAuth Proxy / SSO Integration

When deployed behind an OAuth proxy (e.g., OpenShift `oauth-proxy`), JJI can automatically identify users from the `X-Forwarded-User` header set by the proxy, eliminating the need for manual registration.

### Configuration

Set the following environment variable on the JJI server:

| Variable | Default | Description |
|----------|---------|-------------|
| `TRUST_PROXY_HEADERS` | `false` | Trust `X-Forwarded-User` header for user identification |

> **Security:** Only enable `TRUST_PROXY_HEADERS` when JJI is behind a trusted reverse proxy that sets the `X-Forwarded-User` header. If enabled without a proxy, any client can spoof the header.

### Behavior

When `TRUST_PROXY_HEADERS=true` and `X-Forwarded-User` is present:

1. The header value is used as the JJI username
2. A `jji_username` cookie is automatically set so all downstream code works unchanged
3. The `/register` page redirects to the dashboard (no manual registration needed)
4. Admin sessions and Bearer tokens still take precedence over the header

When the header is absent, the standard cookie-based registration flow is used (backward compatible).

### Example: OpenShift OAuth Proxy

```yaml
# In your Deployment, add to the JJI app container (not the oauth-proxy sidecar):
env:
  - name: TRUST_PROXY_HEADERS
    value: "true"
```

## Development

```bash
git clone https://github.com/myk-org/jenkins-job-insight.git
cd jenkins-job-insight
uvx --with tox-uv tox
```

See the [development guide](https://myk-org.github.io/jenkins-job-insight/development-and-testing.html) for full setup.

## License

MIT
