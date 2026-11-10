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

   `can_view_reports` is a separate flag from role. Admins always have it; anyone else needs an admin to grant it. Most calls that create or modify data also run against the `ALLOWED_USERS` write allow-list — a deployment-level bound for SSO/proxy setups where a wide population authenticates but only a known team should write. It is empty by default (no username restriction; roles decide), never grants a permission, and is always bypassed for admins. The check is per-handler, not global, so it is enforced on data-modifying routes rather than as a middleware boundary; `tests/test_allow_list_coverage.py` enumerates the mutating routes and fails if one that can write data is added without the check. The exemptions are listed exactly in that test: the authentication routes, the read-only Jira and token-validation lookups, the metadata rules preview, and `DELETE /api/user/ai-credentials/{provider}` — revoking your own key stays possible even if you are off the list, otherwise being removed would leave you holding a credential you cannot remove.

   > **Warning:** A role you do not hold returns `403`, not `404`. `403 Admin access required`, `403 Reviewer access required`, and `403 Operator access required` name the exact gate you hit.

### Public and unauthenticated routes

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness, returns immediately |
| GET | `/api/health` | Detailed health including database and AI sidecar; returns 503 when the database or the sidecar (probed via its effective port) is unreachable. `checks.audit` reports `{status, write_failures}`: `degraded` once an audit row has failed to persist since process start |
| GET | `/metrics` | Prometheus metrics (text exposition format): `rootcoz_requests_total`, `rootcoz_errors_total`, `rootcoz_error_rate`, `rootcoz_errors_by_class`, `rootcoz_health_up`, `rootcoz_active_analyses`, `rootcoz_audit_write_failures`, `rootcoz_oldest_analysis_age_seconds` (the age of the longest-running analysis, from its analysis start — **omitted** when nothing is running or when the age could not be read, never `0`), and the unlabelled `rootcoz_data_used_bytes` / `rootcoz_data_capacity_bytes` / `rootcoz_data_used_ratio` for the filesystem that holds the data directory. `rootcoz_errors_by_class` carries a sample only for a class seen in the window, so an absent `status_class` sample means zero errors of that class; the chart health alert reads it with `rootcoz_requests_total` and cross-checks it against `rootcoz_errors_total`. The probe is storage-agnostic: the same gauges work for an OpenShift PVC, a Docker volume, a bind mount or a plain directory, because they measure whatever filesystem backs the data dir — on a shared node filesystem the number can include other tenants' usage, so read it as free space on that filesystem, not as a per-volume quota. The data gauges are **omitted when the filesystem probe fails**, never reported as `0`. The endpoint is unauthenticated, so the absolute used and capacity byte counts of the data filesystem, and the longest running analysis' age, are visible to anyone who can reach it — numbers only, no content, no path and no job names |
| GET | `/api/releases/latest` | Proxies GitHub release metadata (version, changelog) plus `server_version` and `update_available` so clients can tell an update notice from a What's New popup |
| GET | `/openapi.json`, `/docs`, `/redoc` | Schema and interactive API browsers |
| POST | `/api/auth/register` | Create a user, returns the raw API key once |
| POST | `/api/auth/login` | Exchange username + API key for a session cookie |
| GET | `/api/auth/needs-key` | Browser-only identity probe |
| GET | `/api/auth/pending-status` | Poll while an account awaits approval |

### Authentication and your account

| Method | Path | Min role | Notes |
| --- | --- | --- | --- |
| GET | `/api/version` | viewer | Running version, plus `build` when the image was built with its identity args: `{commit, ref, built_at, label?}`. `commit` is the full sha, `ref` the tag the image was published under, `built_at` the build timestamp, and `label` the display suffix the sidebar and `rootcoz version` print (`latest@d6a1f3c`) — absent when the ref *is* the version (a release build prints the bare version). The whole `build` key is omitted when the builder supplied nothing, so `"unknown"` is distinguishable from a real ref, and `version` is a plain string as it always was |
| POST | `/api/auth/logout` | authenticated | Clears the session cookie |
| GET | `/api/auth/me` | viewer | Username, role, `can_view_reports`, `can_use_server_providers` |
| POST | `/api/auth/rotate-key` | viewer | Rotates your own API key; the old key stops working. Known gap — see the note below |
| GET | `/api/user/ai-credentials` | viewer | Supported provider IDs with a `configured` flag and a `supports_user_keys` flag each (a stored key for a provider that stopped accepting user keys — e.g. an ambient-auth provider — stays listed with `supports_user_keys: false`). Keys are never returned |
| PUT | `/api/user/ai-credentials/{provider}` | reviewer | Store a personal key. Body is `{api_key, model}` — `model` is required (`400` without it, since the key is verified against it) and the key is only persisted when the provider returns `accepted`. A `rejected` or `inconclusive` check is not an HTTP error: it returns `200` with `ok: false`, an `outcome`, and an explanatory `message`, leaving any existing credential unchanged, so clients must check `ok` rather than the status code. `{provider}` is an exact provider ID from `GET /api/user/ai-credentials` (for example `anthropic` or `google`), not the `claude`/`gemini`/`cursor` names used for analysis settings. Unknown IDs are rejected with 400 |
| DELETE | `/api/user/ai-credentials/{provider}` | reviewer | Remove a personal provider key |
| GET | `/api/user/tokens` | viewer | Stored Jira and GitHub tracker tokens |
| PUT | `/api/user/tokens` | reviewer | Save Jira and GitHub tracker tokens. Each field is independent: a string sets it, `""` or `null` clears just that field, and an omitted field keeps its stored value, so clearing one credential leaves the others intact. Fields are `github_token`, `jira_email`, `jira_token`, and `jira_sa_token` (an Atlassian service-account credential — an ATOA-prefixed OAuth 2.0 client secret or an ATST-prefixed scoped token — which takes precedence over the regular Jira credentials). A body with none of the fields writes nothing and returns `saved: false`; anything other than a string or `null` is rejected with `400` |
| POST | `/api/user/tokens/test/{integration}` | reviewer | Test the tracker credential **saved in your own account** (`{integration}` is `github` or `jira`; anything else is a `400`). Reads the credential from storage only — the body is ignored, so it tests what is saved, and the username comes from the session, so a caller can only ever test their own row. Same contract as `POST /api/admin/settings/test/{integration}`: **always HTTP 200** with `{ok, message, details?}` (a failed connection is a result, not an error), the same deadline, and `details` carries `login` for GitHub or `auth_mode` + `user` for Jira. No token value appears in the response or the logs. A caller with no saved credential — including the bootstrap `admin`, which has no profile to store one — gets `ok: false` with the same actionable message the tracker endpoints use |
| POST | `/api/validate-token` | viewer | Validate a GitHub or Jira token before saving. Jira tokens are validated against the selected method's server URL (`jira_cloud_url` with an email, `jira_server_url` without); an ATOA-prefixed value (an Atlassian OAuth 2.0 credential client secret) is validated as a service account against the scoped Atlassian gateway, so its cloud ID must be resolvable server-side and the server must have `atlassian_oauth_client_id` configured; an ATST-prefixed value (an Atlassian service-account scoped token) is sent as Bearer to the same gateway with no exchange |
| POST | `/api/jira-projects` | viewer | List Jira projects visible to the caller's stored Jira credentials (`PUT /api/user/tokens`). Body carries `query` only |
| POST | `/api/jira-security-levels` | viewer | List Jira security levels for a project, with the caller's stored Jira credentials. Body carries `project_key` only |
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
| POST | `/results/{job_id}/analyze` | operator | Runs the AI pipeline on a submitted job; optional `ReAnalyzeRequest`. The claim starts a **fresh analysis window**: `analysis_started_at` and `completed_at` are reset when the job is claimed, so `GET /results/{job_id}` returns those two allowlisted fields with the new run's values rather than the earlier ingest run's |
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
| `ai_provider`, `ai_model` | `claude`, `gemini`, or `cursor` |
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
| GET | `/api/results/incomplete` | viewer | `limit` (default 100, min 1, capped at 500) |
| GET | `/api/results/{job_id}/tests` | viewer | `status` (repeatable: `passed`, `skipped`, `failed`), `child_job_name`, `child_build_number`, `offset`, `limit` (1–200, default 50) |
| GET | `/api/results/{job_id}/full` | viewer | `status`, `child_job_name`, `child_build_number`, `offset`, `limit` — apply to the `tests` section (same contract as `/tests`) |
| GET | `/api/results/full` | viewer | `job_ids` (repeatable, 1–50 ids of at most 64 chars each) — the batch summary |
| GET | `/api/failures/{failure_uuid}` | viewer | none |
| GET | `/results/{job_id}/review-status` | viewer | none |
| GET | `/api/dashboard` | viewer | `limit`, `offset` |
| GET | `/api/dashboard/filtered` | viewer | `search`, `status`, `date_from`, `date_to`, `review_status`, `analysis_state`, `limit`, `offset`, plus metadata filters `team`, `tier`, `version`, `label`, `exclude_label` (each repeatable). `submitted_by` (repeatable) is also accepted: each value is trimmed and matched as a **case-insensitive substring** on the original submitter (`yuva` matches `yuval` and `yuvalb`), values OR together, and blanks are dropped. At most 50 values of 64 chars each (`400` above either limit); no values means no filter. Matching is case-insensitive for ASCII only — SQLite's `lower()` does not fold non-ASCII names, which then match case-sensitively |
| GET | `/api/dashboard/submitters` | viewer | none; returns `{"submitters": ["alice", ...]}` — the distinct names shown on the dashboard, sorted, deduped case-insensitively on the same ASCII rule, blank and over-64-char values dropped. Viewers already receive `submitted_by` on every dashboard row, so this endpoint discloses nothing new |
| GET | `/api/dashboard/active-count` | viewer | none |

`fields` returns full values for allowlisted paths only and is never truncated. Unknown paths return `400`, so fetch `/api/results/fields` first when you build against it.

`GET /results/{job_id}` always sets `result.incomplete_count`: the number of failures whose effective classification is empty — no `analysis.classification`, or an analysis stored as plain text by an older run. `analysis_error` only explains why there is no analysis; it is not what makes a row incomplete. It counts failures across `failures`, `child_job_analyses`, and nested `failed_children`, and it is computed from the effective classifications, so an override that sets one removes that failure from the count. It is an allowlisted `fields` path, so `?fields=result.incomplete_count` returns just it; dashboard rows carry the same key only when it is above zero.

`GET /results/{job_id}` sets `result.enriched_xml` when the job had one: the source JUnit document re-serialised with each failure's analysis and the result URL injected. It is only ever built from a source that carries a `raw_xml`, so a `jenkins`, `prow` or `raw` job never has one; a `type=file` job with nothing to enrich stores the submitted string verbatim instead. A document above the **1,048,576-character** inline limit is not stored: `result.enriched_xml` is `null` **and** `result.enriched_xml_omitted` is `{"characters": <the document's length>, "limit": 1048576}`. `null` **without** that marker means no enriched document was produced for this job — no list of reasons is given, because the absence has several unrelated causes. Nothing else in the payload changes: `summary`, `failures` and the classifications are complete either way, and each dropped document writes exactly one `result.enriched_xml_omitted` audit row (`GET /api/admin/audit?action=result.enriched_xml_omitted`, actor `rootcoz`, details `characters` and `limit`, never the document). The enriched form is what is not retrievable; the document it was built from still is — `result.request_params.raw_xml` for a `type=file` job (`raw_xml` is not a sensitive key, and `?fields=result.request_params` returns it). Neither key is individually selectable with `fields` (`?fields=result.enriched_xml_omitted` is a `400`), but `?fields=result` returns the whole unprojected result, marker included.

`GET /results/{job_id}` sets `result.tracker_search` when a tracker lookup ran during analysis: one record per tracker, `result.tracker_search.jira` and `result.tracker_search.github`, each with `searched`, `reason`, `scope`, `keyword_sets`, `candidates` and `matches`. `searched: true` with `candidates: 0` means the lookup ran and found nothing; `searched: false` always carries a non-empty `reason` (no credential, no repository or project in scope, the tracker disabled, or every search failing). A missing `tracker_search` key means **not recorded** — a result written before this field existed, a run with no analysis, or an analysis that failed before enrichment — never "nothing found". It is an allowlisted `fields` path, so `?fields=result.tracker_search` returns just it.

`GET /api/results/incomplete` lists the jobs holding such failures, newest first. It returns an object, never a bare array: `{"jobs": [...], "truncated": bool, "truncated_by": "limit" | "scan_ceiling" | null, "scanned": int}`. Each entry in `jobs` is `{job_id, job_name, build_number, build_id, incomplete_count, test_names}`: `incomplete_count` is `len(test_names)`, `test_names` names the failures still unclassified, and `job_name`, `build_number` and `build_id` fall back to `""`, `0` and `""` when the stored result does not carry them. Only an analysis that actually ran can be incomplete, so a job whose `analysis_state` is `submitted` is never listed — there is no entry and no count for a job that is still pending (`GET /results/{job_id}` reports `incomplete_count: 0` for it). `limit` is 1–500 (default 100; `0` is rejected with `422`) and counts matching jobs, not scanned rows, so a page is never padded with clean results. `scanned` is the number of rows examined, `truncated` is `true` when the scan stopped before examining every row, and `truncated_by` names the stop that did it: `limit` (raise `limit` to see more) or `scan_ceiling` (the fixed 5000-row bound on the most recent results, where a higher `limit` cannot help — on a very large instance this makes the list a recent window rather than the whole history). There is deliberately no total: counting every match is the full-table scan that bound exists to prevent. The endpoint is read-only and available to `viewer` and above.

`GET /api/results/{job_id}/full` returns the complete picture in one call: `job` metadata, `counts` (passed/skipped/failed), a flat `failures` list across child jobs with effective classifications and review state (`reviewed`, `reviewed_by`) joined server-side, a precomputed `summary` (`total_failures`, `reviewed_count`, `classifications`), a paginated `tests` page, `comments`, and `tracked_in` links. Jobs still running return `202` with the sections that exist so far.

`GET /api/results/full` reads up to 50 jobs in one call. `job_ids` is a **repeatable** query parameter (`?job_ids=a&job_ids=b`) — ids are never comma-joined on the wire, because an id is opaque and a comma-joined value would make an id containing a comma unaddressable. The response is an envelope: `jobs` holds one item per **found** id, `missing` names the ids that are not stored, and `forbidden` is the ids the caller may not read. `jobs` and `missing` each follow request order after dedupe, but pair by `item.job.job_id`, never by position: an id in `missing` has no item, so zipping the request against `jobs` misaligns as soon as one id is absent. Each item is exactly the `GET /api/results/{job_id}/full` payload **minus** `tests` (fetch a job's tests with `GET /api/results/{job_id}/tests`); the batch therefore takes no `status`, `child_job_name`, `child_build_number`, `offset`, `limit` or `fields` selector.

The call always answers `200` for a valid request: an unknown id is reported in `missing`, never as a `404` or a `5xx`, and a job still running appears in `jobs` with the sections that exist plus its own `job.status`/`job.analysis_state` saying it is unfinished. `forbidden` is always empty today, because no per-id read rule exists — any authenticated user may read any job, exactly as the single-job read allows — and it stays in the payload so clients can parse by key. The bounds are 1–50 ids **counted before dedupe** and 64 characters per id, the same numbers `submitted_by` uses (the constants are separate on purpose: a job id is not a submitter name). An empty `job_ids`, a blank value beside valid ones, more than 50 values (repeats count), or an over-long id is a `400` naming the count or the length — never a `422`, never a truncation, and never a silently dropped id.

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
| GET | `/results/{job_id}/enrich-comments` | viewer | GitHub PR and Jira ticket status, re-read from GitHub and Jira on a cache miss. Read, not audited: the report page calls it on every load and the result is cached per job for 60 s, so a repeat read can return a minute-old status. Only refused and failed attempts are recorded. Returns `{"enrichments": {comment_id: [badges]}, "failed": bool}`: `failed` is `true` when any lookup errored or answered nothing, so an empty `enrichments` mapping is never mistaken for "nothing to enrich". The field is additive — a client that ignores it behaves as before, and a failure is never cached |
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
| POST | `/results/{job_id}/preview-github-issue` | reviewer | `test_name`, `include_links`, AI overrides and the destination choices; returns preview content. The duplicate search uses the caller's stored token |
| POST | `/results/{job_id}/preview-jira-bug` | reviewer | Same shape, Jira flavour; the duplicate search uses the caller's stored Jira credential |
| POST | `/results/{job_id}/create-github-issue` | reviewer | `test_name`, `title`, `body` and the destination choices; the body carries **no credential** — the tracker token is always the caller's stored account token (`PUT /api/user/tokens`), and a body that still sends `github_token` has it ignored. Returns `201`. A missing stored token is a `400`, never the server's `GITHUB_TOKEN`. Same refusal classes as `POST /api/feedback/create` — `502` when GitHub is unreachable or failing, `403` when the token cannot see `TESTS_REPO_URL` (GitHub answers `404` for that), lacks the `repo` scope, or is invalid or expired, `429` when rate limited |
| POST | `/results/{job_id}/create-jira-bug` | reviewer | Adds `jira_issue_type`; returns `201`. Files with the caller's stored Jira credential (a regular token or a service-account token, `PUT /api/user/tokens`) only — the body carries **no credential**, so a body that still sends `jira_token` / `jira_email` / `jira_sa_token` has them ignored; `400` when the account holds neither, never a server Jira credential; a stored token identical to the deployment's own personal credential (an e-mail + API token pair, or a PAT) is a `400` refusal too — gateway modes (`ATOA` OAuth 2.0 credential, `ATST` scoped token) are not compared, because there the credential belongs to the site's service account; server settings supply only the project key and site URL. `401` for an invalid or expired Jira token, `403` when the token cannot access the Jira project (Jira answers `404` for that), `502` with a retryable `Jira is unavailable right now…` detail on an upstream `5xx`, `502 Jira API error: <status>` for anything else |
| POST | `/api/chat-issue/propose` | reviewer | `target` (`github`/`jira`), `title`, `body`, optional `repo`, `job_id` and the Jira overrides; files nothing and returns `201` |
| POST | `/api/chat-issue` | reviewer | `confirmation_id` and `confirmation_code` from the proposal plus the same `target`, `title`, `body` and destination (`repo`/`job_id`, `jira_project_key`, `jira_security_level`, `jira_issue_type`); returns `201` |
| PUT | `/results/{job_id}/tracked-in` | reviewer | `test_name`, `url`, `type` |
| GET | `/results/{job_id}/tracked-in` | reviewer | Current tracker links |
| DELETE | `/results/{job_id}/tracked-in/{link_id}` | reviewer | Remove one link |
| GET | `/api/exporters` | viewer | Available exporter plugins and enabled state |
| POST | `/results/{job_id}/push/{plugin_name}` | operator | Generic push (e.g. `reportportal`, `datadog`); `child_job_name` and `child_build_number` optional |
| POST | `/results/{job_id}/push-reportportal` | operator | Backward-compatible Report Portal push |

> **Note:** All four issue preview and create endpoints call `_require_reviewer`, so a `viewer` receives `403` before the allow list is consulted. They additionally require valid tracker credentials and the matching `ENABLE_GITHUB_ISSUES` or `ENABLE_JIRA_ISSUES` toggle.

`POST /api/chat-issue` is the chat counterpart: it takes no `test_name` and no credentials. It is what the chat model calls through its `create_github_issue` / `create_jira_issue` tools when the user explicitly asks for an issue, and the title and body are the model's draft. **It always needs a `confirmation_id` and the matching `confirmation_code`** from `POST /api/chat-issue/propose`, which files nothing and returns the preview the user approves: the draft is stored per account, single-use and expires after ten minutes, and the create call must repeat the same title, body, tracker **and destination** (`repo` and `job_id` for GitHub, the project/security-level/issue-type overrides for Jira). A create whose destination differs is a `400` — an id approved for one repository, run or Jira project authorises nothing else — and the chat tools bind the run internally, so the model cannot name another one. The tracker credential is **always** the caller's saved account token (`GET /api/user/tokens` is the source of truth for which trackers are available, and the report endpoints — including their duplicate search — use the same source) — never the deployment's `GITHUB_TOKEN` or Jira credential, and with no exception for the bootstrap `admin`, which has no profile to store one. Status codes: `201` created; `400` for a missing or stale `confirmation_id`, a changed draft or destination, a `repo` that is not a bare `owner/name` slug, a missing account credential, a saved Jira token that is the deployment's own personal credential (gateway `ATOA`/`ATST` credentials are not compared — they belong to the site's service account), a missing `TESTS_REPO_URL` or Jira project key, or an unusable/legacy service-account secret; `403` when the allow list blocks the caller, the caller is a `viewer`, the tracker's `ENABLE_*` toggle is off, or GitHub's own answer is a token that is invalid or expired, lacks the `repo` scope, or cannot see the repository it files in — the run's, the one the caller named, or `TESTS_REPO_URL` (GitHub answers `404` for an invisible repository, and the detail names the repository that failed); `401` when Jira rejects the credential; `429` when GitHub rate limits; `502` when the tracker is unreachable or failing. Every attempt writes one `issue.create` audit row with `target_type="chat"` — the create call, a refused `POST /api/chat-issue/propose`, a call the auth layer refuses (`401`) before the handler runs, and a call these two endpoints reject for request-model validation (`422`), whose row names the `step` and the failing field names (`details.fields`) but never a value: `details.result` is `success`, `refused` (with a reason code, the `step` and, when the tracker answered, its `upstream_status`), or `failure` for an internal error; a proposal names the `job_id` and `repo` it asked for; a `success` names the issue it filed — `url`, plus `number` for GitHub or `key` for Jira; no row carries the issue text or a token. `title` and `body` pass `scrub_sensitive_data` before the draft is stored, so a pasted credential never reaches the tracker.

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
| GET | `/api/chat/{job_id}` | viewer | `limit` (1–500), `offset`. While a workspace is being prepared, `preparing` is `{phase, detail}` with `phase` one of `preparing`, `cloning`, `fetching_build_data`, `starting_session`; it is `null` when nothing is being prepared. Code-graph indexing is not a preparation phase: it runs in the background after the session starts |
| POST | `/api/chat/{job_id}` | reviewer | `message` plus optional AI overrides; returns `202`, answer arrives over SSE. Answers `425` naming the preparation phase when the workspace is being prepared |
| POST | `/api/chat/{job_id}/init` | reviewer | Explicit `ai_provider`, `ai_model`; returns `202` with `{"ready": false, "preparing": {phase, detail}}` — the workspace is prepared in the background and the session appears in `GET /api/chat/{job_id}`. Invalid preconditions still answer inline: `422` for an invalid pair or an unusable credential source, `400` for a deactivated server provider, `404` for a deleted job, `409` for an unusable workspace or a different provider/model already in session. A refused session no longer answers `422 AI session refused: …`: it appears as a failed message in the transcript. A preparation cancelled before it finished its checks answers `503` with a retry sentence, and `404` when the job is being deleted. Once init has answered `202`, a storage fault the server cannot retry past ends it with no session and the generic preparation-failure message `The chat workspace could not be prepared. Start it again.` in the transcript (if the server cannot record that message either, the text is absent and the server log names the cause); a momentary lock is retried and the chat still starts |
| POST | `/api/chat/{job_id}/abort` | reviewer | Cancel the in-flight answer |
| POST | `/api/chat/{job_id}/close` | reviewer | Close the AI session |
| DELETE | `/api/chat/{job_id}` | reviewer | Clear chat history; `425` naming the preparation phase while the workspace is being prepared |
| GET | `/api/chat/{job_id}/stream` | viewer | SSE stream of new messages **and the answer as it is written** (`chat-delta` frames) |

An answer can become a tracker issue: the chat model files it through `POST /api/chat-issue` (see [Trackers, issues, and exporters](#trackers-issues-and-exporters)) when the user asks it to, via its `create_github_issue` / `create_jira_issue` tools. Admin chat (`/api/admin/chat/*`) has no tracker credential of its own; the endpoint uses the caller's account tokens in both chats, so an issue is always filed under the person who asked.

### Server-Sent Events streams

| Path | Min role | Pushes |
| --- | --- | --- |
| `/api/stream` | viewer | Multiplexed stream; subscribe with `?topics=navbar,dashboard,results:JOB_ID,comments:JOB_ID,chat:JOB_ID` |
| `/api/navbar/stream` | viewer | Active analysis count and unread mentions |
| `/api/dashboard/stream` | viewer | Job list changes |
| `/api/results/{job_id}/stream` | viewer | Per-job status changes |
| `/api/results/{job_id}/comments/stream` | viewer | Per-job comment changes |
| `/api/chat/{job_id}/stream` | viewer | One turn's chat messages: `chat-delta` frames carrying the answer as it is written, alongside the unchanged `chat-changed` notice |
| `/api/admin/chat/stream` | admin | The same two frames for admin chat |
| `/api/admin/token-usage/stream` | admin | Token usage changes. Enforced by middleware before the handler runs |
| `/api/admin/logs/stream` | admin | Live server log tail; `lines` and `level` query parameters |
| `/api/admin/audit/stream` | admin | New audit events as they are recorded, one `audit:event` SSE event per row with the same JSON shape as the list endpoint |
| `/api/admin/settings/stream` | admin | Server setting changes |

Use `/api/stream` when you want one connection instead of six. It accepts up to 50 comma-separated topics; `token-usage` and `settings` are delivered only to admins.

The two chat streams carry the answer itself. `event: chat-delta` with `data: {"id": <assistant message id>, "delta": "<fragment>"}` is one fragment of the reply as the model writes it: provisional, display only, and **never stored** — the saved reply is the turn's final text, so a reload shows it whole. `event: chat-changed` keeps its meaning on the same connection and carries two payloads there: `data: refresh` (the unchanged completion notice) and `data: {"dropped": N}` (a subscriber that fell behind lost N payloads; both mean "refetch `GET /api/chat/{job_id}`", so a client that treats any `chat-changed` as a refetch needs no change). `chat-delta` is delivered **only** by `/api/chat/{job_id}/stream` and `/api/admin/chat/stream`: `/api/stream?topics=chat:JOB_ID` is a shared notification bus for up to 50 topics, fanned out to every subscriber and relayed to other tabs, not a per-turn payload channel. A fragment reaches only a stream that is open when it is emitted and is **never replayed**, so a stream that joins mid-turn (a second tab, a reconnect) receives only later fragments. Both artifacts come from `>= 4.20.1` of `@myk-org/pi-sidecar` and `pi-sidecar-client` — one floor, the pair this release pins. A deployment whose sidecar cannot serve the feed still gets the whole answer, just without fragments: streaming is best-effort by design.

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
| GET | `/api/ai-models` | viewer | `provider`; lists models for `claude`, `gemini`, `cursor`. Each model may carry `session_supported` (boolean) and, when it is `false`, `session_unsupported_reason`; both are **absent** for providers that are not key-scoped (`trustKeyScopedListing`) |
| GET | `/api/capabilities` | viewer | Feature toggles and whether server credentials exist |
| GET | `/api/default-server-settings` | viewer | Non-sensitive settings and defaults only |
| POST | `/api/feedback/preview` | viewer | AI-drafted feedback issue |
| POST | `/api/feedback/create` | viewer | Creates the GitHub issue; returns `201`. `503` when feedback is disabled, `400` when the caller has no GitHub token of their own stored (the server's `GITHUB_TOKEN` is never used, not even for the bootstrap `admin`), `429` when GitHub rate-limited, `403` when the token is invalid, lacks the `repo` scope, or cannot see the repository, `502` when GitHub is unreachable or failing |

> **Note:** `POST /api/feedback/create` and `POST /results/{job_id}/create-github-issue` share one refusal helper, so both keep `502` for GitHub trouble (unreachable, timeout, any `5xx`) and `403` for credential trouble, including a `404` from GitHub — that is how GitHub answers a repository the token cannot see, and reporting it as `403` keeps it away from the gateway-failure class. The two are separated on purpose: `502` means retry, `403` means fix the token. Only the subject differs — a refused feedback submission says the feedback was not filed, a refused failure issue says the bug report was not filed. Each refusal is logged at `WARNING` — the upstream status and the tracker's own `message` when there was an answer, the exception type and text when the call never landed — never the token and never the submitted body.
>
> `POST /results/{job_id}/create-jira-bug` applies the same split to Jira: `401` for an invalid or expired token, `403` for a project the token cannot access — Jira hides it behind a `404`, the same trick GitHub plays — and `502` with a retryable outage detail on an upstream `5xx`. Anything else stays `502 Jira API error: <status>`, with no rate-limit wording invented. Its refusals are logged the same way, including Jira's own `errorMessages`.
>
> Auditing is narrower than that: a refused `feedback.create` is recorded as a `denied` event with reason `forbidden` for the mapped `401`/`403` refusals (an invalid token, a missing `repo` scope, a repository the token cannot see), and as a `failure` event for the rest — `upstream_failure` for a `502`, `http_429` for the rate limit. Its `details` carries `upstream_status` only when the upstream status is not already named by that reason code — `401`, `403`, and `429` are omitted, every other status (a `404`, a `5xx`, a `422`) is recorded. Refusals on `create-github-issue` are not audited at all, and neither is a `create-jira-bug` refusal that reached for the tracker: only a successful create writes an `issue.create` row, so those paths have no `upstream_status` to read. The one exception is a `create-jira-bug` refusal for a supplied deployment-credential token — a credential-policy denial, not a tracker refusal — which writes an `issue.create` row with `outcome: denied`, a fixed reason code and no title, body or `upstream_status` (nothing reached the tracker).

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
| GET | `/api/admin/server-status` | Live server status: `disk` (filesystem usage of the data directory plus `database`/`transcripts`/`logs` sizes), `cpu` (`process_percent`, `quota_cores`, `quota_state` = `set`/`unlimited`/`unknown`), `memory` (`rss_bytes`, `current_bytes`, `working_set_bytes`, `limit_bytes`, `limit_state`, `percent_of_limit` from the working set), `process` (`git_sha`, `restart_count` — `null` inside a container), `database` (size including the WAL, row counts, `compression_ratio`), `sidecar`, `activity` (`analyses_running`, `analyses` with per-analysis `elapsed_seconds`, `chat_turns_in_flight`, `sse_clients`), `health_monitor` (`enabled`, `interval_seconds`, `last_sample_ago_seconds`, and `conditions`: one row per condition with `key`, `label`, `state` = `ok`/`firing`, `held_seconds`, `threshold_seconds`, and `age_seconds`/`age_known` for the stuck-analysis condition — `age_known: false` means the age could not be read, which is *not* the same as `age_seconds: null` with `age_known: true`, meaning nothing is running), and `warnings`. Each probe block carries `available`; a probe that cannot read its source answers `{"available": false, "reason": "..."}` instead of failing the request, the health monitor's block included |
| GET | `/api/admin/db/schema` | Read-only schema dump |
| GET | `/api/admin/results-retention` | Dry run for results retention: what the limits would delete right now. Parameters `retention_days` (optional, `0`–`36500`) and `max_rows` (optional, `0`–`9223372036854775807`); an omitted parameter uses the stored setting. Returns `{enabled, retention_days, max_rows, jobs, truncated, oldest_created_at, newest_created_at, rows: {<table>: <count>}}`, where `rows` counts every job-scoped table plus `results` for exactly the jobs listed in `jobs`. With both limits `0` it answers `200` with `enabled: false`, `jobs: 0`, `rows: {}`. Read-only: no audit row, nothing deleted, and it applies the same live-task guards and per-run bound as the prune itself. Admin only |
| POST | `/api/admin/db/query` | Read-only SQL for the admin chat |
| GET | `/api/admin/chat` | Admin chat history |
| POST | `/api/admin/chat` | Send an admin chat message; returns `202` |
| POST | `/api/admin/chat/init`, `/close`, `/abort` | Admin chat session control; admin chat init stays synchronous and answers its own `422 AI session refused: …` (job chat reports a refusal as a transcript message instead) |
| DELETE | `/api/admin/chat` | Clear admin chat history |
| GET | `/api/admin/chat/stream` | Admin chat SSE: new messages **and the answer as it is written** (`chat-delta` frames) |
| POST | `/api/admin-chat/artifacts` | Save a generated HTML report artifact |
| GET | `/api/admin-chat/artifacts/{artifact_id}` | Download that artifact |

See [Configuration Reference](configuration-reference.html) for the full environment variable surface behind these admin settings.

#### Account state

`GET /api/admin/users` rows carry `locked` (bool), `locked_at` (UTC timestamp or null), `disabled` (bool) and `failed_login_count` (int) alongside the role and grants, so an admin can see why an account cannot sign in without opening anything else.

`POST /api/admin/users/{username}/unlock` clears both the lock and the consecutive-failure counter and records `admin.user_unlock` (details: `was_locked_at`, `failed_attempts`).

`PUT /api/admin/users/{username}/disabled` with `{"disabled": true|false}` disables or re-enables the account and records `admin.user_disabled` / `admin.user_enabled`. A disabled account is refused with the *same* `401` body as a wrong credential — the response never reveals which condition applied — while its history, comments, jobs and audit rows stay intact.

#### Audit events

Each entry of `GET /api/admin/settings` carries `is_set` (whether a value exists, without revealing a sensitive one), `label` (the human name), `env_var`, `options`/`options_source`/`multiple`/`allow_custom` (for enumerated settings) and `server_only`/`derived`/`persistable`. A client renders `(empty)` from `is_set: false` rather than masking a value that does not exist.

`GET /api/admin/audit` returns `{events, total, limit, offset}` newest-first. Each event carries `id`, `created_at` (UTC ISO-8601 with a trailing `Z`), `actor`, `actor_role`, `is_admin`, `action`, `target_type`, `target_id`, `target_label`, `target_job_id`, `outcome`, `details` (the stored JSON object, already parsed), `request_ip`, and the chain columns `prev_hash` and `entry_hash`.

`target_label` and `target_job_id` are resolved **when the row is read** and are never stored or hashed: `target_label` is the human name of the target (the job's display name for a `job` target — whether `target_id` holds a uuid or the job name — and the containing job's name for a `comment` target), and `target_job_id` is the job uuid a client can link to, resolved from `target_id` for `job` targets and from `details.job_id` for `comment` targets. Both are `null` when nothing resolves, so a client can tell "no destination" from "destination unknown yet" and never render a dead link.

```bash
curl -s "$BASE/api/admin/audit?actor=alice&action=admin.job_delete&since=2026-04-01T00:00:00Z&limit=20" \
  -H "Authorization: Bearer $KEY"
```

Filters combine with AND. `actor` must match the stored value exactly but ignores case. `action` accepts one id or several, either as a repeated parameter (`?action=a&action=b`) or comma-separated (`?action=a,b`). `q` is a substring match over the event's `details` object. `since` and `until` are inclusive and accept any ISO 8601 timestamp; a value that cannot be parsed is rejected with `400`.

Time bounds are timezone-correct. A bare `YYYY-MM-DD` means a UTC day, so `since=2026-06-15` starts at `00:00:00Z` and `until=2026-06-15` covers through `23:59:59.999999Z`. An explicit offset (`2026-06-15T22:00:00+02:00`) is honoured and converted to UTC; a timestamp with no offset is read as UTC. Every stored `created_at` carries an explicit `Z`, so a filtered result means the same instant no matter which timezone you built the query in.

`GET /api/admin/audit/actions` lists the action ids seen so far with their event count, last-seen timestamp, and a display label (`auth.login_failed` becomes `Auth Login Failed`) for building filter menus.

`GET /api/admin/audit/verify` recomputes the chain: each row's `entry_hash` is `sha256(canonical_json(row) + prev_hash)` and its `prev_hash` is the previous row's `entry_hash`. It returns `{checked, legacy, ok, first_broken_id, truncated, truncated_before_id}`. `checked` is how many chained rows were verified and `legacy` how many rows predate the chain — those are counted separately, never reported as broken. Legacy means a NULL hash on a row **older than the first chained row**; a NULL hash above that boundary is a break, so clearing a row's hash cannot launder an edited row. `ok` is false when a row was edited or deleted, and `first_broken_id` names the oldest offending row. Retention pruning removes the head of the chain by design, so the oldest surviving row points at a deleted hash: that boundary is reported as `truncated: true` with `truncated_before_id`, and the walk continues from the recorded `prev_hash` instead of failing. Truncation is only accepted when it matches the stored prune checkpoint — the id and hash of the last row the retention job (or the row cap) deleted. A prefix removed any other way does not match the checkpoint and is reported as a break (`ok: false`, `first_broken_id` = the oldest surviving row), so deleting old rows by hand is still tampering. A mismatch inside the surviving window always sets `ok: false`.

`GET /api/admin/audit/export` streams every event the same filters match — `format=json` (a JSON array) or `format=csv`, with columns `id, created_at, actor, actor_role, is_admin, action, target_type, target_id, outcome, request_ip, details, hash_version, prev_hash, entry_hash, hash_payload`. Both hash columns are included so an export verifies on its own after the server has pruned the originals. Each JSON event also carries `details_raw` (the exact stored `details` text) and `hash_payload` — the exact canonical JSON that was hashed. Verification is therefore one line on either format: `sha256(hash_payload + prev_hash) == entry_hash`. `hash_version` says which field list the payload covers: version `1` is the original field set (every row written before the rescue counter existed), version `2` adds `throttle_rescue`, so the rescue budget itself is tamper-evident. A trail can mix both versions and verifies row by row; nothing is rewritten when the field list grows. Re-mapping fields yourself will not work, because the API renders `is_admin` as a boolean while the stored row hashed `1`; `hash_payload` carries the stored form. A row written before the chain existed, or one whose stored bytes would expose a credential that read-time redaction removes, ships without `hash_payload` — treat a missing payload as "not verifiable", never as verified. Responses are streamed in batches, and the batches page by row id rather than by offset, so events written mid-export can only join the tail — a row is never repeated and never skipped. This is the supported way to retain audit evidence before retention pruning or the row cap deletes it. CSV text cells that start with `=`, `+`, `-`, `@`, a tab or a carriage return are prefixed with `'` so a spreadsheet cannot execute them as formulas; a cell that already starts with `'` is prefixed too (`'abc` exports as `''abc`), which makes the escaping reversible. To recompute a hash from an exported CSV, remove exactly one leading apostrophe from each text cell — the unescaped value is byte-identical to the stored column. Numbers and ids are never escaped.

All audit read endpoints require `admin` and return `403 Admin access required` otherwise. `GET /api/admin/audit/stream` pushes each new event as it is written, so an audit page can stay live without polling. Events pass through the same redaction as the list endpoint. A subscriber that cannot keep up is never silently starved: when its queue overflows the stream emits an `audit-gap` event with `{"dropped": N}` at the next event **or** at the next keepalive, whichever comes first — so a burst dropped into an emptying queue is reported within one keepalive window rather than waiting for an event that may never come. A client can then tell the administrator that events were missed instead of showing a broken view as live. The event name and the count are unchanged.


## Advanced Usage

Prefer the CLI when you can. Most endpoints above have a `rootcoz` command counterpart. SSE streams have none, because the CLI is a one-shot tool, and the user-token store endpoints (`GET`/`PUT /api/user/tokens`) have client methods but no registered CLI command either — the token *test* endpoint does: `rootcoz user tokens test github|jira`, which exits non-zero when the result is `ok: false`. See [CLI Command Reference](cli-reference.html) and [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html).

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