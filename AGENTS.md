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

- **Backend**: Python + FastAPI + SQLite (aiosqlite)
- **Frontend**: Vite + React 19 + TypeScript + Tailwind CSS + shadcn/ui (in `/frontend/`)
- **AI Integration**: Pi SDK sidecar — Node.js service wrapping the Pi coding agent SDK. Provides Claude (via Vertex), Cursor (via acpx), and Gemini models. No direct CLI dependencies. `AI_PROVIDER` env var selects provider.
- **CLI**: `rootcoz` CLI tool for querying the API — run `rootcoz --help` for available commands

### Backend Module Layout

```text
src/rootcoz/
  engine/                   # CI-agnostic analysis core
    core.py                 # Failure grouping, AI CLI orchestration, prompt building,
                            # JSON response parsing, deduplication. Has ZERO knowledge
                            # of any specific CI system.
    chat.py                 # Chat engine: workspace, AI session, prompt builder
  sources/                  # CI source plugins (data fetching)
    base.py                 # CISource ABC + CISourceResult dataclass
    jenkins_source.py       # Jenkins plugin: JenkinsSource, analyze_job, analyze_child_job,
                            # wait_for_jenkins_completion, Jenkins helpers (handle_jenkins_exception, extract_*, etc.)
    file_source.py          # JUnit XML plugin: FileSource
    raw_source.py           # Raw failure list plugin: RawSource
  main.py                   # FastAPI app, unified POST /analyze endpoint, background tasks
  models.py                 # Pydantic request/response models
  config.py                 # Settings (env vars)
  storage.py                # SQLite persistence
  ai_client.py              # AI provider constants and usage recording setup
  sidecar-helper/            # Pi SDK sidecar service (Node.js/TypeScript)
    src/server.ts           # Thin wrapper calling @myk-org/pi-sidecar startSidecar()
  cli/                      # CLI client (rootcoz command)
  peer_analysis.py          # Multi-AI peer debate loop
  chat_scripts/             # AI-accessible scripts for chat workspace
    rootcoz_chat_job.py     # Query job data (failures, analyses, comments)
    rootcoz_chat_jira.py    # Search/query Jira issues
    rootcoz_chat_github.py  # Search/query GitHub issues/PRs
    rootcoz_chat_db.py      # Read-only SQL queries for admin chat analytics
  ...                       # Other modules (jira, github_issues, monitoring, etc.)
```

**Dependency direction:** `main` → `sources/` + `engine/`. `sources/` → `engine/`. `engine/` does NOT import `sources/`. `engine/core.py` has a lazy import of `peer_analysis` (only when `peer_ai_configs` is set). Adding a new CI plugin means adding a file under `sources/` and a dispatch branch in `main.py` — `engine/core.py` stays untouched.

### Frontend Patterns

- **State**: Page-scoped `useReducer` (e.g., `ReportContext` for the report page) — each page owns its own context; do NOT introduce global state (Redux, Zustand, etc.)
- **API**: Centralized `api.get/post/put/delete` wrapper in `lib/api.ts` — do NOT use raw `fetch` calls
- **User identification**: Session-based — all users must register (auto-generated API key) and log in (username + API key → session cookie). The `rootcoz_username` cookie is set for display, but authentication is enforced via `rootcoz_session` cookie or Bearer token. When `TRUST_PROXY_HEADERS` is enabled, trusted `X-Forwarded-User` satisfies authentication without registration.
- **Auth roles & permissions**:
  - Two roles: `user` and `admin`. A bootstrap `admin` superuser (via `ADMIN_KEY` env var) always exists outside the DB.
  - All API endpoints require authentication except public paths (`/register`, `/health`, `/api/health`, `/api/auth/register`, `/api/auth/login`, `/api/auth/needs-key`, `/api/releases/latest`, `/metrics`). `/api/releases/latest` is intentionally public — it only proxies GitHub release metadata (version, changelog) with no sensitive data.
  - CORS preflight (OPTIONS) requests bypass authentication on all endpoints.
  - **Users** can: register, login, rotate their own API key (`POST /api/auth/rotate-key`), manage their own tracker tokens, submit analyses.
  - **Admins** can: everything users can, plus rotate any user's key (`POST /api/admin/users/{username}/rotate-key`), create/delete/promote/demote users, access admin-only endpoints (`/api/admin/*`).
- **Real-time updates**: Server-Sent Events (SSE) push real-time updates to the frontend — no polling. Backend broadcasts via per-connection `asyncio.Event` objects. Available SSE streams:
  - `/api/navbar/stream` — navbar badge counts (active analyses, unread mentions)
  - `/api/dashboard/stream` — dashboard job list changes
  - `/api/results/{job_id}/stream` — per-job status changes
  - `/api/results/{job_id}/comments/stream` — per-job comment changes
  - `/api/admin/token-usage/stream` — token usage data changes
  - `/api/chat/{job_id}/stream` — per-job chat message changes

### Server Settings Page

Every new environment variable added to `Settings` in `config.py` **MUST** also be registered in the server settings metadata in `main.py`:
1. Add the field to the appropriate category in `_SETTINGS_CATEGORIES`
2. Add to `_SENSITIVE_SETTINGS` if it contains passwords/tokens/keys
3. Add to `_RESTART_REQUIRED_SETTINGS` if it requires server restart to take effect

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

Every new API endpoint MUST also be supported via the `rootcoz` CLI tool. When adding a new endpoint:
1. Add the client method to `src/rootcoz/cli/client.py`
2. Add the CLI command to `src/rootcoz/cli/main.py`
3. Add tests for both in `tests/test_cli_client.py` and `tests/test_cli_main.py`

**Exceptions (no CLI equivalent needed):**
- SSE streaming endpoints (`/api/navbar/stream`, `/api/dashboard/stream`, `/api/results/*/stream`, `/api/admin/token-usage/stream`, `/api/chat/*/stream`) — CLI is a one-shot tool, not a long-lived stream consumer. Equivalent GET endpoints remain available for CLI use.
- SPA bootstrap helpers (`/api/auth/needs-key`) — browser-only identity probes with no CLI use case

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

### Pi SDK Sidecar

Node.js service running inside the same container, wrapping the Pi coding agent SDK for all AI calls.

**Architecture:**
- HTTP API on `127.0.0.1:9100` (localhost only, `0.0.0.0` in `DEV_MODE`)
- Extensions loaded by path (not from settings.json — no orchestrator):
  - `acpx-provider` — Cursor models via `acpx` CLI
  - `pi-vertex-claude` — Claude models via Google Vertex AI
- Built-in providers: Google (Gemini), Anthropic (Claude via API key)
- `SettingsManager.inMemory()` — no settings.json discovery

**Session lifecycle:**
- `POST /sessions` — create session with provider, model, system prompt, cwd
- `POST /sessions/:id/prompt` — send message, get response (clean text, no chain-of-thought)
- `POST /sessions/:id/abort` — cancel in-progress prompt
- `DELETE /sessions/:id` — cleanup session
- `GET /models` — list all available models
- `POST /models/refresh` — re-discover models from extensions
- `GET /health` — returns 503 during startup model discovery, 200 when ready

**Python client (`ai_client.py` → `pi-sidecar-client`):**
- `call_ai_once()` — single-shot AI call with automatic session cleanup
- `call_ai()` — multi-turn AI call (caller manages session lifecycle)
- `AIResult.record_usage()` — record token usage to DB
- Provider mapping: `cursor` → `acpx-cursor`, `claude` → `google-vertex-claude`, `gemini` → `google`

**Container integration:**
- Dockerfile: sidecar build stage, `acpx` CLI installed globally
- Entrypoint: starts sidecar in background, compiles TypeScript in dev mode
- Process supervision: trap + monitor kills container if sidecar dies
- Healthcheck covers both Python backend and sidecar

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
4. Config file (`~/.config/rootcoz/config.toml` per-server setting)

Client-only transport settings and server-only deployment settings stay scoped to their owning interface.

When adding a new analysis setting:
1. Add the field to `Settings` in `config.py`
2. Add the corresponding request field to `BaseAnalysisRequest` (or `AnalyzeRequest`) in `models.py`
3. Add the field to `_merge_settings()` in `main.py` so request values override env defaults
4. Add the CLI option to the relevant command in `cli/main.py`
5. Add the field to `ServerConfig` in `cli/config.py`

Exceptions (server-level only, no payload equivalent):
- `ADMIN_KEY` — server-only bootstrap secret for admin superuser authentication; never expose via request payloads, CLI flags, or shared config files. Rotating `ADMIN_KEY` only affects the bootstrap admin login — delegated admin API keys use `ROOTCOZ_ENCRYPTION_KEY` for HMAC hashing and are not affected by `ADMIN_KEY` rotation.
- `ADMIN_WAIT_APPROVE_MSG` — server-only custom message appended to admin approval notices; tells users how to get approved
- `ALLOWED_USERS` — server-only comma-separated allow list of usernames permitted to create/modify data; empty = open access (backward compatible); admin users always bypass; never expose via request payloads or CLI flags. All users must authenticate (via API key session, Bearer token, or trusted proxy header when `TRUST_PROXY_HEADERS` is enabled) before the allow list is evaluated.
- `DEBUG` — server reload toggle
- `ENABLE_GITHUB_ISSUES` — server capability toggle for GitHub issue creation
- `ENABLE_REPORTPORTAL` — server capability toggle for Report Portal integration
- `ROOTCOZ_ENCRYPTION_KEY` — server-only secret for at-rest encryption AND HMAC secret for all API key hashes (admin and user); never expose via request payloads, CLI flags, or shared config files. **Rotating this key invalidates both encrypted data (tokens) and all stored API key hashes (admin and user)** — operators must re-issue all API keys after rotation. Stored sessions use plain SHA-256 hashing (no HMAC) and are NOT affected by key rotation.
- `LOG_LEVEL` — server log verbosity
- `PUBLIC_BASE_URL` — trusted server-only origin for building absolute links; never derive from request headers to prevent host-header injection
- `METADATA_RULES_FILE` — server-only path to metadata classification rules file
- `SECURE_COOKIES` — server-only deployment toggle for HTTPS cookie flags (default: True, set False for local HTTP dev)
- `TRUST_PROXY_HEADERS` — server-only trust toggle for reverse-proxy user identification; only enable behind a trusted proxy. When enabled, `X-Forwarded-User` satisfies authentication for all routes without requiring API key registration — the proxy is the authentication boundary.
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

Encryption uses Fernet (AES-128-CBC + HMAC-SHA256). Set `ROOTCOZ_ENCRYPTION_KEY` env var for production; falls back to an auto-generated file-based key under `$XDG_DATA_HOME/rootcoz/.encryption_key` (default: `~/.local/share/rootcoz/.encryption_key`) for development.

**Exception:** `POST /api/auth/register` returns the raw API key once at registration time. Response includes `Cache-Control: no-store` to prevent caching.
