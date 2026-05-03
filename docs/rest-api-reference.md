# REST API Reference

> **Note:** When a request field says `server default`, omitting it uses the server's configured value. See [Configuration and Environment Reference](configuration-and-environment-reference.html) for the server-side defaults and environment variables.
>


> **Note:** This page covers the live analysis, history, comments, auth-state, notifications, metadata, and admin APIs. For issue preview/create endpoints, see [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html). For Report Portal endpoints, see [Push Classifications to Report Portal](push-classifications-to-report-portal.html). For deployment health and metrics, see [Copy Common Deployment Recipes](copy-common-deployment-recipes.html).

## Common conventions
- Examples use `http://localhost:8000`.
- Admin-only endpoints return `403 Forbidden` without admin access.
- Current-user endpoints return `401 Unauthorized` when no current username is available.
- Non-admin write endpoints can also return `403 Forbidden` when the server allow list rejects the caller.
- Validation errors return `422 Unprocessable Entity` with a FastAPI-style error array.
- For `GET /results/{job_id}`, send `Accept: application/json` when you want API JSON instead of the browser UI route behavior.

```json
{
  "detail": [
    {
      "type": "int_parsing",
      "loc": ["body", "build_number"],
      "msg": "Input should be a valid integer",
      "input": "not-a-number"
    }
  ]
}
```

### Queued job response
Returned by `POST /analyze` and `POST /re-analyze/{job_id}`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | string | n/a | Always `queued`. |
| `job_id` | string | n/a | Server-generated job UUID. |
| `message` | string | n/a | Polling hint that includes the result path. |
| `base_url` | string | `""` | Trusted public base URL when configured; otherwise empty. |
| `result_url` | string | n/a | Relative or absolute URL for the stored result. |

```json
{
  "status": "queued",
  "job_id": "8d0f0a65-6b52-4eb9-8dc5-9e3c38f6f6a6",
  "message": "Analysis job queued. Poll /results/8d0f0a65-6b52-4eb9-8dc5-9e3c38f6f6a6 for status.",
  "base_url": "",
  "result_url": "/results/8d0f0a65-6b52-4eb9-8dc5-9e3c38f6f6a6"
}
```

### Stored result envelope
Returned by `GET /results/{job_id}`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | n/a | Analysis job ID. |
| `jenkins_url` | string | `""` | Jenkins build URL for Jenkins-backed analyses. |
| `status` | string | n/a | Top-level job state: `pending`, `waiting`, `running`, `completed`, or `failed`. |
| `result` | object \| null | n/a | Stored result payload. Secrets are stripped from `request_params` before response. |
| `created_at` | string | n/a | Job creation timestamp. |
| `completed_at` | string \| null | `null` | Completion timestamp when available. |
| `analysis_started_at` | string \| null | `null` | Analysis-start timestamp when available. |
| `base_url` | string | `""` | Trusted public base URL when configured; otherwise empty. |
| `result_url` | string | n/a | Relative or absolute result URL. |
| `capabilities` | object | n/a | Same capability flags returned by `GET /api/capabilities`. |

```json
{
  "job_id": "8d0f0a65-6b52-4eb9-8dc5-9e3c38f6f6a6",
  "jenkins_url": "https://jenkins.example.com/job/my-pipeline/42/",
  "status": "completed",
  "result": {
    "job_name": "my-pipeline",
    "build_number": 42,
    "summary": "1 failure analyzed",
    "ai_provider": "claude",
    "ai_model": "opus-4",
    "failures": [],
    "request_params": {
      "ai_provider": "claude",
      "ai_model": "opus-4",
      "tests_repo_url": "https://github.com/acme/tests",
      "tests_repo_ref": ""
    }
  },
  "created_at": "2026-04-27 09:20:00",
  "completed_at": "2026-04-27 09:21:10",
  "analysis_started_at": "2026-04-27 09:20:05",
  "base_url": "",
  "result_url": "/results/8d0f0a65-6b52-4eb9-8dc5-9e3c38f6f6a6",
  "capabilities": {
    "github_issues_enabled": true,
    "jira_issues_enabled": true,
    "server_github_token": true,
    "server_jira_token": true,
    "server_jira_email": true,
    "server_jira_project_key": "ACME",
    "reportportal": false,
    "reportportal_project": ""
  }
}
```

## Analysis
> **Note:** For task-oriented usage and option combinations, see [Analyze a Jenkins Job](analyze-a-jenkins-job.html), [Customize AI Analysis](customize-ai-analysis.html), and [Copy Common Analysis Recipes](copy-common-analysis-recipes.html).

### Shared analysis override fields
Used by `POST /analyze`, `POST /analyze-failures`, and `POST /re-analyze/{job_id}`.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `tests_repo_url` | string \| null | server default | Repository URL cloned for analysis context. A `:ref` suffix is accepted. |
| `ai_provider` | string | server default | AI provider: `claude`, `gemini`, or `cursor`. |
| `ai_model` | string \| null | server default | AI model identifier. |
| `enable_jira` | boolean \| null | auto | Enables or disables Jira match enrichment for this request. |
| `ai_cli_timeout` | integer \| null | server default | AI CLI timeout in minutes. |
| `jira_url` | string \| null | server default | Jira base URL override. |
| `jira_email` | string \| null | server default | Jira Cloud email override. |
| `jira_api_token` | string \| null | server default | Jira Cloud API token override. |
| `jira_pat` | string \| null | server default | Jira Server/DC PAT override. |
| `jira_project_key` | string \| null | server default | Jira project key override. |
| `jira_ssl_verify` | boolean \| null | server default | Jira SSL verification override. |
| `jira_max_results` | integer \| null | server default | Max Jira matches to fetch. |
| `raw_prompt` | string \| null | none | Extra prompt text appended for this request. |
| `github_token` | string \| null | server default | GitHub token used for private-repo comment enrichment. |
| `peer_ai_configs` | array<object> \| null | server default | Peer review AI configs. Use `[]` to disable peer analysis for this request. |
| `peer_analysis_max_rounds` | integer | server default | Max peer-debate rounds. Only applied when the field is present. |
| `additional_repos` | array<object> \| null | server default | Additional repositories cloned for AI context. Use `[]` to disable server defaults for this request. |

#### `peer_ai_configs[]`

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `ai_provider` | string | required | `claude`, `gemini`, or `cursor`. |
| `ai_model` | string | required | Non-blank model identifier. |

#### `additional_repos[]`

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Unique directory name used inside the analysis workspace. |
| `url` | string | required | Repository URL to clone. |
| `ref` | string | `""` | Branch or tag to check out. Empty string means the remote default branch. |
| `token` | string \| null | `null` | Clone token for private repositories. |

> **Tip:** `additional_repos[].name` must be unique, must not contain path separators, must not contain `..`, and must not start with `.`.

```json
{
  "tests_repo_url": "https://github.com/acme/tests:main",
  "ai_provider": "claude",
  "ai_model": "opus-4",
  "peer_ai_configs": [
    {
      "ai_provider": "gemini",
      "ai_model": "2.5-pro"
    }
  ],
  "peer_analysis_max_rounds": 2,
  "additional_repos": [
    {
      "name": "service",
      "url": "https://github.com/acme/service",
      "ref": "main"
    }
  ]
}
```

### `/analyze` body fields

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_name` | string | required | Jenkins job name. Folder-style names such as `folder/subfolder/job` are supported. |
| `build_number` | integer | required | Jenkins build number. |
| `force` | boolean | server default | Forces analysis even when the Jenkins build succeeded. |
| `wait_for_completion` | boolean | server default | Waits for the Jenkins build to finish before analysis. |
| `poll_interval_minutes` | integer | server default | Poll interval, in minutes, when waiting for build completion. |
| `max_wait_minutes` | integer | server default | Max wait time in minutes. `0` means no limit. |
| `jenkins_url` | string \| null | server default | Jenkins base URL override. |
| `jenkins_user` | string \| null | server default | Jenkins username override. |
| `jenkins_password` | string \| null | server default | Jenkins password or API token override. |
| `jenkins_ssl_verify` | boolean \| null | server default | Jenkins SSL verification override. |
| `jenkins_timeout` | integer \| null | server default | Jenkins API timeout in seconds. |
| `jenkins_artifacts_max_size_mb` | integer \| null | server default | Max artifact payload size downloaded for AI context. |
| `get_job_artifacts` | boolean \| null | server default | Enables or disables artifact download for this request. |

```json
{
  "job_name": "folder/my-pipeline",
  "build_number": 42,
  "ai_provider": "claude",
  "ai_model": "opus-4",
  "wait_for_completion": true,
  "poll_interval_minutes": 2
}
```

### `/analyze-failures` input objects

#### `failures[]`

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `test_name` | string | required | Fully qualified test name. |
| `error_message` | string | `""` | Failure message. |
| `stack_trace` | string | `""` | Full stack trace. |
| `duration` | number | `0.0` | Test duration in seconds. |
| `status` | string | `FAILED` | Failure status label. |

#### Request fields

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `failures` | array<object> \| null | none | Raw failures to analyze. Required unless `raw_xml` is provided. |
| `raw_xml` | string \| null | none | Raw JUnit XML. Required unless `failures` is provided. |

```json
{
  "failures": [
    {
      "test_name": "tests.test_auth.test_login",
      "error_message": "assert False",
      "stack_trace": "File tests/test_auth.py, line 42"
    }
  ],
  "ai_provider": "claude",
  "ai_model": "opus-4"
}
```

### `POST /analyze`
Submit a Jenkins build for asynchronous analysis.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| Body | object | required | Shared analysis override fields plus `/analyze` body fields. |

Return value/effect:
- `202 Accepted` returns the queued job response.
- `400 Bad Request` when AI provider/model configuration is missing or invalid.
- `403 Forbidden` when the allow list rejects the caller.
- `422 Unprocessable Entity` for validation errors.

```bash
curl -X POST http://localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "job_name": "folder/my-pipeline",
    "build_number": 42,
    "ai_provider": "claude",
    "ai_model": "opus-4"
  }'
```

### `POST /analyze-failures`
Analyze raw failures or raw JUnit XML without Jenkins polling.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| Body | object | required | Shared analysis override fields plus `/analyze-failures` input fields. |

Return value/effect:
- `200 OK` returns a direct-analysis result object with `job_id`, `status`, `summary`, `ai_provider`, `ai_model`, `failures`, optional `enriched_xml`, optional `token_usage`, plus `base_url` and `result_url`.
- `status` inside the response body is `completed` or `failed`.
- `400 Bad Request` for invalid XML or invalid/missing AI configuration.
- `422 Unprocessable Entity` when both `failures` and `raw_xml` are supplied, when neither is supplied, or when nested validation fails.
- If `raw_xml` contains no failures, the endpoint still returns `200` with `status: "completed"` and the original XML in `enriched_xml`.

```bash
curl -X POST http://localhost:8000/analyze-failures \
  -H 'Content-Type: application/json' \
  -d '{
    "failures": [
      {
        "test_name": "tests.test_auth.test_login",
        "error_message": "assert False",
        "stack_trace": "File tests/test_auth.py, line 42"
      }
    ],
    "ai_provider": "claude",
    "ai_model": "opus-4"
  }'
```

### `POST /re-analyze/{job_id}`
Queue a fresh analysis for an existing result, reusing the original stored request parameters and applying any override fields supplied in the body.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Existing analysis job ID to reuse as the source request. |
| Body | object | `{}` | Shared analysis override fields only. Omitted fields reuse the stored request parameters. |

Return value/effect:
- `202 Accepted` returns the queued job response for the new job ID.
- `400 Bad Request` when the stored result has no reusable `request_params` or cannot be reconstructed.
- `404 Not Found` when the source `job_id` does not exist.
- `403 Forbidden` when the allow list rejects the caller.
- `422 Unprocessable Entity` for override-field validation errors.

```bash
curl -X POST http://localhost:8000/re-analyze/8d0f0a65-6b52-4eb9-8dc5-9e3c38f6f6a6 \
  -H 'Content-Type: application/json' \
  -d '{
    "ai_provider": "gemini",
    "ai_model": "2.5-pro"
  }'
```

### `GET /results/{job_id}`
Fetch the stored result envelope for a queued, running, completed, or failed job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |
| `Accept` header | string | `application/json` | Use `application/json` for API JSON responses. |

Return value/effect:
- `200 OK` returns the stored result envelope when the job is completed or failed.
- `202 Accepted` returns the same envelope shape while the job is still `pending`, `waiting`, or `running`.
- `404 Not Found` when the job does not exist.
- `result.request_params` is included when available, but secret fields and `additional_repos[].token` are removed from the response.

> **Note:** Browser-style HTML requests can redirect to UI routes instead of returning JSON.

```bash
curl http://localhost:8000/results/8d0f0a65-6b52-4eb9-8dc5-9e3c38f6f6a6 \
  -H 'Accept: application/json'
```

### `GET /results`
List recent analysis jobs.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `limit` | integer | `50` | Max number of rows to return. Maximum `100`. |

Return value/effect:
- `200 OK` returns an array of recent result summaries.
- Each item includes `job_id`, `jenkins_url`, `status`, and `created_at`.
- `422 Unprocessable Entity` when `limit` exceeds `100` or fails validation.

```bash
curl 'http://localhost:8000/results?limit=10'
```

### `GET /api/dashboard`
List dashboard entries with summary fields extracted from stored results.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns an array of dashboard entries.
- Every item includes `job_id`, `jenkins_url`, `status`, `created_at`, `completed_at`, `analysis_started_at`, `reviewed_count`, and `comment_count`.
- Items can also include `job_name`, `build_number`, `failure_count`, `child_job_count`, `summary`, and `error` when those values exist in `result_json`.

```bash
curl http://localhost:8000/api/dashboard
```

### `GET /api/capabilities`
Return server-level feature and credential flags used by the UI.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns:
  - `github_issues_enabled`
  - `jira_issues_enabled`
  - `server_github_token`
  - `server_jira_token`
  - `server_jira_email`
  - `server_jira_project_key`
  - `reportportal`
  - `reportportal_project`

```bash
curl http://localhost:8000/api/capabilities
```

## Comments and review
> **Note:** For UI workflow details, see [Review and Classify Failures](review-and-classify-failures.html).

### `GET /results/{job_id}/comments`
Return all stored comments and review states for a job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |

Return value/effect:
- `200 OK` returns:
  - `comments`: array of comment objects with `id`, `job_id`, `test_name`, `child_job_name`, `child_build_number`, `comment`, `error_signature`, `username`, and `created_at`
  - `reviews`: object keyed by `test_name` for top-level failures, or `child_job_name#child_build_number::test_name` for child-job failures
- Review values include `reviewed`, `username`, and `updated_at`.

```bash
curl http://localhost:8000/results/job-123/comments
```

### `POST /results/{job_id}/comments`
Add a comment to one stored failure.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |
| `test_name` | string | required | Failure test name. |
| `comment` | string | required | Comment body. |
| `child_job_name` | string | `""` | Child job name for nested failures. |
| `child_build_number` | integer | `0` | Child build scope. `0` acts as a wildcard when `child_job_name` is supplied. |

Return value/effect:
- `201 Created` returns `{ "id": <comment_id> }`.
- `400 Bad Request` when the test name is not present in the stored result or comment creation fails validation.
- `404 Not Found` when the job does not exist.
- `403 Forbidden` when the allow list rejects the caller.
- When push notifications are configured, `@mentions` in the comment trigger best-effort notification fan-out.

```bash
curl -X POST http://localhost:8000/results/job-123/comments \
  -H 'Content-Type: application/json' \
  -d '{
    "test_name": "tests.test_auth.test_login",
    "comment": "Opened ACME-123 for this failure."
  }'
```

### `DELETE /results/{job_id}/comments/{comment_id}`
Delete one comment.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |
| `comment_id` | integer | required | Comment ID. |

Return value/effect:
- `200 OK` returns `{ "status": "deleted" }`.
- `401 Unauthorized` when no current username is available.
- `404 Not Found` when the comment is missing, or when a non-admin caller tries to delete a comment they do not own.
- `403 Forbidden` when the allow list rejects the caller.
- Admin callers can delete any comment for the job.

```bash
curl -X DELETE http://localhost:8000/results/job-123/comments/17
```

### `PUT /results/{job_id}/reviewed`
Set or clear the reviewed state for one failure.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |
| `test_name` | string | required | Failure test name. |
| `reviewed` | boolean | required | `true` marks the failure reviewed; `false` clears it. |
| `child_job_name` | string | `""` | Child job name for nested failures. |
| `child_build_number` | integer | `0` | Child build scope. `0` acts as a wildcard when `child_job_name` is supplied. |

Return value/effect:
- `200 OK` returns `{ "status": "ok", "reviewed_by": "<username-or-empty>" }`.
- `400 Bad Request` when the test is not present in the stored result.
- `404 Not Found` when the job does not exist.
- `403 Forbidden` when the allow list rejects the caller.

```bash
curl -X PUT http://localhost:8000/results/job-123/reviewed \
  -H 'Content-Type: application/json' \
  -d '{
    "test_name": "tests.test_auth.test_login",
    "reviewed": true
  }'
```

### `GET /results/{job_id}/review-status`
Return dashboard-style review counts for one job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |

Return value/effect:
- `200 OK` returns:
  - `total_failures`
  - `reviewed_count`
  - `comment_count`
- If the job has no stored result, the counts are `0`.

```bash
curl http://localhost:8000/results/job-123/review-status
```

### `POST /results/{job_id}/enrich-comments`
Resolve live status information for GitHub pull requests, GitHub issues, and Jira keys found inside stored comments.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |

Return value/effect:
- `200 OK` returns `{ "enrichments": { ... } }`.
- `enrichments` is keyed by comment ID string.
- Each value is an array of objects with:
  - `type`: `github_pr`, `github_issue`, or `jira`
  - `key`: tracker-specific identifier such as `owner/repo#123` or `ACME-123`
  - `status`: tracker status string
- `403 Forbidden` when the allow list rejects the caller.

```bash
curl -X POST http://localhost:8000/results/job-123/enrich-comments
```

### `PUT /results/{job_id}/override-classification`
Override the primary classification for one failure group inside a stored result.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |
| `test_name` | string | required | Failure test name used to identify the group. |
| `classification` | string | required | One of `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE`. |
| `child_job_name` | string | `""` | Child job name for nested failures. |
| `child_build_number` | integer | `0` | Child build number. When `child_job_name` is set for this endpoint, a non-zero build number is required. |

Return value/effect:
- `200 OK` returns `{ "status": "ok", "classification": "<value>" }`.
- The override is applied to all failures in the same error-signature group within the job.
- `400 Bad Request` when the job exists but the target failure cannot be resolved, or when child-job scoping is invalid.
- `404 Not Found` when the job does not exist.
- `403 Forbidden` when the allow list rejects the caller.
- `422 Unprocessable Entity` when `classification` is not one of the allowed values.

```bash
curl -X PUT http://localhost:8000/results/job-123/override-classification \
  -H 'Content-Type: application/json' \
  -d '{
    "test_name": "tests.test_auth.test_login",
    "classification": "PRODUCT BUG"
  }'
```

## History
> **Note:** For investigative workflows and interpretation guidance, see [Investigate Failure History](investigate-failure-history.html).

### `GET /history/failures`
List failure-history rows with pagination and optional filters.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `search` | string | `""` | Free-text search across `test_name`, `error_message`, and `job_name`. |
| `job_name` | string | `""` | Exact `job_name` filter. |
| `classification` | string | `""` | Exact classification filter. |
| `limit` | integer | `50` | Max rows to return. Maximum `200`. |
| `offset` | integer | `0` | Number of rows to skip. Minimum `0`. |

Return value/effect:
- `200 OK` returns:
  - `failures`: array of rows with `id`, `job_id`, `job_name`, `build_number`, `test_name`, `error_message`, `error_signature`, `classification`, `child_job_name`, `child_build_number`, and `analyzed_at`
  - `total`: total row count before pagination
- `422 Unprocessable Entity` for query validation errors.

```bash
curl 'http://localhost:8000/history/failures?job_name=my-pipeline&classification=FLAKY&limit=25&offset=0'
```

### `GET /history/test/{test_name}`
Return historical statistics and recent failure rows for one test.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `test_name` | string | required | Test name path parameter. |
| `limit` | integer | `20` | Max recent runs to return. Maximum `100`. |
| `job_name` | string | `""` | Exact job-name filter. |
| `exclude_job_id` | string | `""` | Excludes rows from one analysis job. |

Return value/effect:
- `200 OK` returns:
  - `test_name`
  - `total_runs`
  - `failures`
  - `passes`
  - `failure_rate`
  - `first_seen`
  - `last_seen`
  - `last_classification`
  - `classifications`
  - `recent_runs`
  - `comments`
  - `consecutive_failures`
  - `note`
- `passes` and `failure_rate` can be `null` when no `job_name` filter is supplied and the endpoint cannot compute a pass denominator.
- When no matching history exists, the response still returns `200` with zeroed counts and empty collections.

```bash
curl 'http://localhost:8000/history/test/tests.test_auth.test_login?limit=10&job_name=my-pipeline'
```

### `GET /history/search`
Find failures that share one error signature.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `signature` | string | required | Error-signature hash to search. |
| `exclude_job_id` | string | `""` | Excludes rows from one analysis job. |

Return value/effect:
- `200 OK` returns:
  - `signature`
  - `total_occurrences`
  - `unique_tests`
  - `tests`: array of `{ "test_name": "...", "occurrences": <int> }`
  - `last_classification`
  - `comments`: array of `{ "comment": "...", "username": "...", "created_at": "..." }`
- `422 Unprocessable Entity` when `signature` is omitted.

```bash
curl 'http://localhost:8000/history/search?signature=abc123def456'
```

### `GET /history/stats/{job_name}`
Return aggregate failure statistics for one Jenkins job name.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_name` | string | required | Jenkins job name path parameter. |
| `exclude_job_id` | string | `""` | Excludes rows from one analysis job. |

Return value/effect:
- `200 OK` returns:
  - `job_name`
  - `total_builds_analyzed`
  - `builds_with_failures`
  - `overall_failure_rate`
  - `most_common_failures`: array of `{ "test_name": "...", "count": <int>, "classification": "..." }`
  - `recent_trend`: `stable`, `improving`, or `worsening`
- If no history exists for the job, the endpoint still returns `200` with zeroed counters and `recent_trend: "stable"`.

```bash
curl 'http://localhost:8000/history/stats/my-pipeline'
```

### `POST /history/classify`
Create a history classification record for one test.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `test_name` | string | required | Test name to classify. |
| `classification` | string | required | One of `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, `KNOWN_BUG`, or `INTERMITTENT`. |
| `reason` | string | `""` | Free-text reason. |
| `job_name` | string | `""` | Job name scope stored on the classification record. |
| `references` | string | `""` | Reference text such as Jira keys or URLs. |
| `job_id` | string | required | Analysis job ID that the classification is tied to. |
| `child_build_number` | integer | `0` | Child build scope. `0` acts as the wildcard value. |

Return value/effect:
- `201 Created` returns `{ "id": <classification_id> }`.
- `400 Bad Request` when `test_name` is blank or when `KNOWN_BUG` is sent without non-empty `references`.
- `403 Forbidden` when the allow list rejects the caller.
- `422 Unprocessable Entity` for validation errors.

> **Warning:** `classification: "KNOWN_BUG"` requires a non-empty `references` value.

```bash
curl -X POST http://localhost:8000/history/classify \
  -H 'Content-Type: application/json' \
  -d '{
    "test_name": "tests.test_auth.test_login",
    "classification": "FLAKY",
    "reason": "Intermittent network timeout",
    "job_id": "job-123"
  }'
```

### `GET /history/classifications`
List visible primary classification records.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `test_name` | string | `""` | Exact test-name filter. |
| `classification` | string | `""` | Exact classification filter. |
| `job_name` | string | `""` | Exact job-name filter. |
| `parent_job_name` | string | `""` | Exact parent-job-name filter. |
| `job_id` | string | `""` | Exact job-ID filter. |

Return value/effect:
- `200 OK` returns `{ "classifications": [...] }`.
- Each classification row includes `id`, `test_name`, `job_name`, `parent_job_name`, `classification`, `reason`, `references_info`, `created_by`, `job_id`, `child_build_number`, and `created_at`.
- This endpoint returns the visible primary-domain records used for stored failure overrides.

```bash
curl 'http://localhost:8000/history/classifications?classification=PRODUCT%20BUG'
```

## Auth state and saved user tokens
> **Note:** This section documents the live auth-state and saved-token endpoints. For user bootstrap, API keys, and role-management workflows, see [Manage Users, Access, and Token Usage](manage-users-access-and-token-usage.html). For tracker-specific profile workflows, see [Configure Your Profile and Notifications](configure-your-profile-and-notifications.html).

### `GET /api/auth/me`
Return the current request's resolved user identity.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns:
  - `username`
  - `role`
  - `is_admin`
- The endpoint also returns `200` when no current user exists; in that case `username` is empty and `is_admin` is `false`.

```javascript
const res = await fetch("/api/auth/me", { credentials: "include" });
console.log(await res.json());
```

### `POST /api/auth/logout`
Clear the current admin-auth state.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns `{ "ok": true }`.

```javascript
const res = await fetch("/api/auth/logout", {
  method: "POST",
  credentials: "include"
});
console.log(await res.json());
```

### `GET /api/user/tokens`
Return the current user's saved tracker tokens.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns `{ "github_token": "...", "jira_email": "...", "jira_token": "..." }`.
- If the current username is unknown to the database, the endpoint still returns `200` with empty strings for all three fields.
- `401 Unauthorized` when no current user exists.

```javascript
const res = await fetch("/api/user/tokens", { credentials: "include" });
console.log(await res.json());
```

### `PUT /api/user/tokens`
Merge non-empty token fields into the current user's saved token record.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `github_token` | string | omitted | GitHub personal access token. |
| `jira_email` | string | omitted | Jira Cloud email. |
| `jira_token` | string | omitted | Jira token. |

Return value/effect:
- `200 OK` returns `{ "ok": true }`.
- Only non-empty fields in the request body are written.
- Omitted fields are left unchanged.
- `401 Unauthorized` when no current user exists.
- `404 Not Found` when the current username does not exist in the database.

> **Warning:** Blank-string values are not persisted as clears; only non-empty values are written.

```javascript
const res = await fetch("/api/user/tokens", {
  method: "PUT",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    github_token: "ghp_example",
    jira_email: "alice@example.com",
    jira_token: "jira-example"
  })
});
console.log(await res.json());
```

## Notifications and mentions
> **Note:** For end-user setup and browser flow details, see [Configure Your Profile and Notifications](configure-your-profile-and-notifications.html).

### `GET /api/notifications/vapid-public-key`
Return the public VAPID key used by browser push subscriptions.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns `{ "vapid_public_key": "..." }`.
- `404 Not Found` when Web Push is not configured.
- `503 Service Unavailable` when push support is enabled but the VAPID keys are unavailable.

```javascript
const res = await fetch("/api/notifications/vapid-public-key");
console.log(await res.json());
```

### `POST /api/notifications/subscribe`
Register or update one push subscription for the current user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `endpoint` | string | required | HTTPS push endpoint URL. Maximum length `2048`. |
| `p256dh_key` | string | required | Client public key. Maximum length `256`. |
| `auth_key` | string | required | Client auth secret. Maximum length `256`. |

Return value/effect:
- `200 OK` returns `{ "status": "subscribed" }`.
- The subscription is upserted by `endpoint`.
- The server keeps at most `10` subscriptions per user and drops the oldest extras.
- `401 Unauthorized` when no current user exists.
- `404 Not Found` when Web Push is not configured.
- `403 Forbidden` when the allow list rejects the caller.
- `422 Unprocessable Entity` when validation fails, including non-HTTPS endpoints.

```javascript
const res = await fetch("/api/notifications/subscribe", {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    endpoint: "https://push.example.com/sub/abc123",
    p256dh_key: "p256dh-test",
    auth_key: "auth-test"
  })
});
console.log(await res.json());
```

### `POST /api/notifications/unsubscribe`
Remove one push subscription for the current user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `endpoint` | string | required | HTTPS push endpoint URL. Maximum length `2048`. |

Return value/effect:
- `200 OK` returns `{ "status": "unsubscribed" }`.
- `401 Unauthorized` when no current user exists.
- `404 Not Found` when Web Push is not configured or when the endpoint is not owned by the current user.
- `403 Forbidden` when the allow list rejects the caller.
- `422 Unprocessable Entity` when validation fails, including non-HTTPS endpoints.

```javascript
const res = await fetch("/api/notifications/unsubscribe", {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    endpoint: "https://push.example.com/sub/abc123"
  })
});
console.log(await res.json());
```

### `GET /api/users/mentions`
Return comments that mention the current user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `offset` | integer | `0` | Pagination offset. Negative values are clamped to `0`. |
| `limit` | integer | `50` | Pagination size. Values above `200` are clamped to `200`. |
| `unread_only` | boolean-like string | `false` | Truthy values: `true`, `1`, `yes`. |

Return value/effect:
- `200 OK` returns:
  - `mentions`: array of mention objects with `id`, `job_id`, `test_name`, `child_job_name`, `child_build_number`, `comment`, `username`, `created_at`, and `is_read`
  - `total`
  - `unread_count`
- `401 Unauthorized` when no current user exists.
- `403 Forbidden` when the allow list rejects the caller.
- `400 Bad Request` when `offset` or `limit` is not an integer.

```javascript
const res = await fetch("/api/users/mentions?unread_only=true&limit=20", {
  credentials: "include"
});
console.log(await res.json());
```

### `POST /api/users/mentions/read`
Mark specific mention rows as read for the current user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `comment_ids` | array<integer> | required | Non-empty list of comment IDs. Booleans are rejected. |

Return value/effect:
- `200 OK` returns `{ "ok": true }`.
- `401 Unauthorized` when no current user exists.
- `403 Forbidden` when the allow list rejects the caller.
- `400 Bad Request` when `comment_ids` is missing, empty, or contains non-integers.

```javascript
const res = await fetch("/api/users/mentions/read", {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ comment_ids: [12, 15] })
});
console.log(await res.json());
```

### `POST /api/users/mentions/read-all`
Mark every unread mention as read for the current user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns `{ "marked_read": <count> }`.
- `401 Unauthorized` when no current user exists.
- `403 Forbidden` when the allow list rejects the caller.

```javascript
const res = await fetch("/api/users/mentions/read-all", {
  method: "POST",
  credentials: "include"
});
console.log(await res.json());
```

### `GET /api/users/mentions/unread-count`
Return the unread-mention badge count for the current user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns `{ "count": <int> }`.
- `401 Unauthorized` when no current user exists.
- `403 Forbidden` when the allow list rejects the caller.

```javascript
const res = await fetch("/api/users/mentions/unread-count", {
  credentials: "include"
});
console.log(await res.json());
```

### `GET /api/users/mentionable`
Return the list of usernames that can be mentioned.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns `{ "usernames": ["alice", "bob", ...] }`.
- `401 Unauthorized` when no current user exists.
- `403 Forbidden` when the allow list rejects the caller.

```javascript
const res = await fetch("/api/users/mentionable", {
  credentials: "include"
});
console.log(await res.json());
```

## Metadata
> **Note:** For end-user metadata workflows and examples, see [Organize Jobs with Metadata](organize-jobs-with-metadata.html).

### `GET /api/jobs/metadata`
List all stored job metadata, optionally filtered.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `team` | string | `""` | Exact team filter. |
| `tier` | string | `""` | Exact tier filter. |
| `version` | string | `""` | Exact version filter. |
| `label` | string (repeatable) | none | Label filter. Repeating the parameter requires all listed labels to be present. |

Return value/effect:
- `200 OK` returns an array of metadata objects with `job_name`, `team`, `tier`, `version`, and `labels`.

```bash
curl 'http://localhost:8000/api/jobs/metadata?team=platform&label=nightly&label=smoke'
```

### `GET /api/jobs/{job_name:path}/metadata`
Return one metadata object.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_name` | string | required | Jenkins job name. Path-style folder names are supported. |

Return value/effect:
- `200 OK` returns `{ "job_name": "...", "team": "...", "tier": "...", "version": "...", "labels": [...] }`.
- `404 Not Found` when no metadata exists for the job.

```bash
curl http://localhost:8000/api/jobs/folder/subfolder/my-job/metadata
```

### `PUT /api/jobs/{job_name:path}/metadata`
Create or update one metadata record.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_name` | string | required | Jenkins job name. |
| `team` | string \| null | omitted | Team value to write. |
| `tier` | string \| null | omitted | Tier value to write. |
| `version` | string \| null | omitted | Version value to write. |
| `labels` | array<string> | omitted | Label array to write. |

Return value/effect:
- `200 OK` returns the stored metadata object.
- Omitted fields are preserved from the existing record.
- `403 Forbidden` without admin access.

```bash
curl -X PUT http://localhost:8000/api/jobs/my-job/metadata \
  -H 'Authorization: Bearer <admin-bearer-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "team": "platform",
    "tier": "critical",
    "labels": ["nightly", "smoke"]
  }'
```

### `DELETE /api/jobs/{job_name:path}/metadata`
Delete one metadata record.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_name` | string | required | Jenkins job name. |

Return value/effect:
- `200 OK` returns `{ "status": "deleted", "job_name": "..." }`.
- `404 Not Found` when no metadata exists for the job.
- `403 Forbidden` without admin access.

```bash
curl -X DELETE http://localhost:8000/api/jobs/my-job/metadata \
  -H 'Authorization: Bearer <admin-bearer-token>'
```

### `PUT /api/jobs/metadata/bulk`
Bulk upsert metadata records.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `items` | array<object> | required | Array of `1` to `1000` metadata rows. Each row has `job_name`, `team`, `tier`, `version`, and `labels`. |

Return value/effect:
- `200 OK` returns `{ "updated": <count> }`.
- `403 Forbidden` without admin access.
- `422 Unprocessable Entity` when validation fails or a row is missing `job_name`.

> **Warning:** Bulk import is a full row replace for each item. Optional fields omitted from an item are stored as `null` or `[]`, not preserved from an existing row.

```bash
curl -X PUT http://localhost:8000/api/jobs/metadata/bulk \
  -H 'Authorization: Bearer <admin-bearer-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [
      {
        "job_name": "job-a",
        "team": "alpha"
      },
      {
        "job_name": "job-b",
        "team": "beta",
        "labels": ["ci"]
      }
    ]
  }'
```

### `GET /api/jobs/metadata/rules`
Return the configured metadata-rule file name and normalized rule list.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns:
  - `rules_file`: basename of the configured rules file, or `null`
  - `rules`: normalized rule array
- Each rule can contain:
  - `pattern`
  - `team`
  - `tier`
  - `version`
  - `labels`

```bash
curl http://localhost:8000/api/jobs/metadata/rules
```

### `POST /api/jobs/metadata/rules/preview`
Preview the metadata that the current rule set would assign to one job name.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_name` | string | required | Jenkins job name to test against the loaded rules. |

Return value/effect:
- `200 OK` returns:
  - `job_name`
  - `matched`
  - `metadata`: object or `null`
- `422 Unprocessable Entity` when `job_name` is missing, blank, or not a string.

```bash
curl -X POST http://localhost:8000/api/jobs/metadata/rules/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "job_name": "team-a/nightly"
  }'
```

### `GET /api/dashboard/filtered`
Return dashboard entries with attached metadata and optional metadata filters.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `team` | string | `""` | Exact team filter. |
| `tier` | string | `""` | Exact tier filter. |
| `version` | string | `""` | Exact version filter. |
| `label` | string (repeatable) | none | Label filter. Repeating the parameter requires all listed labels to be present. |

Return value/effect:
- `200 OK` returns the same dashboard entry shape as `GET /api/dashboard`.
- Every returned job also includes `metadata`, which is either a metadata object or `null`.
- Without filters, the endpoint returns all dashboard jobs with metadata attached.

```bash
curl 'http://localhost:8000/api/dashboard/filtered?team=platform&label=nightly'
```

## Admin
> **Note:** For admin workflows and operational guidance, see [Manage Users, Access, and Token Usage](manage-users-access-and-token-usage.html).

### `DELETE /api/results/bulk`
Delete multiple analysis jobs and their related data in one request.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_ids` | array<string> | required | Array of `1` to `500` job IDs. Duplicate IDs are de-duplicated while preserving order. |

Return value/effect:
- `200 OK` returns:
  - `deleted`: array of deleted job IDs
  - `failed`: array of `{ "job_id": "...", "reason": "..." }`
  - `total`: number of unique job IDs processed
- `403 Forbidden` without admin access.
- `422 Unprocessable Entity` for validation errors.

```bash
curl -X DELETE http://localhost:8000/api/results/bulk \
  -H 'Authorization: Bearer <admin-bearer-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "job_ids": ["job-1", "job-2"]
  }'
```

### `DELETE /results/{job_id}`
Delete one analysis job and all related data.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |

Return value/effect:
- `200 OK` returns `{ "status": "deleted", "job_id": "..." }`.
- `404 Not Found` when the job does not exist.
- `403 Forbidden` without admin access.

```bash
curl -X DELETE http://localhost:8000/results/job-123 \
  -H 'Authorization: Bearer <admin-bearer-token>'
```

### `GET /api/admin/token-usage`
Return aggregated AI token-usage totals with optional filters and grouping.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `start_date` | string | none | Lower bound for `created_at`. |
| `end_date` | string | none | Upper bound for `created_at`. A date-only value such as `2026-04-27` is expanded to the end of that day. |
| `ai_provider` | string | none | Exact provider filter. |
| `ai_model` | string | none | Exact model filter. |
| `call_type` | string | none | Exact call-type filter. |
| `group_by` | string | none | One of `provider`, `model`, `call_type`, `day`, `week`, `month`, or `job`. |

Return value/effect:
- `200 OK` returns:
  - `total_input_tokens`
  - `total_output_tokens`
  - `total_cache_read_tokens`
  - `total_cache_write_tokens`
  - `total_cost_usd`
  - `total_calls`
  - `total_duration_ms`
  - `breakdown`
- `breakdown` is empty when `group_by` is omitted.
- When present, each breakdown row includes `group_key`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `call_count`, and `avg_duration_ms`.
- `403 Forbidden` without admin access.
- `422 Unprocessable Entity` when `group_by` is not one of the supported values.

```bash
curl 'http://localhost:8000/api/admin/token-usage?group_by=provider&start_date=2026-04-01&end_date=2026-04-30' \
  -H 'Authorization: Bearer <admin-bearer-token>'
```

### `GET /api/admin/token-usage/summary`
Return dashboard-oriented token-usage summaries.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns:
  - `today`
  - `this_week`
  - `this_month`
  - `top_models`
  - `top_jobs`
- Period objects include `calls`, `tokens`, `input_tokens`, `output_tokens`, and `cost_usd`.
- `top_models` rows include `model`, `calls`, and `cost_usd`.
- `top_jobs` rows include `job_id`, `calls`, and `cost_usd`.
- `403 Forbidden` without admin access.

```bash
curl http://localhost:8000/api/admin/token-usage/summary \
  -H 'Authorization: Bearer <admin-bearer-token>'
```

### `GET /api/admin/token-usage/{job_id}`
Return raw token-usage records for one analysis job.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_id` | string | required | Analysis job ID. |

Return value/effect:
- `200 OK` returns:
  - `job_id`
  - `records`: array of rows with `id`, `job_id`, `created_at`, `ai_provider`, `ai_model`, `call_type`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `total_tokens`, `cost_usd`, `duration_ms`, `prompt_chars`, and `response_chars`
- `404 Not Found` when no token-usage rows exist for the job.
- `403 Forbidden` without admin access.

```bash
curl http://localhost:8000/api/admin/token-usage/job-123 \
  -H 'Authorization: Bearer <admin-bearer-token>'
```

### `POST /api/admin/users`
Create a new admin user and return its generated API key.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `username` | string | required | New admin username. Must be 2 to 50 characters, start with an alphanumeric character, and only contain letters, digits, `.`, `_`, or `-`. |

Return value/effect:
- `200 OK` returns `{ "username": "...", "api_key": "...", "role": "admin" }`.
- `400 Bad Request` when the username is invalid, already taken, or reserved.
- `403 Forbidden` without admin access.

```bash
curl -X POST http://localhost:8000/api/admin/users \
  -H 'Authorization: Bearer <admin-bearer-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "username": "newadmin"
  }'
```

### `GET /api/admin/users`
List tracked users.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| None | n/a | n/a | No request parameters. |

Return value/effect:
- `200 OK` returns `{ "users": [...] }`.
- Each user row includes `id`, `username`, `role`, `created_at`, and `last_seen`.
- `403 Forbidden` without admin access.

```bash
curl http://localhost:8000/api/admin/users \
  -H 'Authorization: Bearer <admin-bearer-token>'
```

### `DELETE /api/admin/users/{username}`
Delete one admin user.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `username` | string | required | Admin username to delete. |

Return value/effect:
- `200 OK` returns `{ "deleted": "<username>" }`.
- `400 Bad Request` when the request would delete the caller's own account or the last admin user.
- `404 Not Found` when the named admin user does not exist.
- `403 Forbidden` without admin access.

```bash
curl -X DELETE http://localhost:8000/api/admin/users/oldadmin \
  -H 'Authorization: Bearer <admin-bearer-token>'
```

### `PUT /api/admin/users/{username}/role`
Change one tracked user's role.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `username` | string | required | Username to update. |
| `role` | string | required | Target role: `admin` or `user`. |

Return value/effect:
- `200 OK` returns `{ "username": "...", "role": "..." }`.
- When promoting to `admin`, the response also includes `api_key`.
- Demoting an admin to `user` removes their admin key and invalidates their active sessions.
- `400 Bad Request` when the caller targets themself, requests the current role, uses an invalid role, targets the reserved `admin` user, or attempts to demote the last admin.
- `404 Not Found` when the user does not exist.
- `403 Forbidden` without admin access.

```bash
curl -X PUT http://localhost:8000/api/admin/users/alice/role \
  -H 'Authorization: Bearer <admin-bearer-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "role": "admin"
  }'
```

### `POST /api/admin/users/{username}/rotate-key`
Rotate or set an admin user's API key.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `username` | string | required | Admin username whose key will be rotated. |
| `new_key` | string \| null | auto-generate | Optional replacement key. Must be at least 16 characters when supplied. |

Return value/effect:
- `200 OK` returns `{ "username": "...", "new_api_key": "..." }`.
- Existing sessions for that admin user are invalidated.
- `400 Bad Request` for invalid JSON or invalid key input.
- `404 Not Found` when the admin user does not exist.
- `403 Forbidden` without admin access.

```bash
curl -X POST http://localhost:8000/api/admin/users/alice/rotate-key \
  -H 'Authorization: Bearer <admin-bearer-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "new_key": "a-very-long-custom-admin-key"
  }'
```

## Related Pages

- [CLI Command Reference](cli-command-reference.html)
- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Investigate Failure History](investigate-failure-history.html)
- [Manage Users, Access, and Token Usage](manage-users-access-and-token-usage.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)