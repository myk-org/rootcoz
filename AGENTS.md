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

**`tox` must pass before every commit. No exceptions.** Requires [Helm 3](https://helm.sh/docs/intro/install/) for the `chart` environment.

Run all tests:

```bash
uvx --with tox-uv tox
```

This runs all three environments:
- `backend` — Python tests via `uv run pytest tests/ -q`
- `frontend` — Frontend build (`vite build`) + Vitest tests (`npm test`)
- `chart` — Helm lint + template smoke tests (auto-installs Helm 3 if missing)

Individual environments:

```bash
uvx --with tox-uv tox -e backend    # Python only
uvx --with tox-uv tox -e frontend   # Frontend only
uvx --with tox-uv tox -e chart      # Helm chart only
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
- **AI Integration**: Pi SDK sidecar — Node.js service wrapping the Pi coding agent SDK. Provides Claude (via Vertex), Cursor (via acpx), Gemini, and optional CLI models under the same provider names when `CLI_AGENTS` is set (same pattern as `ACPX_AGENTS`). No bash in analysis/chat tool lists. `AI_PROVIDER` env var selects provider (`claude` / `gemini` / `cursor`).
- **CLI**: `rootcoz` CLI tool for querying the API — run `rootcoz --help` for available commands. Sub-commands include `results`, `history`, `comments`, `classifications`, `metadata`, `failure`, `chat`, `reports`, `config`, `auth`, `admin`, `admin-chat`

### Backend Module Layout

```text
src/rootcoz/
  engine/                   # CI-agnostic analysis core
    core.py                 # Failure grouping, AI orchestration (per-group with agent
                            # system_prompt + cross-failure detection), prompt building,
                            # JSON response parsing, deduplication. Has ZERO knowledge
                            # of any specific CI system.
    chat.py                 # Chat engine: workspace, AI session, prompt builder
  sources/                  # CI source plugins (data fetching)
    base.py                 # CISource ABC, CISourceResult, WorkspaceFile, and shared workspace setup helpers
    jenkins_source.py       # Jenkins plugin: JenkinsSource, analyze_job, analyze_child_job,
                            # wait_for_jenkins_completion, Jenkins helpers (handle_jenkins_exception, extract_*, etc.)
    file_source.py          # JUnit XML plugin: FileSource
    raw_source.py           # Raw failure list plugin: RawSource
    prow_source.py          # Prow CI plugin: ProwSource (GCS artifacts)
    chat_workspace.py       # CI-source chat workspace population dispatcher
    prow_validation.py      # Re-export shim of rootcoz.prow_validation for plugin imports
    registry.py             # CI source plugin registry: analysis_type → CISource class mapping
  exporters/                # Exporter plugins (push results to external systems)
    base.py                 # Exporter ABC, ExportContext, ExporterResult
    reportportal.py         # Report Portal exporter: ReportPortalClient
  prow_validation.py        # Canonical Prow validators (+ URL helper re-exports for compatibility)
  url_utils.py              # Cross-cutting HTTP(S) URL sanitization (strip userinfo, href)
  main.py                   # FastAPI app, unified POST /analyze endpoint, background tasks
  xml_enrichment.py          # JUnit XML parsing: extract_all_tests_from_xml (pass/skip/fail),
                            # extract_failures_from_xml/extract_test_failures wrappers,
                            # apply_analysis_to_xml, build_enriched_xml
  models.py                 # Pydantic request/response models (BaseTestEntry, FailedTest, etc.)
  config.py                 # Settings (env vars)
  storage.py                # SQLite persistence (includes test_entries table)
  agents/                    # Built-in pi agents (e.g. test-analyzer) copied to workspace .pi/agents/
  ai_client.py              # AI provider constants and usage recording setup
  sidecar-helper/            # Pi SDK sidecar service (Node.js/TypeScript)
    src/server.ts           # Thin wrapper calling @myk-org/pi-sidecar startSidecar()
    src/http-tools-mcp.ts   # Stdio MCP server wrapping HTTP custom tools for CLI/acpx
  cli/                      # CLI client (rootcoz command)
  peer_analysis.py          # Multi-AI peer debate loop
  ...                       # Other modules (jira, github_issues, monitoring, etc.)
```

**Dependency direction:** `main` → `sources/` + `engine/` + `exporters/`. `sources/` → `engine/`. `engine/` does NOT import `sources/` or `exporters/`. `engine/core.py` has a lazy import of `peer_analysis` (only when `peer_ai_configs` is set). Adding a new CI plugin means adding a file under `sources/` and registering it in `sources/registry.py`. Adding a new exporter means adding a file under `exporters/` and updating `_create_exporter()` in `main.py`. `engine/core.py` stays untouched.

### Frontend Patterns

- **State**: Page-scoped `useReducer` (e.g., `ReportContext` for the report page) — each page owns its own context; do NOT introduce global state (Redux, Zustand, etc.)
- **API**: Centralized `api.get/post/put/delete` wrapper in `lib/api.ts` — do NOT use raw `fetch` calls
- **User identification**: Session-based — all users must register (auto-generated API key) and log in (username + API key → session cookie). The `rootcoz_username` cookie is set for display, but authentication is enforced via `rootcoz_session` cookie or Bearer token. When `TRUST_PROXY_HEADERS` is enabled, trusted `X-Forwarded-User` satisfies authentication without registration.
- **Auth roles & permissions**:
  - Four roles: `viewer`, `reviewer`, `operator`, `admin`. A bootstrap `admin` superuser (via `ADMIN_KEY` env var) always exists outside the DB. `DEFAULT_USER_ROLE` env var controls the default role for new registrations (default: `reviewer`).
  - All API endpoints require authentication except public paths (`/login`, `/health`, `/api/health`, `/api/auth/register`, `/api/auth/login`, `/api/auth/needs-key`, `/api/auth/pending-status`, `/api/releases/latest`, `/metrics`, `/pending`, `/favicon.ico`, `/favicon.svg`, `/sw.js`, `/openapi.json`, `/docs`, `/redoc`). `/api/releases/latest` is intentionally public — it only proxies GitHub release metadata (version, changelog) with no sensitive data. `/openapi.json`, `/docs`, and `/redoc` are public so API consumers can discover the schema without auth.
  - CORS preflight (OPTIONS) requests bypass authentication on all endpoints.
  - **Viewers** can: view jobs/results only. Cannot chat, comment, re-analyze, or modify anything.
  - **Reviewers** can: everything viewers can, plus chat about jobs, comment on jobs, register, login, rotate their own API key, manage their own tracker tokens.
  - **Operators** can: everything reviewers can, plus submit NEW analyses (`POST /analyze`), re-analyze any job, delete their own jobs.
  - **Admins** can: everything operators can, plus delete any job, rotate any user's key (`POST /api/admin/users/{username}/rotate-key`), create/delete users, change user roles, manage `can_view_reports`, access admin-only endpoints (`/api/admin/*`). Admins always have reports access.
  - **`can_view_reports`** (DB flag, default false, orthogonal to role): when true, the user may call `/api/reports/*`. Non-admins reload the flag from the users table on each request (so grants/revokes apply without session invalidation); admins have effective access (`True`) without depending on the stored column. Managed via `PUT /api/admin/users/{username}/can-view-reports`, admin user create (`can_view_reports` in body), CLI `admin users create --can-view-reports` / `admin users set-can-view-reports`, and the admin UI. Exposed on `request.state.can_view_reports`, `GET /api/auth/me`, and `POST /api/auth/login`.
- **Real-time updates**: Server-Sent Events (SSE) push real-time updates to the frontend. A polling fallback activates after sending a chat message if the SSE connection is dead, and cancels once SSE delivers an event. Backend broadcasts via per-connection `asyncio.Event` objects. Available SSE streams:
  - `/api/navbar/stream` — navbar badge counts (active analyses, unread mentions)
  - `/api/dashboard/stream` — dashboard job list changes
  - `/api/results/{job_id}/stream` — per-job status changes
  - `/api/results/{job_id}/comments/stream` — per-job comment changes
  - `/api/admin/token-usage/stream` — token usage data changes
  - `/api/chat/{job_id}/stream` — per-job chat message changes
  - `/api/admin/logs/stream` — real-time server log tailing (admin only)
- **Reports API**: Analytics endpoints for aggregated metrics (requires admin **or** `can_view_reports`):
  - `GET /api/reports/totals?team=&tier=&version=&from=&to=` — total jobs, failures, reviewed with per-job detail list
  - `GET /api/reports/classification-overrides?...` — user classification overrides grouped by from→to transition
  - `GET /api/reports/issues-created?...` — GitHub/Jira issues created from analysis results
- **Sparse result fields**: `GET /results/{job_id}?fields=status,result.summary,...` returns only allowlisted paths (full values, never truncated). Omit `fields` for the full response. Discover paths via `GET /api/results/fields` (CLI: `rootcoz results fields`) or pass `--fields` to `rootcoz results show`. Allowlist and filter helper live in `result_fields.py`.

### Server Settings Page

Every new environment variable added to `Settings` in `config.py` **MUST** also be registered in the server settings metadata in `main.py`:
1. Add the field to the appropriate category in `_SETTINGS_CATEGORIES`
2. Add to `_SENSITIVE_SETTINGS` if it contains passwords/tokens/keys
3. Add to `_RESTART_REQUIRED_SETTINGS` if it requires server restart to take effect

### Auto-Generated Documentation

The `docs/` directory is **auto-generated** by [docsfy](https://github.com/myk-org/docsfy). **NEVER edit files in `docs/` manually** — all changes will be overwritten. To update documentation, modify source code and regenerate with docsfy, or edit `AGENTS.md` / `README.md` for project-level docs.

### Project Customization (`.rootcoz/` folder)

Analyzed repositories can provide project-specific customization files under a `.rootcoz/` directory:

```text
<analyzed-repo>/
  .rootcoz/
    settings.json                  # Per-repo AI analysis settings (see below)
    ROOTCOZ_PROMPT.md              # Custom analysis instructions for the AI
    ROOTCOZ_HISTORY_PROMPT.md      # Custom history analysis instructions
    ROOTCOZ_ISSUE_PROMPT.md        # Custom issue generation prompt
    agents/                        # Custom pi agents for this project
    skills/                        # Custom pi skills for this project
    extensions/                    # Custom pi extensions for this project
```

- **`settings.json`**: Optional non-sensitive analysis settings for the test repo. Validated against the JSON Schema in `src/rootcoz/schemas/rootcoz-settings.schema.json` (Pydantic model `RootcozRepoSettings`). Allowed keys only: `ai_provider`, `ai_model`, `ai_call_timeout`, `max_concurrent_ai_calls`, `peer_ai_configs`, `peer_analysis_max_rounds`, `additional_repos`. No secrets (tokens rejected). Priority for all allowed keys: request → `settings.json` → server. Loaded after the test repo is cloned (`rootcoz_repo_settings.py`).
- **Prompt files**: `build_resources_section()` and `build_prompt_sections()` in `engine/core.py` scan `<repo>/.rootcoz/` for `ROOTCOZ_PROMPT.md` and `ROOTCOZ_HISTORY_PROMPT.md`. The issue prompt (`ROOTCOZ_ISSUE_PROMPT.md`) is fetched via the GitHub Contents API from `.rootcoz/` in `main.py`.
- **Pi resources**: After cloning repos (analysis, re-analysis, and chat paths), `.rootcoz/{agents,skills,extensions}/` are copied into `<workspace>/.pi/` via `copy_rootcoz_pi_resources()` so pi's `DefaultResourceLoader` discovers them. Built-in agents from `src/rootcoz/agents/` are then copied via `copy_builtin_agents_to_workspace()` — existing user agent files with the same name are NOT overwritten (user agents take precedence). Analysis sessions use tools `["read", "ls", "find", "grep"]` only (no `subagent`); the `test-analyzer` agent file is loaded as `system_prompt`. Chat sessions still include `subagent`.
- This is a **breaking change** — the previous legacy prompt filenames in the repo root are no longer supported. Only `.rootcoz/` is recognized.

### AI Tool Access (MANDATORY)

**NEVER embed data in the AI prompt.** All data the AI needs MUST be written to files in the job workspace. The prompt only tells the AI which files exist, what they contain, and that reading them is MANDATORY.

**DO:**
- Write data to files in the job workspace (e.g., `console-output.txt`, `other-failure-groups.txt`)
- Tell the AI in the prompt: "MANDATORY: Read file X before analyzing. It contains Y."
- Expose API endpoints the AI can curl
- Provide skill files documenting available tools
- Let the AI query, explore, and interpret data on its own

**DON'T:**
- Embed data directly in the prompt (console output, cross-reference summaries, etc.)
- Pre-query the database and stuff results into the prompt
- Summarize or filter data before the AI sees it
- Make decisions about what data the AI needs — let the AI decide
- Truncate, cap, or slice data before giving it to the AI — in prompts OR in workspace files

**File-based data pattern:**
```python
# CORRECT — write to file, tell AI to read it
filepath = workspace / "other-failure-groups.txt"
filepath.write_text(content)
prompt = f"MANDATORY: Read {filepath} before analyzing."

# WRONG — embed in prompt
prompt = f"Here is the data: {content}"
```

**Exceptions — when embedding in the prompt IS allowed:**
- **Content formatting** (e.g., `bug_creation.py`): When the AI is formatting already-analyzed data into structured text (issue titles, bodies), not performing new analysis. The input is fully known and the output is a template — no exploration needed.

### AI Chat Tool Restriction (MANDATORY)

AI chat sessions MUST use restricted tool sets — **never give bash access**.

- **Allowed builtin tools**: `["read", "ls", "find", "grep", "subagent"]` — filesystem browsing + delegating to project-provided agents
- **Data access**: Use HTTP-backed custom tools via pi-sidecar (pi-sidecar ≥4.3.4). CLI/acpx nested agents do not inherit those tools — rootcoz writes the same path-specific HTTP tool list as cwd MCP (`.cursor/mcp.json`, `.mcp.json`, `.gemini/settings.json`) executed by `sidecar-helper` `http-tools-mcp.js`. Sidecar 4.3.4+ points nested CLI `--workspace` at the session cwd so those files load. Analysis, job chat, and admin chat each pass their existing builder output (Jira/GitHub only when credentials exist).
- **Never**: `bash`, `exec`, `write`, `edit` — the AI must not execute arbitrary commands or modify files
- Custom tools define exactly which API endpoints the AI can call — nothing else is reachable
- Per-job chat tools: `get_job_result`, `get_job_comments`, `get_job_tests`, `search_jira`, `get_jira_issue`, `search_github_issues`, `get_github_issue` (conditional on user credentials)
- Admin chat tools: `db_schema`, `db_query` (read-only SQL against the database), `get_report_totals`, `get_classification_overrides`, `get_issues_created` (pre-built analytics reports), `save_report` (generate downloadable HTML report)

### CLI Parity

Every new API endpoint MUST also be supported via the `rootcoz` CLI tool. When adding a new endpoint:
1. Add the client method to `src/rootcoz/cli/client.py`
2. Add the CLI command to `src/rootcoz/cli/main.py`
3. Add tests for both in `tests/test_cli_client.py` and `tests/test_cli_main.py`

**Exceptions (no CLI equivalent needed):**
- SSE streaming endpoints (`/api/navbar/stream`, `/api/dashboard/stream`, `/api/results/*/stream`, `/api/admin/token-usage/stream`, `/api/chat/*/stream`, `/api/admin/logs/stream`) — CLI is a one-shot tool, not a long-lived stream consumer. Equivalent GET endpoints remain available for CLI use.
- SPA bootstrap helpers (`/api/auth/needs-key`) — browser-only identity probes with no CLI use case

### AI Provider/Model Resolution

AI provider and model are resolved in this order (first non-empty wins):
1. Per-request value (`ai_provider`/`ai_model` in request body)
2. `.rootcoz/settings.json` in the cloned test repo (when a tests repo is used)
3. Settings DB value (admin server settings page → AI category)
4. Environment variable (`AI_PROVIDER`/`AI_MODEL`)

Other keys allowed in `.rootcoz/settings.json` (`ai_call_timeout`,
`max_concurrent_ai_calls`, `peer_ai_configs`, `peer_analysis_max_rounds`,
`additional_repos`) use the same order: request → `settings.json` → server.

When not configured, error messages are role-aware: admins are pointed to Server Settings → AI, users are told to contact an administrator. If a tests repo URL is set but AI is still unset at submit time, resolution may defer until after clone so `settings.json` can supply it. Unsupported `ai_provider` values fail immediately (even when deferring for a missing model).

### AI System Identity

`rootcoz-ai` is the reserved system identity for all AI-originated actions (auto-review, classification). The identity string is defined as `AI_SYSTEM_USERNAME` in `storage.py` — all code must use this constant instead of hardcoding the string. It is blocked from user registration. The `POST /history/classify` endpoint uses `source="ai"` in the request body to identify AI callers, and stores `created_by = "rootcoz-ai"` for attribution. A backend guard prevents AI from overriding user classifications.

### Test Entries & Zero-Failure Fast Path

All test outcomes (passed, skipped, failed) are stored in the `test_entries` table with `child_job_name`/`child_build_number` scoping. Counts are cached in `result_json` for dashboard display. The `BaseTestEntry` model (base class of `FailedTest`) provides the shared `test_name`/`duration`/`status` fields.

- **Paginated API**: `GET /api/results/{job_id}/tests?status=passed&status=skipped&offset=0&limit=50` (viewer+ auth, CLI: `rootcoz results tests`)
- **Zero-failure fast path**: When `CISourceResult.skip_analysis=True` (e.g. Jenkins SUCCESS, file with no failures), the pipeline skips AI analysis, repo cloning, and workspace setup. Test entries are still saved and counts cached. Metadata assignment, SSE notifications, and auth enforcement still apply.
- **Jenkins FAILURE/UNSTABLE/ABORTED with empty test report**: NOT fast path — console-only analysis runs (preserves existing behavior).

### Failure Deduplication

When multiple tests fail with the same error:
1. Failures are grouped by error signature (SHA-256 hash of normalized error + stack trace)
2. One `call_ai_once` per unique error with `test-analyzer` agent as system_prompt (cross-failure patterns detected in a final pass)
3. Analysis is applied to all failures with matching signature
4. Signatures are normalized before hashing (timestamps, UUIDs, pod name suffixes, build numbers stripped)

### Jira Integration (Optional)

When configured, searches Jira for existing bugs matching PRODUCT BUG failures:
1. AI generates search keywords during analysis
2. Keywords search Jira (configurable issue type, summary search)
3. AI evaluates each candidate's relevance
4. Only relevant matches are attached to the result
5. Jira errors never crash the pipeline — all failures are swallowed gracefully

### Exporter Plugin Architecture

Exporters push analysis results to external systems. Each exporter implements the `Exporter` ABC in `exporters/base.py`.

- **Generic push endpoint**: `POST /results/{job_id}/push/{plugin_name}` (operator+ role)
- **Legacy endpoint**: `POST /results/{job_id}/push-reportportal` (backward compatible)
- **Exporters list**: `GET /api/exporters` — lists available exporters with enabled status
- **Auto-push**: When `AUTO_PUSH_EXPORTERS` is set and all failures are auto-reviewed, results are pushed to configured exporters
- **CLI**: `rootcoz push --plugin <name>`, `rootcoz exporters`, `rootcoz push-reportportal` (backward compat)

### Report Portal Integration (Optional)

When `ENABLE_REPORTPORTAL=true`, users can push test classifications back to Report Portal via the `push-reportportal` endpoint, the generic `push --plugin reportportal` CLI command, or the generic push endpoint.

### Auto-Review

After any completed analysis, each failure is checked against previous analyses of the same `job_name` for the same `test_name`. If the `error_signature` matches exactly **and the previous failure was reviewed by a human** (not auto-reviewed by `rootcoz-ai`), the failure is auto-reviewed (marked reviewed by `rootcoz-ai`). This human-review gate prevents auto-review chains from cascading indefinitely without human validation. The auto-review comment includes a clickable link to the previous job when `PUBLIC_BASE_URL` is set.

### Feedback System

Users submit feedback (bugs, feature requests) via the FeedbackDialog component. Feedback is previewed with AI-generated issue content, then created as a GitHub issue. This replaces the old "Report Bug" flow.

### Pi SDK Sidecar

Node.js service running inside the same container, wrapping the Pi coding agent SDK for all AI calls.

**Architecture:**
- HTTP API on `127.0.0.1:9100` (localhost only, `0.0.0.0` in `DEV_MODE`)
- Extensions loaded by path (not from settings.json — no orchestrator):
  - `acpx-provider` — Cursor models via `acpx` CLI
  - `pi-vertex-claude` — Claude models via Google Vertex AI
  - `cli-provider` — CLI agent models via local pi CLI (enabled by `CLI_AGENTS` env var)
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
- Provider mapping: `cursor` → `acpx-cursor` (default) or `cli-cursor` (when the chosen model is from CLI / `CLI_AGENTS`); `claude` → `google-vertex-claude` or `cli-claude`; `gemini` → `google` or `cli-gemini`. Public API uses only `claude` / `gemini` / `cursor` — never `*-cli` provider names.

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

### Explicit OpenAPI operation IDs

Every new FastAPI route that is included in the OpenAPI schema **MUST** set an explicit `operation_id` (camelCase, derived from the handler name). Do not rely on FastAPI auto-generated operationIds (they produce unstable `*_get` / path-suffix forms). Routes with `include_in_schema=False` (SPA/static) skip this.

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

**Helm chart parity (bootstrap-only):** When adding environment variables that **cannot** be configured via Server Settings UI, also update the Helm chart:

- Sidecar AI keys (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `CURSOR_API_KEY`, Vertex/Cursor mount paths)
- Bootstrap secrets (`ADMIN_KEY`, `ROOTCOZ_ENCRYPTION_KEY`)
- Container runtime env (`XDG_*`, auto-derived `SECURE_COOKIES`, `PUBLIC_BASE_URL` derivation)
- Routing templates (Route/Ingress)

DB-configurable `Settings` fields do **not** require Helm chart changes — users set those after login in Server Settings.

Exceptions (server-level only, no payload equivalent):
- `ADMIN_KEY` — server-only bootstrap secret for admin superuser authentication; never expose via request payloads, CLI flags, or shared config files. Rotating `ADMIN_KEY` only affects the bootstrap admin login — delegated admin API keys use `ROOTCOZ_ENCRYPTION_KEY` for HMAC hashing and are not affected by `ADMIN_KEY` rotation.
- `DEFAULT_USER_ROLE` — server-only default role for new user registrations (`viewer`, `reviewer`, or `operator`); never expose via request payloads or CLI flags
- `ADMIN_WAIT_APPROVE_MSG` — server-only custom message appended to admin approval notices; tells users how to get approved
- `ALLOWED_USERS` — server-only comma-separated allow list of usernames permitted to create/modify data; empty = open access (backward compatible); admin users always bypass; never expose via request payloads or CLI flags. All users must authenticate (via API key session, Bearer token, or trusted proxy header when `TRUST_PROXY_HEADERS` is enabled) before the allow list is evaluated.
- `DEBUG` — server reload toggle
- `ENABLE_GITHUB_ISSUES` — server capability toggle for GitHub issue creation
- `ENABLE_REPORTPORTAL` — server capability toggle for Report Portal integration
- `RP_PUSH_CLASSIFICATIONS` — server-only toggle for including classification (defect type mapping) in Report Portal pushes (default: True)
- `RP_PUSH_ROOTCOZ_URL` — server-only toggle for including rootcoz analysis URL comment in Report Portal pushes (default: True)
- `RP_PUSH_TRACKER_LINKS` — server-only toggle for including Jira/GitHub issue links as external system issues in Report Portal pushes (default: True)
- `ENABLE_AUTO_REVIEW` — server capability toggle for auto-review of matching failures; when disabled, failures are never automatically marked as reviewed
- `AUTO_PUSH_EXPORTERS` — server-only comma-separated list of exporter plugins to auto-push to when all failures are reviewed (e.g. `reportportal`); empty disables auto-push; requires `ENABLE_AUTO_REVIEW` to also be enabled
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

Request-tunable settings with full config parity (env, API payload, CLI, config.toml, Server Settings):
- `prow_url` — Prow Deck base URL (server default; overridable per request via `UnifiedAnalyzeRequest`)
- `gcs_bucket` — GCS bucket for Prow artifacts (server default; overridable per request)

Request-only fields on `UnifiedAnalyzeRequest` (per-build, no server-level default):
- `prow_job_name` — Prow job name for prow source analyses
- `build_id` — Prow build ID (numeric string; may exceed JavaScript safe integer range)
- `gcs_prefix` — GCS path prefix, unique per Prow build (e.g. `pr-logs/pull/org_repo/pr/job/build_id`). When empty, auto-resolves via prowjob.json or pr-logs/directory pointer.
- `raw_xml` — raw JUnit XML content for file source
- `failures` — raw failure list for raw source

### Sensitive Data Handling

Sensitive data (passwords, API tokens, credentials) must be:
1. **Encrypted at rest** — use `encrypt_sensitive_fields()` before storing to the database
2. **Stripped from responses** — use `strip_sensitive_from_response()` before returning to API consumers
3. **Never logged** — do not log passwords, tokens, or credentials at any log level

Sensitive fields: `jenkins_password`, `jenkins_user`, `jira_api_token`, `jira_pat`, `jira_email`, `github_token`, `tests_repo_token`, `reportportal_api_token`, `vapid_private_key`

Encryption uses Fernet (AES-128-CBC + HMAC-SHA256). Set `ROOTCOZ_ENCRYPTION_KEY` env var for production; falls back to an auto-generated file-based key under `$XDG_DATA_HOME/rootcoz/.encryption_key` (default: `~/.local/share/rootcoz/.encryption_key`) for development.

**Exception:** `POST /api/auth/register` returns the raw API key once at registration time. Response includes `Cache-Control: no-store` to prevent caching.
