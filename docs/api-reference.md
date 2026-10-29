# API Endpoint Reference

Everything the web UI does goes through the same HTTP API the CLI and the AI chat tools use, so you can script RootCoz from CI, from a shell, or from your own tooling. This page groups the endpoints by what they do, states the minimum role each one needs, and points at the live OpenAPI schema for exact field-level detail.

## Prerequisites
- A running RootCoz server. The examples use `http://localhost:8000`; see [Quickstart](quickstart.html).
- An API key. Register or log in in the web UI, or send `Authorization: Bearer <API_KEY>` with any request.
- The role your call needs: `viewer`, `reviewer`, `operator`, or `admin`. See [Managing Users and Server Settings](manage-users-and-server-settings.html).

## Quick Example

```bash
curl -H "Authorization: Bearer $ROOTCOZ_API_KEY" \
  http://localhost:8000/results/JOB_ID
```

That single authenticated GET returns the stored analysis for one job, including failures, classifications, patterns, classification overrides, tracker links, capabilities, and child jobs. It does **not** include the comment thread — call `GET /results/{job_id}/comments` separately for that.

## Step-by-Step

1. **Authenticate.**

   Send your API key as a bearer token. The `rootcoz_session` cookie set at login works too, and `X-Forwarded-User` works when the server runs behind a trusted proxy with `TRUST_PROXY_HEADERS=true`.

   ```bash
   curl -H "Authorization: Bearer $ROOTCOZ_API_KEY" \
     http://localhost:8000/api/auth/me
   ```

   `GET /api/auth/me` is the cheapest way to confirm your identity, role, and reports access. Send `OPTIONS` requests freely — CORS preflight bypasses authentication everywhere.

2. **Discover the schema instead of guessing field names.**

   | Path | What it is |
   | --- | --- |
   | `GET /openapi.json` | Full OpenAPI schema, public |
   | `GET /docs` | Swagger UI, public |
   | `GET /redoc` | ReDoc UI, public |

   These three are public so an integrator can read the contract without credentials. Every route in the schema sets an explicit camelCase `operation_id` derived from the handler name, so generated clients get stable method names such as `getJobResult` and `analyzeSubmittedJob`.

   > **Note:** This page is a curated map of the endpoints people actually call. The live schema at `/openapi.json` is the exhaustive list.

3. **Know the role ladder before you script anything.**

   | Role | Can do |
   | --- | --- |
   | `viewer` | Read jobs, results, tests, history, dashboards, reports when granted |
   | `reviewer` | Everything a viewer can, plus comments, chat, review state, classification and pattern overrides, tags, tracker links, personal tokens |
   | `operator` | Everything a reviewer can, plus submit analyses, re-analyze, abort, delete own jobs, push to exporters |
   | `admin` | Everything an operator can, plus delete any job, manage users and roles, server settings, token usage, log stream, admin chat |

   `can_view_reports` is a separate flag from role. Admins always have it; anyone else needs an admin to grant it. Most calls that create or modify data also run against the `ALLOWED_USERS` allow list, which is empty (open) by default and always bypassed for admins. The check is per-handler, not global, so it is enforced on data-modifying routes rather than as a middleware boundary; `tests/test_allow_list_coverage.py` enumerates the mutating routes and fails if one that can write data is added without the check. The exemptions are listed exactly in that test: the authentication routes, the read-only Jira and token-validation lookups, the metadata rules preview, and `DELETE /api/user/ai-credentials/{provider}` — revoking your own key stays possible even if you are off the list, otherwise being removed would leave you holding a credential you cannot remove.

   > **Warning:** A role you do not hold returns `403`, not `404`. `403 Admin access required`, `403 Reviewer access required`, and `403 Operator access required` name the exact gate you hit.

### Public and unauthenticated routes

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness, returns immediately |
| GET | `/api/health` | Detailed health including database and AI sidecar; returns 503 when the database or the sidecar (probed via its effective port) is unreachable. `checks.audit` reports `{status, write_failures}`: `degraded` once an audit row has failed to persist since process start |
| GET | `/metrics` | Prometheus metrics, including `rootcoz_audit_write_failures` |
| GET | `/api/releases/latest` | Proxies GitHub release metadata (version, changelog) plus `server_version` and `update_available` so clients can tell an update notice from a What's New popup |
| GET | `/openapi.json`, `/docs`, `/redoc` | Schema and interactive API browsers |
| POST | `/api/auth/register` | Create a user, returns the raw API key once |
| POST | `/api/auth/login` | Exchange username + API key for a session cookie |
| GET | `/api/auth/needs-key` | Browser-only identity probe |
| GET | `/api/auth/pending-status` | Poll while an account awaits approval |

### Authentication and your account

| Method | Path | Min role | Notes |
| --- | --- | --- | --- |
| GET | `/api/version` | viewer | Running version |
| POST | `/api/auth/logout` | authenticated | Clears the session cookie |
| GET | `/api/auth/me` | viewer | Username, role, `can_view_reports`, `can_use_server_providers` |
| POST | `/api/auth/rotate-key` | viewer | Rotates your own API key; the old key stops working. Known gap — see the note below |
| GET | `/api/user/ai-credentials` | viewer | Supported provider IDs with a `configured` flag and a `supports_user_keys` flag each (a stored key for a provider that stopped accepting user keys — e.g. an ambient-auth provider — stays listed with `supports_user_keys: false`). Keys are never returned |
| PUT | `/api/user/ai-credentials/{provider}` | reviewer | Store a personal key. Body is `{api_key, model}` — `model` is required (`400` without it, since the key is verified against it) and the key is only persisted when the provider returns `accepted`. A `rejected` or `inconclusive` check is not an HTTP error: it returns `200` with `ok: false`, an `outcome`, and an explanatory `message`, leaving any existing credential unchanged, so clients must check `ok` rather than the status code. `{provider}` is an exact provider ID from `GET /api/user/ai-credentials` (for example `anthropic` or `google`), not the `claude`/`gemini`/`cursor` names used for analysis settings. Unknown IDs are rejected with 400 |
| DELETE | `/api/user/ai-credentials/{provider}` | reviewer | Remove a personal provider key |
| GET | `/api/user/tokens` | viewer | Stored Jira and GitHub tracker tokens |
| PUT | `/api/user/tokens` | reviewer | Save Jira and GitHub tracker tokens. Each field is independent: a string sets it, `""` or `null` clears just that field, and an omitted field keeps its stored value, so clearing one credential leaves the others intact. Fields are `github_token`, `jira_email`, `jira_token`, and `jira_sa_token` (an Atlassian service-account credential — an ATOA-prefixed OAuth 2.0 client secret or an ATST-prefixed scoped token — which takes precedence over the regular Jira credentials). A body with none of the fields writes nothing and returns `saved: false`; anything other than a string or `null` is rejected with `400` |
| POST | `/api/validate-token` | viewer | Validate a GitHub or Jira token before saving. Jira tokens are validated against the selected method's server URL (`jira_cloud_url` with an email, `jira_server_url` without); an ATOA-prefixed value (an Atlassian OAuth 2.0 credential client secret) is validated as a service account against the scoped Atlassian gateway, so its cloud ID must be resolvable server-side and the server must have `atlassian_oauth_client_id` configured; an ATST-prefixed value (an Atlassian service-account scoped token) is sent as Bearer to the same gateway with no exchange |
| POST | `/api/jira-projects` | viewer | List Jira projects visible to the supplied credentials |
| POST | `/api/jira-security-levels` | viewer | List Jira security levels for a project |
| GET | `/api/user/filter-presets?page={dashboard\|reports}` | viewer | Saved filter presets for a page: your personal presets plus every server preset, newest first. Each entry carries `id`, `name`, `page`, `scope` (`user` or `server`), `filters` (URL query param names as-is; dashboard uses `date_from`/`date_to`, reports uses `from`/`to`), `created_at`, `updated_at` |
| POST | `/api/user/filter-presets` | viewer | Save the current filter combination as a named preset. Body is `{name, page, scope, filters}` — `scope` defaults to `user`; `scope=server` requires operator+ (`403` below that). Name is 1–100 chars, stripped; `filters` accepts at most 50 known keys per page (unknown keys → `422`). `409` when the name already exists in the same scope+page; `422` at 20 presets per page per scope |
| PUT | `/api/user/filter-presets/{preset_id}` | viewer | Rename and/or replace a preset's filters (`{name}`, `{filters}`, both optional). Personal presets: only the owner, `404` otherwise (existence is never leaked). Server presets: operator+ (`403` below that). `409` on a name conflict in the same scope+page |
| DELETE | `/api/user/filter-presets/{preset_id}` | viewer | Delete a preset. Same rules as PUT: own personal preset (`404` for anyone else's), server preset operator+ (`403` below that). Returns `204` |

Registration returns the raw API key exactly once and sends `Cache-Control: no-store`. Store it before you navigate away.

### Submitting and driving analyses

All of these continue in the background and return `202 Accepted`, except `POST /results/{job_id}/abort`, which returns `200 OK`. `POST /submit` ingests only and stops before cloning or AI work; `POST /analyze` runs the full pipeline.

| Method | Path | Min role | Body highlights |
| --- | --- | --- | --- |
| POST | `/analyze` | operator | `type` (`jenkins`, `file`, `raw`, `prow`) plus source-specific fields |
| POST | `/submit` | operator | Same body; stops at ingest, `analysis_state=submitted` |
| POST | `/results/{job_id}/analyze` | operator | Runs the AI pipeline on a submitted job; optional `ReAnalyzeRequest` |
| POST | `/re-analyze/{job_id}` | operator | Re-runs analysis with a new `ReAnalyzeRequest` |
| POST | `/api/failures/{failure_uuid}/re-analyze` | operator | Re-analyzes one failure in place, keeps the previous answer |
| POST | `/results/{job_id}/abort` | reviewer | Cancels a running analysis |

The shared body accepts the server defaults as per-request overrides:

| Field | Purpose |
| --- | --- |
| `type` | `jenkins`, `file`, `raw`, or `prow` |
| `job_name`, `build_number` | Jenkins job and build |
| `raw_xml`, `failures`, `passed_tests`, `skipped_tests` | File and raw sources |
| `prow_job_name`, `build_id`, `gcs_prefix` | Prow build identity and artifact prefix |
| `prow_url`, `gcs_bucket` | Prow server defaults, overridable per request |
| `ai_provider`, `ai_model`, `force_server_credentials` | `claude`, `gemini`, or `cursor` |
| `ai_call_timeout`, `max_concurrent_ai_calls` | AI budget for this run |
| `peer_ai_configs`, `peer_analysis_max_rounds` | Multi-AI debate settings |
| `additional_repos` | Extra repositories to clone for context |
| `tests_repo_url`, `tests_repo_token` | Tests repository used for history and context |
| `atlassian_oauth_cloud_id`, `jira_site_url`, `jira_project_key`, `jira_max_results`, `enable_jira` | Jira search during analysis. Jira credentials and per-method URLs (`jira_email`, `jira_api_token`, `jira_pat`, `jira_oauth_client_secret`, `jira_atst_token`, `jira_cloud_url`, `jira_server_url`, and the three `jira_*_ssl_verify` flags) are **Server Settings only** — analysis requests cannot carry them; the server-selected `jira_auth_mode` and its credentials always apply |
| `github_token` | GitHub access for comment enrichment |
| `raw_prompt`, `issue_prompt` | Additive prompt instructions |
| `labels`, plus name and tag fields | Metadata for the job |

> **Note:** Resolution order for provider, model, timeout, concurrency, peers, and additional repos is request → `.rootcoz/settings.json` in the cloned tests repo → server setting → environment variable. See [Configuring Analysis Context](configure-analysis-context.html).

### Reading results

| Method | Path | Min role | Query parameters |
| --- | --- | --- | --- |
| GET | `/results` | viewer | `limit` (max 100), `analysis_state` (`submitted`, `analyzed`) |
| GET | `/results/{job_id}` | viewer | `fields` — comma-separated allowlisted paths |
| GET | `/api/results/fields` | viewer | none; returns the allowlist |
| GET | `/api/results/{job_id}/tests` | viewer | `status` (repeatable: `passed`, `skipped`, `failed`), `child_job_name`, `child_build_number`, `offset`, `limit` (1–200, default 50) |
| GET | `/api/results/{job_id}/full` | viewer | `status`, `child_job_name`, `child_build_number`, `offset`, `limit` — apply to the `tests` section (same contract as `/tests`) |
| GET | `/api/failures/{failure_uuid}` | viewer | none |
| GET | `/results/{job_id}/review-status` | viewer | none |
| GET | `/api/dashboard` | viewer | `limit`, `offset` |
| GET | `/api/dashboard/filtered` | viewer | `search`, `status`, `date_from`, `date_to`, `review_status`, `analysis_state`, `limit`, `offset`, plus metadata filters `team`, `tier`, `version`, `label`, `exclude_label` (each repeatable) |
| GET | `/api/dashboard/active-count` | viewer | none |

`fields` returns full values for allowlisted paths only and is never truncated. Unknown paths return `400`, so fetch `/api/results/fields` first when you build against it.

`GET /api/results/{job_id}/full` returns the complete picture in one call: `job` metadata, `counts` (passed/skipped/failed), a flat `failures` list across child jobs with effective classifications and review state (`reviewed`, `reviewed_by`) joined server-side, a precomputed `summary` (`total_failures`, `reviewed_count`, `classifications`), a paginated `tests` page, `comments`, and `tracked_in` links. Jobs still running return `202` with the sections that exist so far.

### Review, classification, and job maintenance

| Method | Path | Min role | Body |
| --- | --- | --- | --- |
| PUT | `/results/{job_id}/reviewed` | reviewer | `test_name`, `reviewed`. |
| PUT | `/results/{job_id}/override-classification` | reviewer | `test_name`, `classification` |
| PUT | `/results/{job_id}/override-pattern` | reviewer | `test_name`, `pattern` |
| PUT | `/results/{job_id}/tags` | reviewer | JSON body `{"tags": ["a", "b"]}` — a list of strings. A non-list value is rejected with `400 tags must be a list of strings` |
| DELETE | `/results/{job_id}` | operator | none; deletes your own job, admins delete any |
| DELETE | `/api/results/bulk` | operator | `job_ids` |

Classification values are `CODE ISSUE`, `PRODUCT BUG`, and `INFRASTRUCTURE`. Pattern values are `NEW`, `REGRESSION`, `FLAKY`, `INTERMITTENT`, `KNOWN_BUG`, and `PERSISTENT`. These are the exact API values; the UI renders `KNOWN_BUG` as `Known Bug` and spaces the words for readability.

> **Note:** This table lists the role each route **actually enforces**. One route marked "Known gap" enforces no role beyond authentication — `viewer` is therefore its effective minimum, even though the documented model reserves it for `reviewer`: `POST /api/auth/rotate-key`, tracked in [#289](https://github.com/myk-org/rootcoz/issues/289). Treat it as a bug, not as a supported way to operate. `PUT /results/{job_id}/reviewed` and `PUT /results/{job_id}/tags` were previously gaps as well and now require `reviewer`. Review state is tracked per test; classification and pattern changes apply across the whole same-error group.

### Comments and mentions

| Method | Path | Min role | Notes |
| --- | --- | --- | --- |
| GET | `/results/{job_id}/comments` | viewer | Full thread for one job |
| POST | `/results/{job_id}/comments` | reviewer | `test_name`, `comment`; returns `201` |
| DELETE | `/results/{job_id}/comments/{comment_id}` | reviewer | Comment author or admin |
| POST | `/results/{job_id}/enrich-comments` | viewer | Live GitHub PR and Jira ticket status |
| POST | `/api/analyze-comment-intent` | reviewer | Classifies an incoming comment as a question or an action |
| GET | `/api/users/mentions` | viewer | Mentions for the current user |
| GET | `/api/users/mentions/unread-count` | viewer | Navbar badge count |
| POST | `/api/users/mentions/read` | viewer | Mark selected mentions read |
| POST | `/api/users/mentions/read-all` | viewer | Clear the unread badge |
| GET | `/api/users/mentionable` | viewer | Users who can be `@`-mentioned |

### Trackers, issues, and exporters

| Method | Path | Min role | Notes |
| --- | --- | --- | --- |
| GET | `/results/{job_id}/issue-prompt` | viewer | Custom `.rootcoz/ROOTCOZ_ISSUE_PROMPT.md` for the repo |
| POST | `/results/{job_id}/preview-github-issue` | reviewer | `test_name`, `include_links`, AI overrides; returns preview content |
| POST | `/results/{job_id}/preview-jira-bug` | reviewer | Same shape, Jira flavour |
| POST | `/results/{job_id}/create-github-issue` | reviewer | `test_name`, `title`, `body`; returns `201` |
| POST | `/results/{job_id}/create-jira-bug` | reviewer | Adds `jira_issue_type`; returns `201` |
| PUT | `/results/{job_id}/tracked-in` | reviewer | `test_name`, `url`, `type` |
| GET | `/results/{job_id}/tracked-in` | reviewer | Current tracker links |
| DELETE | `/results/{job_id}/tracked-in/{link_id}` | reviewer | Remove one link |
| GET | `/api/exporters` | viewer | Available exporter plugins and enabled state |
| POST | `/results/{job_id}/push/{plugin_name}` | operator | Generic push; `child_job_name` and `child_build_number` optional |
| POST | `/results/{job_id}/push-reportportal` | operator | Backward-compatible Report Portal push |

> **Note:** All four issue preview and create endpoints call `_require_reviewer`, so a `viewer` receives `403` before the allow list is consulted. They additionally require valid tracker credentials and the matching `ENABLE_GITHUB_ISSUES` or `ENABLE_JIRA_ISSUES` toggle.

### History and classification lookup

| Method | Path | Min role | Query parameters |
| --- | --- | --- | --- |
| GET | `/history/failures` | viewer | `search`, `job_name`, `classification`, `from`, `to`, `limit`, `offset`. The date bounds are named `from` and `to` on the wire, not `date_from`/`date_to` |
| GET | `/history/test/{test_name}` | viewer | `limit`, `job_name`, `exclude_job_id` |
| GET | `/history/search` | viewer | `signature`, `exclude_job_id` |
| GET | `/history/stats/{job_name}` | viewer | `exclude_job_id` |
| GET | `/history/classifications` | viewer | `test_name`, `classification`, `job_name`, `parent_job_name`, `job_id` |
| POST | `/history/classify` | reviewer | **JSON body** (not query string): `test_name`, `classification`, `reason`, `job_id`, `source`; returns `201` |

Send `source="ai"` to attribute the classification to the reserved `rootcoz-ai` identity. The backend blocks AI callers from overriding classifications a human has already set.

### Chat

| Method | Path | Min role | Notes |
| --- | --- | --- | --- |
| GET | `/api/chat/{job_id}` | viewer | `limit` (1–500), `offset` |
| POST | `/api/chat/{job_id}` | reviewer | `message` plus optional AI overrides; returns `202`, answer arrives over SSE |
| POST | `/api/chat/{job_id}/init` | reviewer | Explicit `ai_provider`, `ai_model`, `force_server_credentials` |
| POST | `/api/chat/{job_id}/abort` | reviewer | Cancel the in-flight answer |
| POST | `/api/chat/{job_id}/close` | reviewer | Close the AI session |
| DELETE | `/api/chat/{job_id}` | reviewer | Clear chat history |
| GET | `/api/chat/{job_id}/stream` | viewer | SSE stream of new messages |

### Server-Sent Events streams

| Path | Min role | Pushes |
| --- | --- | --- |
| `/api/stream` | viewer | Multiplexed stream; subscribe with `?topics=navbar,dashboard,results:JOB_ID,comments:JOB_ID,chat:JOB_ID` |
| `/api/navbar/stream` | viewer | Active analysis count and unread mentions |
| `/api/dashboard/stream` | viewer | Job list changes |
| `/api/results/{job_id}/stream` | viewer | Per-job status changes |
| `/api/results/{job_id}/comments/stream` | viewer | Per-job comment changes |
| `/api/chat/{job_id}/stream` | viewer | Per-job chat messages |
| `/api/admin/token-usage/stream` | admin | Token usage changes. Enforced by middleware before the handler runs |
| `/api/admin/logs/stream` | admin | Live server log tail; `lines` and `level` query parameters |
| `/api/admin/audit/stream` | admin | New audit events as they are recorded, one `audit:event` SSE event per row with the same JSON shape as the list endpoint |
| `/api/admin/settings/stream` | admin | Server setting changes |

Use `/api/stream` when you want one connection instead of six. It accepts up to 50 comma-separated topics; `token-usage` and `settings` are delivered only to admins.

### Reports and analytics

| Method | Path | Min role | Query parameters |
| --- | --- | --- | --- |
| GET | `/api/reports/totals` | admin or `can_view_reports` | `team`, `tier`, `version`, `from`, `to`, `status`, `tags`, `exclude_tags`, `review_status`, `limit`, `offset` |
| GET | `/api/reports/classification-overrides` | admin or `can_view_reports` | same filter set |
| GET | `/api/reports/issues-created` | admin or `can_view_reports` | same filter set |

Date bounds use the `from` and `to` query names. Without the flag you get `403 Reports access required`.

### Metadata assignment

| Method | Path | Min role |
| --- | --- | --- |
| GET | `/api/jobs/metadata` | viewer |
| GET | `/api/jobs/{job_name}/metadata` | viewer |
| PUT | `/api/jobs/{job_name}/metadata` | admin |
| DELETE | `/api/jobs/{job_name}/metadata` | admin |
| PUT | `/api/jobs/metadata/bulk` | admin |
| GET | `/api/jobs/metadata/rules` | viewer |
| POST | `/api/jobs/metadata/rules/preview` | viewer |

### Notifications and AI discovery

| Method | Path | Min role | Notes |
| --- | --- | --- | --- |
| GET | `/api/notifications/vapid-public-key` | viewer | Web Push public key |
| POST | `/api/notifications/subscribe` | viewer | `endpoint`, `p256dh_key`, `auth_key` |
| POST | `/api/notifications/unsubscribe` | viewer | Removes a subscription |
| GET | `/api/ai-models` | viewer | `provider`, `force_server_credentials`; lists models for `claude`, `gemini`, `cursor` |
| GET | `/api/capabilities` | viewer | Feature toggles and whether server credentials exist |
| GET | `/api/default-server-settings` | viewer | Non-sensitive settings and defaults only |
| POST | `/api/feedback/preview` | viewer | AI-drafted feedback issue |
| POST | `/api/feedback/create` | viewer | Creates the GitHub issue; returns `201` |

### Admin

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/admin/users` | List users |
| GET | `/api/admin/users/pending` | Accounts awaiting approval |
| POST | `/api/admin/users/create` | `username`, `role`, `can_view_reports`, `can_use_server_providers` |
| DELETE | `/api/admin/users/{username}` | Delete a user |
| PUT | `/api/admin/users/{username}/role` | Change role |
| POST | `/api/admin/users/{username}/approve` | Approve a pending registration |
| POST | `/api/admin/users/{username}/reject` | Reject a pending registration |
| POST | `/api/admin/users/{username}/rotate-key` | Re-issue a user's API key |
| PUT | `/api/admin/users/{username}/can-view-reports` | Grant or revoke reports access |
| PUT | `/api/admin/users/{username}/can-use-server-providers` | Grant or revoke server AI credentials |
| GET | `/api/admin/free-models` | List admin-declared free provider+model pairs (with `version` fingerprint) |
| PUT | `/api/admin/free-models` | Replace the free pair list (validated against the catalog); optional `expected_version` from a prior GET rejects concurrent edits with `409` |
| GET | `/api/admin/server-providers` | Per provider: key presence, verification state, `key_scoped` flag, `has_deployment_credential` (a deployment credential is resolvable for it, independently of any captured listing), `key_scoped_source`, `key_scoped_status` (last capture outcome: `supported`/`unsupported`/`unavailable`, null when no capture ran), and its models with `included` + `free` flags — never the keys. One entry per catalog provider, plus stored providers that left the catalog and every provider the sidecar registers as accepting session API keys (such a registry-only provider reports `models: []`, `supports_key: true`, `can_refresh_models: false`, and no selection rows) — a provider the sidecar reports as not key-capable is never added. For a key-scoped provider the model list is exactly what the stored key can access |
| POST | `/api/admin/server-providers` | Add a server provider key: `provider`, `api_key`, `verification_model` — verified with a real sidecar turn before storing, then the key-scoped model listing is captured; `409` when a key is already stored |
| POST | `/api/admin/server-providers/{provider}/store-deployment-key` | Promote the deployment credential RootCoz already resolves for the provider into the encrypted store: body `{ "verification_model" }` only, nothing pasted; verified with a real sidecar turn first, then the same store/capture/seed path as add (`ok`, `outcome`, `model_count`); `404` when no deployment credential resolves, `409` when a key is already stored |
| POST | `/api/admin/server-providers/{provider}/rotate-key` | Replace the key (re-verified first); `404` when none is stored; the key-scoped listing is re-captured |
| POST | `/api/admin/server-providers/{provider}/refresh-models` | Re-run key-scoped discovery with the stored key (no re-entry, no LLM call); reports `status` (`supported`/`unsupported`/`unavailable`) and `model_count`; `404` when no key is stored |
| PUT | `/api/admin/server-providers/{provider}/models` | Replace the provider's model selection `{id, included, free}[]` (validated against the catalog); `free` implies `included`, and the free flags write the same `free_ai_models` pairs |
| DELETE | `/api/admin/server-providers/{provider}` | Remove the key, selection, and free pairs; `200` with a warning payload listing the affected models |
| GET | `/api/admin/settings` | All settings with value, source, and metadata; `reveal_key` unmasks one key or `__all__` |
| PUT | `/api/admin/settings` | `{"settings": {"KEY": "value"}}` |
| GET | `/api/admin/settings/history` | `key`, `limit` change history |
| DELETE | `/api/admin/settings/{key}` | Reset one setting to its default |
| GET | `/api/admin/token-usage` | `start_date`, `end_date`, `ai_provider`, `ai_model`, `call_type`, `group_by` |
| GET | `/api/admin/token-usage/summary` | Dashboard totals |
| GET | `/api/admin/token-usage/{job_id}` | Per-job usage |
| GET | `/api/admin/transcripts` | List session transcripts with joined context (`job_id` filter) |
| GET | `/api/admin/transcripts/{session_id}` | Download one transcript JSONL (streaming, no truncation) |
| GET | `/api/admin/audit` | Recorded audit events, newest first: `actor` (exact, case-insensitive), `action` (repeat the parameter or comma-separate), `target_type`, `target_id`, `outcome` (`success`/`failure`/`denied`), `q` (substring over `details`), `since`/`until` (ISO 8601), `limit` (50, max 500), `offset`. Returns `{events, total, limit, offset}` |
| GET | `/api/admin/audit/actions` | Distinct recorded actions as `{actions: [{action, count, last_seen, label}]}` |
| GET | `/api/admin/audit/verify` | Recompute the audit hash chain: `{checked, legacy, ok, first_broken_id}`; `limit` verifies only the newest N events |
| GET | `/api/admin/audit/export` | Stream every event matching the list filters: `format` (`json`/`csv`) plus the same filters as `/api/admin/audit`. Uncapped — the way to retain evidence before retention deletes it |
| POST | `/api/admin/ai-models/refresh` | Re-discover models from the sidecar |
| GET | `/api/admin/component-versions` | Backend, sidecar, and chart versions |
| GET | `/api/admin/db/schema` | Read-only schema dump |
| POST | `/api/admin/db/query` | Read-only SQL for the admin chat |
| GET | `/api/admin/chat` | Admin chat history |
| POST | `/api/admin/chat` | Send an admin chat message; returns `202` |
| POST | `/api/admin/chat/init`, `/close`, `/abort` | Admin chat session control |
| DELETE | `/api/admin/chat` | Clear admin chat history |
| GET | `/api/admin/chat/stream` | Admin chat SSE |
| POST | `/api/admin-chat/artifacts` | Save a generated HTML report artifact |
| GET | `/api/admin-chat/artifacts/{artifact_id}` | Download that artifact |

See [Configuration Reference](configuration-reference.html) for the full environment variable surface behind these admin settings.

#### Audit events

`GET /api/admin/audit` returns `{events, total, limit, offset}` newest-first. Each event carries `id`, `created_at` (UTC ISO-8601 with a trailing `Z`), `actor`, `actor_role`, `is_admin`, `action`, `target_type`, `target_id`, `outcome`, `details` (the stored JSON object, already parsed), `request_ip`, and the chain columns `prev_hash` and `entry_hash`.

```bash
curl -s "$BASE/api/admin/audit?actor=alice&action=admin.job_delete&since=2026-04-01T00:00:00Z&limit=20" \
  -H "Authorization: Bearer $KEY"
```

Filters combine with AND. `actor` must match the stored value exactly but ignores case. `action` accepts one id or several, either as a repeated parameter (`?action=a&action=b`) or comma-separated (`?action=a,b`). `q` is a substring match over the event's `details` object. `since` and `until` are inclusive and accept any ISO 8601 timestamp; a value that cannot be parsed is rejected with `400`.

Time bounds are timezone-correct. A bare `YYYY-MM-DD` means a UTC day, so `since=2026-06-15` starts at `00:00:00Z` and `until=2026-06-15` covers through `23:59:59.999999Z`. An explicit offset (`2026-06-15T22:00:00+02:00`) is honoured and converted to UTC; a timestamp with no offset is read as UTC. Every stored `created_at` carries an explicit `Z`, so a filtered result means the same instant no matter which timezone you built the query in.

`GET /api/admin/audit/actions` lists the action ids seen so far with their event count, last-seen timestamp, and a display label (`auth.login_failed` becomes `Auth Login Failed`) for building filter menus.

`GET /api/admin/audit/verify` recomputes the chain: each row's `entry_hash` is `sha256(canonical_json(row) + prev_hash)` and its `prev_hash` is the previous row's `entry_hash`. It returns `{checked, legacy, ok, first_broken_id, truncated, truncated_before_id}`. `checked` is how many chained rows were verified and `legacy` how many rows predate the chain — those are counted separately, never reported as broken. Legacy means a NULL hash on a row **older than the first chained row**; a NULL hash above that boundary is a break, so clearing a row's hash cannot launder an edited row. `ok` is false when a row was edited or deleted, and `first_broken_id` names the oldest offending row. Retention pruning removes the head of the chain by design, so the oldest surviving row points at a deleted hash: that boundary is reported as `truncated: true` with `truncated_before_id`, and the walk continues from the recorded `prev_hash` instead of failing. Truncation is only accepted when it matches the stored prune checkpoint — the id and hash of the last row the retention job (or the row cap) deleted. A prefix removed any other way does not match the checkpoint and is reported as a break (`ok: false`, `first_broken_id` = the oldest surviving row), so deleting old rows by hand is still tampering. A mismatch inside the surviving window always sets `ok: false`.

`GET /api/admin/audit/export` streams every event the same filters match — `format=json` (a JSON array) or `format=csv`, with columns `id, created_at, actor, actor_role, is_admin, action, target_type, target_id, outcome, request_ip, details, hash_version, prev_hash, entry_hash, hash_payload`. Both hash columns are included so an export verifies on its own after the server has pruned the originals. Each JSON event also carries `details_raw` (the exact stored `details` text) and `hash_payload` — the exact canonical JSON that was hashed. Verification is therefore one line on either format: `sha256(hash_payload + prev_hash) == entry_hash`. `hash_version` says which field list the payload covers: version `1` is the original field set (every row written before the rescue counter existed), version `2` adds `throttle_rescue`, so the rescue budget itself is tamper-evident. A trail can mix both versions and verifies row by row; nothing is rewritten when the field list grows. Re-mapping fields yourself will not work, because the API renders `is_admin` as a boolean while the stored row hashed `1`; `hash_payload` carries the stored form. A row written before the chain existed, or one whose stored bytes would expose a credential that read-time redaction removes, ships without `hash_payload` — treat a missing payload as "not verifiable", never as verified. Responses are streamed in batches, and the batches page by row id rather than by offset, so events written mid-export can only join the tail — a row is never repeated and never skipped. This is the supported way to retain audit evidence before retention pruning or the row cap deletes it. CSV text cells that start with `=`, `+`, `-`, `@`, a tab or a carriage return are prefixed with `'` so a spreadsheet cannot execute them as formulas; a cell that already starts with `'` is prefixed too (`'abc` exports as `''abc`), which makes the escaping reversible. To recompute a hash from an exported CSV, remove exactly one leading apostrophe from each text cell — the unescaped value is byte-identical to the stored column. Numbers and ids are never escaped.

All audit read endpoints require `admin` and return `403 Admin access required` otherwise. `GET /api/admin/audit/stream` pushes each new event as it is written, so an audit page can stay live without polling. Events pass through the same redaction as the list endpoint. A subscriber that cannot keep up is never silently starved: when its queue overflows the stream emits an `audit-gap` event with `{"dropped": N}` before the next normal event, so a client can tell the administrator that events were missed instead of showing a broken view as live.


## Advanced Usage

Prefer the CLI when you can. Most endpoints above have a `rootcoz` command counterpart. SSE streams have none, because the CLI is a one-shot tool, and the user-token endpoints (`GET`/`PUT /api/user/tokens`) have client methods but no registered CLI command either. See [CLI Command Reference](cli-reference.html) and [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html).

A three-call integration that skips the UI entirely:

```bash
KEY="$ROOTCOZ_API_KEY"
BASE=http://localhost:8000

JOB=$(curl -s -X POST "$BASE/analyze" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"type":"file","raw_xml":"<testsuite>...</testsuite>","labels":["nightly"]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -s -H "Authorization: Bearer $KEY" "$BASE/results/$JOB" | jq '.status'

curl -s -H "Authorization: Bearer $KEY" \
  "$BASE/results/$JOB?fields=status,result.summary"
```

For long-lived integrations, open `/api/stream?topics=navbar,results:$JOB` once instead of polling the result endpoint.

## Troubleshooting
- `403 Authentication required` or a `401` on a supposedly public path.  
  The path is not in the public allow list. Use `POST /api/auth/login` for a session cookie or send `Authorization: Bearer <API_KEY>`.

- `403 Admin access required` on `/api/admin/*`.  
  Your account is not `admin`. The bootstrap `admin` superuser is created from `ADMIN_KEY`; see [Managing Users and Server Settings](manage-users-and-server-settings.html).

- `403 Reports access required` from `/api/reports/*`.  
  You need the `can_view_reports` flag, which is independent of role. Admins always have it.

- `400` from `GET /results/{job_id}?fields=...`.  
  One path is not on the allowlist. Fetch `GET /api/results/fields` and use an exact path.

- `404` on `/api/results/{job_id}/tests` for tests you expect.  
  A `404` here means the job ID does not exist — the endpoint checks that before applying any filter, so no amount of `child_job_name` or `child_build_number` will help. An existing job that simply has no matching entries returns `200` with an empty `entries` list; to narrow or widen that, confirm the status filter matches `passed`, `skipped`, or `failed`, and add `child_job_name` and `child_build_number` when the tests live under a child job.

- `403` on `POST /analyze` right after registering.  
  Your default role is `viewer` or the account is still pending approval. Check `GET /api/auth/me`, then ask an admin to raise your role or approve the account.

## Related Pages
- [Configuration Reference](configuration-reference.html)
- [CLI Command Reference](cli-reference.html)
- [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html)
- [Submitting Analyses](submit-analyses.html)
- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Using Server Chat for Cross-Job Analysis](use-server-chat-for-cross-job-analysis.html)
- [Exploring History and Reports](explore-history-and-reports.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)