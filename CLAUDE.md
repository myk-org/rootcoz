# Project Coding Principles

## Data Integrity

- Never truncate data arbitrarily (no `[:100]` or `[:2000]` slicing)
- Preserve full information; let consumers handle their own limits

## No Dead Code

- Use everything you create: imports, variables, clones, instantiations
- Remove unused code rather than leaving it dormant

## No Duplicate Code — MANDATORY

**ZERO tolerance for duplicate code. This is a hard rule, not a guideline.**

- If the same logic exists in 2+ places, it is a BUG. Extract it immediately.
- Before writing ANY code, search for existing helpers that do the same thing. Reuse first.
- This applies to ALL code: Python, JavaScript, CSS, HTML templates, SQL queries.
- Shared React components → extract to `components/shared/` or `components/ui/`
- Shared TypeScript logic → extract to `lib/` utilities
- Shared Python logic → extract functions, base classes, or mixins
- Copy-paste is NEVER acceptable. Not even "just this once." Not even "it's small."
- Every PR review will check for duplication. Duplicates found = code rejected.

## Testing — MANDATORY

**`tox` must pass before every commit. No exceptions.**

Run all tests:

```bash
uvx --with tox-uv tox
```

This runs both environments:
- `backend` — Python tests via `uv run pytest tests/ -q`
- `frontend` — Frontend build (`vite build`) + Vitest tests (`npm test`)

Individual environments:

```bash
uvx --with tox-uv tox -e backend    # Python only
uvx --with tox-uv tox -e frontend   # Frontend only
```

## Smart Context Management

- Prefer structured data (test reports, APIs) over raw logs
- When raw data is necessary, extract relevant content (errors, failures, warnings) instead of full dumps

## Parallel Execution

- Run independent, stateless operations in parallel
- Handle failures gracefully: one failure should not crash all parallel tasks
- Capture exceptions and continue processing

## File Handling

- Preserve user edits when modifying files
- Add missing elements rather than replacing entire content
- Never overwrite user customizations

## Communication

- Explain data flow through the system, not just variable locations
- Show how components connect and interact

## Architecture Rules

### Tech Stack

- **Backend**: Python + FastAPI + TinyDB
- **Frontend**: Vite + React 19 + TypeScript + Tailwind CSS + shadcn/ui (in `/frontend/`)
- **AI Integration**: CLI-based (Claude CLI, Gemini CLI, Cursor Agent CLI) — no SDK dependencies, provider-agnostic, `AI_PROVIDER` env var selects provider
- **CLI**: `jji` CLI tool for querying the API — run `jji --help` for available commands

### Frontend Patterns

- **State**: Page-scoped `useReducer` (e.g., `ReportContext` for the report page) — each page owns its own context; do NOT introduce global state (Redux, Zustand, etc.)
- **API**: Centralized `api.get/post/put/delete` wrapper in `lib/api.ts` — do NOT use raw `fetch` calls
- **User identification**: Cookie-based (`jji_username`), display-only — NOT an authentication/authorization boundary

### Auto-Generated Documentation

The `docs/` directory is **auto-generated** by [docsfy](https://github.com/myk-org/docsfy). **NEVER edit files in `docs/` manually** — all changes will be overwritten. To update documentation, modify source code and regenerate with docsfy, or edit `AGENTS.md` / `README.md` for project-level docs.

### AI Tool Access (IMPORTANT)

Never pre-feed data to the AI in the prompt. Give the AI tools (API endpoints, scripts, commands) and let it decide what data it needs.

**DO:**
- Expose API endpoints the AI can curl
- Provide skill files documenting available tools
- Let the AI query, explore, and interpret data on its own

**DON'T:**
- Pre-query the database and stuff results into the prompt
- Summarize or filter data before the AI sees it
- Make decisions about what data the AI needs — let the AI decide

### CLI Parity

Every new API endpoint MUST also be supported via the `jji` CLI tool. When adding a new endpoint:
1. Add the client method to `src/jenkins_job_insight/cli/client.py`
2. Add the CLI command to `src/jenkins_job_insight/cli/main.py`
3. Add tests for both in `tests/test_cli_client.py` and `tests/test_cli_main.py`

### Failure Deduplication

When multiple tests fail with the same error:
1. Failures are grouped by error signature (SHA-256 hash of error + stack trace)
2. Only one AI CLI call per unique error type
3. Analysis is applied to all failures with matching signature

### Jira Integration (Optional)

When configured, searches Jira for existing bugs matching PRODUCT BUG failures:
1. AI generates search keywords during analysis
2. Keywords search Jira (configurable issue type, summary search)
3. AI evaluates each candidate's relevance
4. Only relevant matches are attached to the result
5. Jira errors never crash the pipeline — all failures are swallowed gracefully

### Report Portal Integration (Optional)

When `ENABLE_REPORTPORTAL=true`, users can push test classifications back to Report Portal via the `push-reportportal` endpoint and CLI command.

### Feedback System

Users submit feedback (bugs, feature requests) via the FeedbackDialog component. Feedback is previewed with AI-generated issue content, then created as a GitHub issue. This replaces the old "Report Bug" flow.

### Logging

Uses `python-simple-logger`:
- INFO: Milestones (job started, AI calls, completed)
- DEBUG: Detailed operations (response lengths, extracted data)
- Configured via `LOG_LEVEL` environment variable

## API Design

### Configuration Parity

For request-tunable analysis settings, keep these interfaces in sync:
1. Environment variable (server-level default)
2. API payload field (per-request override)
3. CLI option (command-line flag)
4. Config file (`~/.config/jji/config.toml` per-server setting)

Client-only transport settings and server-only deployment settings stay scoped to their owning interface.

When adding a new analysis setting:
1. Add the field to `Settings` in `config.py`
2. Add the corresponding request field to `BaseAnalysisRequest` (or `AnalyzeRequest`) in `models.py`
3. Add the field to `_merge_settings()` in `main.py` so request values override env defaults
4. Add the CLI option to the relevant command in `cli/main.py`
5. Add the field to `ServerConfig` in `cli/config.py`

Exceptions (server-level only, no payload equivalent):
- `ADMIN_KEY` — server-only bootstrap secret for admin superuser authentication; never expose via request payloads, CLI flags, or shared config files. Rotating `ADMIN_KEY` only affects the bootstrap admin login — delegated admin API keys use `JJI_ENCRYPTION_KEY` for HMAC hashing and are not affected by `ADMIN_KEY` rotation.
- `ALLOWED_USERS` — server-only comma-separated allow list of usernames permitted to create/modify data; empty = open access (backward compatible); admin users always bypass; never expose via request payloads or CLI flags. Note: this is a trusted-network access guard, not a cryptographic security boundary — enforcement reads the client-supplied `jji_username` cookie, so protection relies on network-level trust rather than server-verified identity
- `DEBUG` — server reload toggle
- `ENABLE_GITHUB_ISSUES` — server capability toggle for GitHub issue creation
- `ENABLE_REPORTPORTAL` — server capability toggle for Report Portal integration
- `JJI_ENCRYPTION_KEY` — server-only secret for at-rest encryption AND HMAC secret for delegated admin API key hashes; never expose via request payloads, CLI flags, or shared config files. **Rotating this key invalidates both encrypted data (tokens) and all stored delegated admin API key hashes** — operators must re-issue delegated admin API keys after rotation
- `LOG_LEVEL` — server log verbosity
- `PUBLIC_BASE_URL` — trusted server-only origin for building absolute links; never derive from request headers to prevent host-header injection
- `METADATA_RULES_FILE` — server-only path to metadata classification rules file
- `SECURE_COOKIES` — server-only deployment toggle for HTTPS cookie flags (default: True, set False for local HTTP dev)
- `TRUST_PROXY_HEADERS` — server-only trust toggle for reverse-proxy user identification; only enable behind a trusted proxy
- `VAPID_CLAIM_EMAIL` — server-only contact email for VAPID claims (Web Push notifications)
- `VAPID_PRIVATE_KEY` — server-only VAPID private key for Web Push notifications; never expose via request payloads, CLI flags, or shared config files
- `VAPID_PUBLIC_KEY` — server-only VAPID public key for Web Push notifications; auto-generated with `VAPID_PRIVATE_KEY` if not set
- Security-sensitive credentials for preview/create-issue endpoints (`GITHUB_TOKEN`, `TESTS_REPO_URL`, Jira credentials, `REPORTPORTAL_URL`, `REPORTPORTAL_API_TOKEN`, `REPORTPORTAL_PROJECT`) — these use deployment config, not per-request overrides

### Sensitive Data Handling

Sensitive data (passwords, API tokens, credentials) must be:
1. **Encrypted at rest** — use `encrypt_sensitive_fields()` before storing to the database
2. **Stripped from responses** — use `strip_sensitive_from_response()` before returning to API consumers
3. **Never logged** — do not log passwords, tokens, or credentials at any log level

Sensitive fields: `jenkins_password`, `jenkins_user`, `jira_api_token`, `jira_pat`, `jira_email`, `github_token`, `tests_repo_token`, `reportportal_api_token`, `vapid_private_key`

Encryption uses Fernet (AES-128-CBC + HMAC-SHA256). Set `JJI_ENCRYPTION_KEY` env var for production; falls back to an auto-generated file-based key under `$XDG_DATA_HOME/jji/.encryption_key` (default: `~/.local/share/jji/.encryption_key`) for development.
