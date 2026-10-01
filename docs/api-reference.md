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

That single authenticated GET returns the stored analysis for one job, including failures, classifications, patterns, comments, and child jobs.

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

   `can_view_reports` is a separate flag from role. Admins always have it; anyone else needs an admin to grant it. Calls that create or modify data also run against the `ALLOWED_USERS` allow list, which is empty (open) by default and always bypassed for admins.

   > **Warning:** A role you do not hold returns `403`, not `404`. `403 Admin access required`, `403 Reviewer access required`, and `403 Operator access required` name the exact gate you hit.

### Public and unauthenticated routes

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness, returns immediately |
| GET | `/api/health` | Detailed health including database and AI sidecar |
| GET | `/metrics` | Prometheus metrics |
| GET | `/api/releases/latest` | Proxies GitHub release metadata (version, changelog) |
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
| POST | `/api/auth/rotate-key` | viewer | Rotates your own API key; the old key stops working |
| GET | `/api/user/ai-credentials` | viewer | Personal provider credentials, secrets masked |
| PUT | `/api/user/ai-credentials/{provider}` | reviewer | Store a personal key. `{provider}` is an exact provider ID from `GET /api/user/ai-credentials` (for example `anthropic` or `google`), not the `claude`/`gemini`/`cursor` names used for analysis settings. Unknown IDs are rejected with 400 |
| DELETE | `/api/user/ai-credentials/{provider}` | reviewer | Remove a personal provider key |
| GET | `/api/user/tokens` | viewer | Stored Jira and GitHub tracker tokens |
| PUT | `/api/user/tokens` | viewer | Save Jira and GitHub tracker tokens |
| POST | `/api/validate-token` | viewer | Validate a GitHub or Jira token before saving |
| POST | `/api/jira-projects` | viewer | List Jira projects visible to the supplied credentials |
| POST | `/api/jira-security-levels` | viewer | List Jira security levels for a project |

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
| `jira_url`, `jira_email`, `jira_api_token`, `jira_pat`, `jira_project_key`, `jira_ssl_verify`, `jira_max_results`, `enable_jira` | Jira search during analysis |
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
| GET | `/api/failures/{failure_uuid}` | viewer | none |
| GET | `/results/{job_id}/review-status` | viewer | none |
| GET | `/api/dashboard` | viewer | `limit`, `offset` |
| GET | `/api/dashboard/filtered` | viewer | `search`, `status`, `date_from`, `date_to`, `review_status`, `analysis_state`, `limit`, `offset` |
| GET | `/api/dashboard/active-count` | viewer | none |

`fields` returns full values for allowlisted paths only and is never truncated. Unknown paths return `400`, so fetch `/api/results/fields` first when you build against it.

### Review, classification, and job maintenance

| Method | Path | Min role | Body |
| --- | --- | --- | --- |
| PUT | `/results/{job_id}/reviewed` | reviewer | `test_name`, `reviewed` |
| PUT | `/results/{job_id}/override-classification` | reviewer | `test_name`, `classification` |
| PUT | `/results/{job_id}/override-pattern` | reviewer | `test_name`, `pattern` |
| PUT | `/results/{job_id}/tags` | reviewer | name and tag fields |
| DELETE | `/results/{job_id}` | operator | none; deletes your own job, admins delete any |
| DELETE | `/api/results/bulk` | operator | `job_ids` |

Classification values are `CODE ISSUE`, `PRODUCT BUG`, and `INFRASTRUCTURE`. Pattern values are `NEW`, `REGRESSION`, `FLAKY`, `INTERMITTENT`, `KNOWN_BUG`, and `PERSISTENT`. These are the exact API values; the UI renders `KNOWN_BUG` as `Known Bug` and spaces the words for readability. Review state is tracked per test; classification and pattern changes apply across the whole same-error group.

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
| POST | `/results/{job_id}/preview-github-issue` | viewer | `test_name`, `include_links`, AI overrides; returns preview content |
| POST | `/results/{job_id}/preview-jira-bug` | viewer | Same shape, Jira flavour |
| POST | `/results/{job_id}/create-github-issue` | viewer | `test_name`, `title`, `body`; returns `201` |
| POST | `/results/{job_id}/create-jira-bug` | viewer | Adds `jira_issue_type`; returns `201` |
| PUT | `/results/{job_id}/tracked-in` | reviewer | `test_name`, `url`, `type` |
| GET | `/results/{job_id}/tracked-in` | reviewer | Current tracker links |
| DELETE | `/results/{job_id}/tracked-in/{link_id}` | reviewer | Remove one link |
| GET | `/api/exporters` | viewer | Available exporter plugins and enabled state |
| POST | `/results/{job_id}/push/{plugin_name}` | operator | Generic push; `child_job_name` and `child_build_number` optional |
| POST | `/results/{job_id}/push-reportportal` | operator | Backward-compatible Report Portal push |

> **Note:** The issue preview and create endpoints are gated by the `ALLOWED_USERS` allow list rather than by a role check, so a `viewer` account can call them when the server runs with an empty allow list. They still require valid tracker credentials and the matching `ENABLE_GITHUB_ISSUES` or `ENABLE_JIRA_ISSUES` toggle.

### History and classification lookup

| Method | Path | Min role | Query parameters |
| --- | --- | --- | --- |
| GET | `/history/failures` | viewer | `search`, `job_name`, `classification`, `date_from`, `date_to`, `limit`, `offset` |
| GET | `/history/test/{test_name}` | viewer | `limit`, `job_name`, `exclude_job_id` |
| GET | `/history/search` | viewer | `signature`, `exclude_job_id` |
| GET | `/history/stats/{job_name}` | viewer | `exclude_job_id` |
| GET | `/history/classifications` | viewer | `test_name`, `classification`, `job_name`, `parent_job_name`, `job_id` |
| POST | `/history/classify` | reviewer | `test_name`, `classification`, `reason`, `job_id`, `source`; returns `201` |

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
| `/api/admin/token-usage/stream` | admin | Token usage changes |
| `/api/admin/logs/stream` | admin | Live server log tail; `lines` and `level` query parameters |
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
| GET | `/api/admin/settings` | All settings with value, source, and metadata; `reveal_key` unmasks one key or `__all__` |
| PUT | `/api/admin/settings` | `{"settings": {"KEY": "value"}}` |
| GET | `/api/admin/settings/history` | `key`, `limit` change history |
| DELETE | `/api/admin/settings/{key}` | Reset one setting to its default |
| GET | `/api/admin/token-usage` | `start_date`, `end_date`, `ai_provider`, `ai_model`, `call_type`, `group_by` |
| GET | `/api/admin/token-usage/summary` | Dashboard totals |
| GET | `/api/admin/token-usage/{job_id}` | Per-job usage |
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
  Failed tests may live under a child job. Add `child_job_name` and `child_build_number`, and confirm the status filter matches `passed`, `skipped`, or `failed`.

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