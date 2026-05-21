# API Endpoint Reference

> **Note:** Response `result_url` values are relative unless the server sets `PUBLIC_BASE_URL`.


> **Tip:** Examples that use `-b cookies.txt -c cookies.txt` assume you already stored cookies from `POST /api/auth/login`.


> **Note:** Mutating endpoints can return `403` when the current requester is blocked by the server allow list.

## Analysis

### Shared analysis body fields
Shared JSON fields accepted by analysis endpoints. `POST /analyze` adds Jenkins-specific fields. `POST /analyze-failures` adds direct-failure fields. `POST /re-analyze/{job_id}` accepts only these shared fields.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `tests_repo_url` | `string \| null` | `null` | Tests repository URL. A `:ref` suffix selects a branch or tag. |
| `tests_repo_token` | `string \| null` | `null` | Token for cloning a private tests repository. |
| `ai_provider` | `string \| null` | `null` | AI provider override. Allowed values: `claude`, `gemini`, `cursor`. |
| `ai_model` | `string \| null` | `null` | AI model override. |
| `enable_jira` | `boolean \| null` | `null` | Enables or disables Jira duplicate matching for this request. |
| `ai_cli_timeout` | `integer \| null` | `null` | AI CLI timeout in minutes. |
| `max_concurrent_ai_calls` | `integer \| null` | `null` | Maximum concurrent AI CLI calls. |
| `jira_url` | `string \| null` | `null` | Jira base URL override. |
| `jira_email` | `string \| null` | `null` | Jira Cloud email override. |
| `jira_api_token` | `string \| null` | `null` | Jira Cloud API token override. |
| `jira_pat` | `string \| null` | `null` | Jira Server/Data Center personal access token override. |
| `jira_project_key` | `string \| null` | `null` | Jira project key override. |
| `jira_ssl_verify` | `boolean \| null` | `null` | Jira SSL verification override. |
| `jira_max_results` | `integer \| null` | `null` | Maximum Jira matches to fetch. |
| `raw_prompt` | `string \| null` | `null` | Extra AI instructions appended to the request. |
| `github_token` | `string \| null` | `null` | GitHub token override used for private-repo lookups and comment enrichment. |
| `peer_ai_configs` | `array<object> \| null` | `null` | Peer-analysis model list. Omit to inherit the server default. Send `[]` to disable peer analysis for this request. |
| `peer_analysis_max_rounds` | `integer` | `3` | Maximum debate rounds for peer analysis. |
| `additional_repos` | `array<object> \| null` | `null` | Extra repositories cloned into the AI workspace. Omit to inherit the server default. Send `[]` to disable. |

**`peer_ai_configs[]` fields**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `ai_provider` | `string` | `Required` | Peer AI provider. Allowed values: `claude`, `gemini`, `cursor`. |
| `ai_model` | `string` | `Required` | Peer AI model identifier. |

**`additional_repos[]` fields**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `string` | `Required` | Repository label and clone directory name. |
| `url` | `string` | `Required` | Repository URL to clone. |
| `ref` | `string` | `""` | Branch or tag to check out. |
| `token` | `string \| null` | `null` | Token for cloning a private repository. |

**Example**

```json
{
  "tests_repo_url": "https://github.com/example/tests:main",
  "ai_provider": "claude",
  "ai_model": "claude-sonnet-4-20250514",
  "enable_jira": true,
  "ai_cli_timeout": 15,
  "max_concurrent_ai_calls": 4,
  "peer_ai_configs": [
    {
      "ai_provider": "gemini",
      "ai_model": "gemini-2.5-pro"
    }
  ],
  "additional_repos": [
    {
      "name": "product",
      "url": "https://github.com/example/product",
      "ref": "release-4.18"
    }
  ]
}
```

**Return value/effect**

Supplies per-request analysis overrides. Omitted fields use the server defaults.

### POST /analyze
Queues a Jenkins build for analysis and returns a new job ID immediately.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_name` | body | `string` | `Required` | Jenkins job name. Folder-style names such as `folder/job-name` are accepted. |
| `build_number` | body | `integer` | `Required` | Jenkins build number to analyze. |
| `force` | body | `boolean` | `false` | Analyze even when the Jenkins build result is `SUCCESS`. |
| `wait_for_completion` | body | `boolean` | `true` | Wait for the Jenkins build to finish before analysis starts. |
| `poll_interval_minutes` | body | `integer` | `2` | Delay between Jenkins status polls while waiting. |
| `max_wait_minutes` | body | `integer` | `0` | Maximum wait time in minutes. `0` means no limit. |
| `jenkins_url` | body | `string \| null` | `null` | Jenkins base URL override. |
| `jenkins_user` | body | `string \| null` | `null` | Jenkins username override. |
| `jenkins_password` | body | `string \| null` | `null` | Jenkins password or API token override. |
| `jenkins_ssl_verify` | body | `boolean \| null` | `null` | Jenkins SSL verification override. |
| `jenkins_timeout` | body | `integer \| null` | `null` | Jenkins API timeout in seconds. |
| `jenkins_artifacts_max_size_mb` | body | `integer \| null` | `null` | Maximum artifact download size in MB. |
| `get_job_artifacts` | body | `boolean \| null` | `null` | Downloads Jenkins build artifacts for AI context. |
| Shared analysis body fields | body | `object` | optional | Also accepts all fields from **Shared analysis body fields** above. |

**Return value**

Returns `202 Accepted`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `queued`. |
| `job_id` | `string` | New analysis job ID. |
| `message` | `string` | Polling hint for the queued job. |
| `base_url` | `string` | Empty string or the configured public base URL. |
| `result_url` | `string` | Relative or absolute URL for `GET /results/{job_id}`. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/analyze" \
  -H "Content-Type: application/json" \
  -d '{
    "job_name": "folder/nightly-e2e",
    "build_number": 4281,
    "ai_provider": "claude",
    "ai_model": "claude-sonnet-4-20250514",
    "tests_repo_url": "https://github.com/example/tests:release-4.18",
    "wait_for_completion": true,
    "poll_interval_minutes": 2
  }'
```

### POST /analyze-failures
Analyzes raw failure data or raw JUnit XML without Jenkins.

> **Warning:** Send exactly one of `failures` or `raw_xml`. Sending both, or neither, returns `422`.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `failures` | body | `array<object> \| null` | `null` | Raw failures to analyze directly. |
| `raw_xml` | body | `string \| null` | `null` | Raw JUnit XML. Maximum size: 50 MB. |
| Shared analysis body fields | body | `object` | optional | Also accepts all fields from **Shared analysis body fields** above. |

**`failures[]` fields**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `test_name` | `string` | `Required` | Fully qualified failing test name. |
| `error_message` | `string` | `""` | Failure message. |
| `stack_trace` | `string` | `""` | Full stack trace. |
| `duration` | `number` | `0.0` | Test duration in seconds. |
| `status` | `string` | `FAILED` | Source failure status. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `job_id` | `string` | New analysis job ID. |
| `status` | `string` | `completed` or `failed`. |
| `summary` | `string` | Human-readable analysis summary. |
| `ai_provider` | `string` | Effective AI provider. |
| `ai_model` | `string` | Effective AI model. |
| `failures` | `array<object>` | Structured failure analyses. |
| `enriched_xml` | `string \| null` | Enriched JUnit XML. Present only when the request used `raw_xml`. |
| `token_usage` | `object \| null` | Aggregated AI token usage totals and per-call records. |
| `base_url` | `string` | Empty string or the configured public base URL. |
| `result_url` | `string` | Relative or absolute URL for the stored result. |

**`failures[]` response fields**

| Field | Type | Description |
| --- | --- | --- |
| `test_name` | `string` | Failed test name. |
| `error` | `string` | Error message stored for the analyzed failure. |
| `error_signature` | `string` | SHA-256 signature of the error and stack trace. |
| `analysis.classification` | `string` | Root cause classification such as `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE`. |
| `analysis.affected_tests` | `array<string>` | Other tests grouped with the same finding. |
| `analysis.details` | `string` | Main analysis text. |
| `analysis.artifacts_evidence` | `string` | Verbatim artifact evidence captured during analysis. |
| `analysis.code_fix` | `object \| absent` | Code-fix payload with file, line, change, optional full-file snapshots, search keywords, and matched issues. |
| `analysis.product_bug_report` | `object \| absent` | Product-bug payload with title, severity, component, description, evidence, Jira keywords, and Jira matches. |
| `peer_debate` | `object \| null` | Peer-analysis consensus trail when peer review is enabled. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/analyze-failures" \
  -H "Content-Type: application/json" \
  -d '{
    "failures": [
      {
        "test_name": "tests.auth.test_login.TestLogin.test_valid_credentials",
        "error_message": "AssertionError: expected 200, got 500",
        "stack_trace": "Traceback (most recent call last): ..."
      }
    ],
    "ai_provider": "claude",
    "ai_model": "claude-sonnet-4-20250514"
  }'
```

### POST /re-analyze/{job_id}
Queues a new analysis by reconstructing the original stored request and applying fresh overrides.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Existing analysis job ID to resume from. |
| body | body | `object` | optional | Accepts the **Shared analysis body fields** above. Jenkins job selection fields are taken from the stored request. |

**Return value**

Returns `202 Accepted`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `queued`. |
| `job_id` | `string` | New analysis job ID. |
| `message` | `string` | Polling hint for the queued job. |
| `base_url` | `string` | Empty string or the configured public base URL. |
| `result_url` | `string` | Relative or absolute URL for the new result. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/re-analyze/old-job-id-123" \
  -H "Content-Type: application/json" \
  -d '{
    "ai_provider": "cursor",
    "ai_model": "gpt-5.1",
    "peer_ai_configs": []
  }'
```

## Results

> **Note:** `GET /results/{job_id}` returns `202 Accepted` while a queued or running job is still in progress.


> **Warning:** Failure-targeted result subresources can return `202` while the job is still pending and `409` if the stored job failed.

### GET /results/{job_id}
Returns the stored result wrapper for an analysis job.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |

**Return value**

Returns `200 OK` for completed or failed jobs, and `202 Accepted` for in-progress jobs.

| Field | Type | Description |
| --- | --- | --- |
| `job_id` | `string` | Analysis job ID. |
| `jenkins_url` | `string` | Stored Jenkins build URL. |
| `status` | `string` | Job status: `pending`, `waiting`, `running`, `completed`, or `failed`. |
| `result` | `object \| null` | Stored analysis payload. Partial while the job is still running. |
| `created_at` | `string` | Creation timestamp. |
| `analysis_started_at` | `string \| null` | Timestamp when analysis execution started. |
| `completed_at` | `string \| null` | Completion timestamp. |
| `capabilities` | `object` | Server feature flags attached to the response. |
| `base_url` | `string` | Empty string or the configured public base URL. |
| `result_url` | `string` | Relative or absolute URL for this result. |

**Common fields inside `result`**

| Field | Type | Description |
| --- | --- | --- |
| `job_name` | `string` | Jenkins job name when the result came from `POST /analyze`. |
| `build_number` | `integer` | Jenkins build number when the result came from `POST /analyze`. |
| `status` | `string` | Stored result status when the completed payload includes it. |
| `summary` | `string` | Human-readable summary. |
| `ai_provider` | `string` | Effective AI provider. |
| `ai_model` | `string` | Effective AI model. |
| `failures` | `array<object>` | Failure analyses using the same structure returned by `POST /analyze-failures`. |
| `child_job_analyses` | `array<object>` | Recursive child-job analyses with `job_name`, `build_number`, `failures`, `failed_children`, and optional `note`. |
| `token_usage` | `object \| null` | Aggregated AI token usage totals and per-call records. |
| `request_params` | `object` | Saved request parameters with sensitive credential fields stripped from the response. |
| `progress_phase` | `string` | Current phase while a job is running. |
| `progress_log` | `array<object>` | Phase history with `phase` and `timestamp` entries. |
| `enriched_xml` | `string \| null` | Enriched JUnit XML for direct XML analysis results. |

**`capabilities` fields**

| Field | Type | Description |
| --- | --- | --- |
| `github_issues_enabled` | `boolean` | GitHub issue creation is available. |
| `jira_issues_enabled` | `boolean` | Jira issue creation is available. |
| `server_github_token` | `boolean` | The server has its own GitHub token configured. |
| `server_jira_token` | `boolean` | The server has Jira credentials configured. |
| `server_jira_email` | `boolean` | The server has a Jira Cloud email configured. |
| `server_jira_project_key` | `string` | Server Jira project key, or `""`. |
| `reportportal` | `boolean` | Report Portal integration is available. |
| `reportportal_project` | `string` | Configured Report Portal project, or `""`. |
| `feedback_enabled` | `boolean` | Feedback preview/create endpoints are available. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/results/7b5f0f61-0b1d-4d63-8d7a-4b2d6e5a8d7d"
```

### GET /results
Lists recent analysis jobs.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `limit` | query | `integer` | `50` | Maximum results to return. Maximum value: `100`. |

**Return value**

Returns `200 OK` and a JSON array.

| Field | Type | Description |
| --- | --- | --- |
| `job_id` | `string` | Analysis job ID. |
| `jenkins_url` | `string` | Stored Jenkins build URL. |
| `status` | `string` | Job status. |
| `created_at` | `string` | Creation timestamp. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/results?limit=10"
```

### GET /results/{job_id}/review-status
Returns a compact review summary for one stored result.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `total_failures` | `integer` | Total failures across the result, including child jobs. |
| `reviewed_count` | `integer` | Number of failures marked reviewed. |
| `comment_count` | `integer` | Number of comments stored for the job. |

Missing jobs return zero counts.

**Example**

```bash
curl -sS "https://rootcoz.example.com/results/7b5f0f61-0b1d-4d63-8d7a-4b2d6e5a8d7d/review-status"
```

### PUT /results/{job_id}/override-classification
Overrides the stored classification for a failure group.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `test_name` | body | `string` | `Required` | Representative test name from the failure group. |
| `classification` | body | `string` | `Required` | New classification. Allowed values: `CODE ISSUE`, `PRODUCT BUG`, `INFRASTRUCTURE`. |
| `child_job_name` | body | `string` | `""` | Child job name for pipeline failure overrides. |
| `child_build_number` | body | `integer` | `0` | Child build number for pipeline failure overrides. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `ok` on success. |
| `classification` | `string` | Applied classification. |

**Example**

```bash
curl -sS -X PUT "https://rootcoz.example.com/results/job-123/override-classification" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "tests.auth.test_login.TestLogin.test_valid_credentials",
    "classification": "PRODUCT BUG"
  }'
```

### DELETE /results/{job_id}
Deletes one stored result and all related rows.

> **Warning:** Admin-only endpoint.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID to delete. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `deleted` on success. |
| `job_id` | `string` | Deleted job ID. |

**Example**

```bash
curl -sS -X DELETE "https://rootcoz.example.com/results/job-123" \
  -H "Authorization: Bearer <admin-api-key>"
```

### DELETE /api/results/bulk
Deletes multiple stored results in one request.

> **Warning:** Admin-only endpoint.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_ids` | body | `array<string>` | `Required` | Job IDs to delete. Minimum `1`, maximum `500`. Duplicate IDs are ignored after the first occurrence. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `deleted` | `array<string>` | Job IDs deleted successfully. |
| `failed` | `array<object>` | Failed deletions. Each item has `job_id` and `reason`. |
| `total` | `integer` | Number of unique requested job IDs processed. |

**Example**

```bash
curl -sS -X DELETE "https://rootcoz.example.com/api/results/bulk" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "job_ids": ["job-123", "job-456", "job-789"]
  }'
```

### GET /results/{job_id}/issue-prompt
Fetches the stored issue-generation prompt file from the analyzed tests repository.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `prompt` | `string` | Prompt file contents, or `""` when the job has no tests repository, the file is missing, the job does not exist, or the fetch fails. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/results/job-123/issue-prompt"
```

### POST /results/{job_id}/preview-github-issue
Generates a GitHub issue preview from one analyzed failure.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `test_name` | body | `string` | `Required` | Failure to preview. |
| `child_job_name` | body | `string` | `""` | Child job name for pipeline failure previews. |
| `child_build_number` | body | `integer` | `0` | Child build number for pipeline failure previews. |
| `github_token` | body | `string` | `""` | GitHub token used for duplicate searching. Falls back to the server token when available. |
| `github_repo_url` | body | `string` | `""` | Overrides the GitHub repository used for duplicate searching. |
| `include_links` | body | `boolean` | `false` | Includes report and Jenkins links in generated content when the server can build public URLs. |
| `ai_provider` | body | `string` | `""` | AI provider override for preview generation. |
| `ai_model` | body | `string` | `""` | AI model override for preview generation. |
| `issue_prompt` | body | `string` | `""` | Extra instructions appended to issue generation. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `title` | `string` | Generated issue title. |
| `body` | `string` | Generated issue body. |
| `similar_issues` | `array<object>` | Duplicate-search results. Items can include `number`, `key`, `title`, `url`, and `status`. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/results/job-123/preview-github-issue" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "tests.auth.test_login.TestLogin.test_valid_credentials",
    "ai_provider": "claude",
    "ai_model": "claude-sonnet-4-20250514",
    "include_links": true,
    "issue_prompt": "Include the affected product version."
  }'
```

### POST /results/{job_id}/preview-jira-bug
Generates a Jira bug preview from one analyzed failure.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `test_name` | body | `string` | `Required` | Failure to preview. |
| `child_job_name` | body | `string` | `""` | Child job name for pipeline failure previews. |
| `child_build_number` | body | `integer` | `0` | Child build number for pipeline failure previews. |
| `jira_token` | body | `string` | `""` | Jira token used for duplicate searching. Falls back to the server credentials when available. |
| `jira_email` | body | `string` | `""` | Jira Cloud email used with `jira_token`. |
| `jira_project_key` | body | `string` | `""` | Jira project key override used for duplicate searching. |
| `include_links` | body | `boolean` | `false` | Includes report and Jenkins links in generated content when the server can build public URLs. |
| `ai_provider` | body | `string` | `""` | AI provider override for preview generation. |
| `ai_model` | body | `string` | `""` | AI model override for preview generation. |
| `issue_prompt` | body | `string` | `""` | Extra instructions appended to issue generation. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `title` | `string` | Generated Jira title. |
| `body` | `string` | Generated Jira body. |
| `similar_issues` | `array<object>` | Duplicate-search results. Items can include `number`, `key`, `title`, `url`, and `status`. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/results/job-123/preview-jira-bug" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "tests.network.TestDNS.test_lookup",
    "jira_project_key": "OCPBUGS",
    "ai_provider": "claude",
    "ai_model": "claude-sonnet-4-20250514",
    "issue_prompt": "Include the cluster version and component."
  }'
```

### POST /results/{job_id}/create-github-issue
Creates a GitHub issue from one analyzed failure and stores a tracker comment on success.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `test_name` | body | `string` | `Required` | Failure to create the issue for. |
| `child_job_name` | body | `string` | `""` | Child job name for pipeline failures. |
| `child_build_number` | body | `integer` | `0` | Child build number for pipeline failures. |
| `github_token` | body | `string` | `""` | Personal GitHub token. Falls back to the server token when available. |
| `github_repo_url` | body | `string` | `""` | Overrides the GitHub repository used for issue creation. |
| `title` | body | `string` | `Required` | Issue title. |
| `body` | body | `string` | `Required` | Issue body. |

**Return value**

Returns `201 Created`.

| Field | Type | Description |
| --- | --- | --- |
| `url` | `string` | URL of the created GitHub issue. |
| `number` | `integer` | GitHub issue number. |
| `key` | `string` | Always `""` for GitHub issues. |
| `title` | `string` | Created issue title. |
| `comment_id` | `integer` | ID of the auto-created comment linking the issue back to the result. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/results/job-123/create-github-issue" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "tests.auth.test_login.TestLogin.test_valid_credentials",
    "github_token": "<github-token>",
    "title": "Login test fails with HTTP 500",
    "body": "## Summary\n\nThe login endpoint returns HTTP 500 in nightly CI."
  }'
```

### POST /results/{job_id}/create-jira-bug
Creates a Jira issue from one analyzed failure and stores a tracker comment on success.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `test_name` | body | `string` | `Required` | Failure to create the bug for. |
| `child_job_name` | body | `string` | `""` | Child job name for pipeline failures. |
| `child_build_number` | body | `integer` | `0` | Child build number for pipeline failures. |
| `jira_token` | body | `string` | `""` | Jira token. Falls back to the server credentials when available. |
| `jira_email` | body | `string` | `""` | Jira Cloud email used with `jira_token`. |
| `jira_project_key` | body | `string` | `""` | Jira project key override. |
| `jira_security_level` | body | `string` | `""` | Jira security level name. |
| `title` | body | `string` | `Required` | Jira issue title. |
| `body` | body | `string` | `Required` | Jira issue body. |
| `jira_issue_type` | body | `string` | `Bug` | Jira issue type name. |

**Return value**

Returns `201 Created`.

| Field | Type | Description |
| --- | --- | --- |
| `url` | `string` | URL of the created Jira issue. |
| `key` | `string` | Jira issue key. |
| `title` | `string` | Created issue title. |
| `comment_id` | `integer` | ID of the auto-created comment linking the issue back to the result. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/results/job-123/create-jira-bug" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "tests.network.TestDNS.test_lookup",
    "jira_token": "<jira-token>",
    "jira_email": "user@example.com",
    "jira_project_key": "OCPBUGS",
    "jira_security_level": "Internal",
    "jira_issue_type": "Bug",
    "title": "DNS lookup fails in nightly CI",
    "body": "h2. Summary\n\nDNS resolution fails during the network suite."
  }'
```

### POST /results/{job_id}/push-reportportal
Pushes stored classifications into Report Portal.

> **Warning:** Requires Report Portal server settings and `PUBLIC_BASE_URL`.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `child_job_name` | query | `string \| null` | `null` | Child job name for a pipeline child push. |
| `child_build_number` | query | `integer \| null` | `null` | Child build number for a pipeline child push. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `pushed` | `integer` | Number of Report Portal items updated successfully. |
| `unmatched` | `array<string>` | Report Portal items that could not be matched or classified. |
| `errors` | `array<string>` | Push-time errors. |
| `launch_id` | `integer \| null` | Matched Report Portal launch ID. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/results/job-123/push-reportportal"
```

## Comments and Review

### GET /results/{job_id}/comments
Returns all comments and review states for one job.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `comments` | `array<object>` | Stored comments for the job. |
| `reviews` | `object` | Review map keyed by test name for top-level failures, or `child_job_name#child_build_number::test_name` for child jobs. |

**`comments[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | `integer` | Comment ID. |
| `job_id` | `string` | Analysis job ID. |
| `test_name` | `string` | Failure name. |
| `child_job_name` | `string` | Child job name, or `""`. |
| `child_build_number` | `integer` | Child build number, or `0`. |
| `comment` | `string` | Comment text. |
| `error_signature` | `string` | Stored error signature for the failure group. |
| `username` | `string` | Comment author. |
| `created_at` | `string` | Creation timestamp. |

**`reviews` entry fields**

| Field | Type | Description |
| --- | --- | --- |
| `reviewed` | `boolean` | Current review state. |
| `username` | `string` | User who last changed the review state. |
| `updated_at` | `string` | Last update timestamp. |

Missing jobs return empty collections.

**Example**

```bash
curl -sS "https://rootcoz.example.com/results/job-123/comments"
```

### POST /results/{job_id}/comments
Adds a comment to one stored failure.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `test_name` | body | `string` | `Required` | Failure to comment on. |
| `comment` | body | `string` | `Required` | Comment text. |
| `child_job_name` | body | `string` | `""` | Child job name for pipeline failures. |
| `child_build_number` | body | `integer` | `0` | Child build number for pipeline failures. |

**Return value**

Returns `201 Created`.

| Field | Type | Description |
| --- | --- | --- |
| `id` | `integer` | New comment ID. |

If the comment contains `@mentions` and Web Push is configured, the server also attempts mention notifications.

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/results/job-123/comments" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "tests.auth.test_login.TestLogin.test_valid_credentials",
    "comment": "Filed a bug and linked the failing logs."
  }'
```

### DELETE /results/{job_id}/comments/{comment_id}
Deletes one stored comment.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `comment_id` | path | `integer` | `Required` | Comment ID. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `deleted` on success. |

Admin users can delete any comment. Non-admin deletion is owner-scoped.

**Example**

```bash
curl -sS -X DELETE "https://rootcoz.example.com/results/job-123/comments/17" \
  -b cookies.txt -c cookies.txt
```

### PUT /results/{job_id}/reviewed
Marks or unmarks a failure as reviewed.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |
| `test_name` | body | `string` | `Required` | Failure to update. |
| `reviewed` | body | `boolean` | `Required` | New review state. |
| `child_job_name` | body | `string` | `""` | Child job name for pipeline failures. |
| `child_build_number` | body | `integer` | `0` | Child build number for pipeline failures. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `ok` on success. |
| `reviewed_by` | `string` | Username recorded for this change, or `""`. |

**Example**

```bash
curl -sS -X PUT "https://rootcoz.example.com/results/job-123/reviewed" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "tests.auth.test_login.TestLogin.test_valid_credentials",
    "reviewed": true
  }'
```

### POST /results/{job_id}/enrich-comments
Scans stored comments for GitHub and Jira references and returns their current statuses.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `enrichments` | `object` | Map keyed by comment ID string. Each value is an array of enrichment records. |

**Enrichment record fields**

| Field | Type | Description |
| --- | --- | --- |
| `type` | `string` | One of `github_pr`, `github_issue`, or `jira`. |
| `key` | `string` | Tracker identifier such as `owner/repo#123` or `OCPBUGS-12345`. |
| `status` | `string` | Current tracker status. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/results/job-123/enrich-comments"
```

### POST /api/analyze-comment-intent
Uses AI to decide whether a comment suggests that a failure has already been reviewed or resolved.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `comment` | body | `string` | `Required` | Comment text to analyze. |
| `job_id` | body | `string` | `""` | Optional job ID used to recover the original AI provider/model when the request body omits them. |
| `ai_provider` | body | `string \| null` | `null` | AI provider override. |
| `ai_model` | body | `string \| null` | `null` | AI model override. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `suggests_reviewed` | `boolean` | `true` when the comment implies reviewed or resolved status. |
| `reason` | `string` | Short explanation from the AI, or `""`. |

If the AI call fails or returns invalid JSON, the endpoint returns `suggests_reviewed: false`.

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/analyze-comment-intent" \
  -H "Content-Type: application/json" \
  -d '{
    "comment": "Filed OCPBUGS-12345 for this failure.",
    "job_id": "job-123"
  }'
```

## History

> **Note:** `GET /history/test/{test_name}` and `GET /history/stats/{job_name}` return `200 OK` with zeroed values when no matching history exists.

### GET /history/failures
Returns paginated failure history across analyzed jobs.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `search` | query | `string` | `""` | Free-text search across test name, error message, and job name. |
| `job_name` | query | `string` | `""` | Exact job-name filter. |
| `classification` | query | `string` | `""` | Exact classification filter. |
| `limit` | query | `integer` | `50` | Maximum rows to return. Maximum value: `200`. |
| `offset` | query | `integer` | `0` | Pagination offset. Minimum value: `0`. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `failures` | `array<object>` | Matching failure-history rows. |
| `total` | `integer` | Total matching row count before pagination. |

**`failures[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | `integer` | Failure-history row ID. |
| `job_id` | `string` | Analysis job ID. |
| `job_name` | `string` | Jenkins job name. |
| `build_number` | `integer` | Jenkins build number. |
| `test_name` | `string` | Failed test name. |
| `error_message` | `string` | Stored failure message. |
| `error_signature` | `string` | Error signature hash. |
| `classification` | `string` | Stored classification value. |
| `child_job_name` | `string` | Child job name, or `""`. |
| `child_build_number` | `integer` | Child build number, or `0`. |
| `analyzed_at` | `string` | Analysis timestamp. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/history/failures?job_name=nightly-e2e&classification=PRODUCT%20BUG&limit=25&offset=0"
```

### GET /history/test/{test_name:path}
Returns historical data for one test.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `test_name` | path | `string` | `Required` | Test name to inspect. |
| `limit` | query | `integer` | `20` | Maximum recent failure rows to return. Maximum value: `100`. |
| `job_name` | query | `string` | `""` | Exact job-name filter. |
| `exclude_job_id` | query | `string` | `""` | Excludes one analysis job from the response. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `test_name` | `string` | Requested test name. |
| `total_runs` | `integer` | Total analyzed runs used for the result. |
| `failures` | `integer` | Recorded failure count. |
| `passes` | `integer \| null` | Estimated pass count. `null` when the endpoint cannot infer pass totals. |
| `failure_rate` | `number \| null` | Failure rate. `null` when the endpoint cannot infer pass totals. |
| `first_seen` | `string \| null` | Oldest recorded failure timestamp. |
| `last_seen` | `string \| null` | Most recent recorded failure timestamp. |
| `last_classification` | `string` | Most recent classification seen for the test. |
| `classifications` | `object` | Classification histogram keyed by classification name. |
| `recent_runs` | `array<object>` | Recent failure rows for the test. |
| `comments` | `array<object>` | Related historical comments for the test or matching signature. |
| `consecutive_failures` | `integer` | Consecutive recorded failure rows. |
| `note` | `string` | Availability note for the calculated pass/fail fields. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/history/test/tests.network.TestDNS.test_lookup?job_name=ocp-4.18-nightly&limit=10"
```

### GET /history/search
Finds tests that failed with the same error signature.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `signature` | query | `string` | `Required` | Error signature hash to search for. |
| `exclude_job_id` | query | `string` | `""` | Excludes one analysis job from the response. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `signature` | `string` | Requested error signature. |
| `total_occurrences` | `integer` | Matching failure-history rows. |
| `unique_tests` | `integer` | Number of distinct test names that share the signature. |
| `tests` | `array<object>` | Distinct tests with occurrence counts. |
| `last_classification` | `string` | Most recent classification recorded for this signature. |
| `comments` | `array<object>` | Historical comments tied to the same signature. |

**`tests[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `test_name` | `string` | Matching test name. |
| `occurrences` | `integer` | Failure count for this signature. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/history/search?signature=7d8e0d1c..."
```

### GET /history/stats/{job_name:path}
Returns aggregate statistics for one Jenkins job.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_name` | path | `string` | `Required` | Job name. Folder-style names are accepted. |
| `exclude_job_id` | query | `string` | `""` | Excludes one analysis job from the response. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `job_name` | `string` | Requested job name. |
| `total_builds_analyzed` | `integer` | Completed analyzed builds used for the calculation. |
| `builds_with_failures` | `integer` | Distinct analyzed builds that recorded failures. |
| `overall_failure_rate` | `number` | `builds_with_failures / total_builds_analyzed`. |
| `most_common_failures` | `array<object>` | Top failures grouped by test name and classification. |
| `recent_trend` | `string` | One of `stable`, `improving`, or `worsening`. |

**`most_common_failures[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `test_name` | `string` | Failing test name. |
| `count` | `integer` | Number of matching failure rows. |
| `classification` | `string` | Stored classification value for that grouped row. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/history/stats/folder/nightly-e2e"
```

### POST /history/classify
Creates a history classification record for one test.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `test_name` | body | `string` | `Required` | Test name to classify. |
| `classification` | body | `string` | `Required` | History classification. Allowed values: `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, `KNOWN_BUG`, `INTERMITTENT`. Matching is case-insensitive. |
| `reason` | body | `string` | `""` | Short explanation. |
| `job_name` | body | `string` | `""` | Job name context stored with the classification. |
| `references` | body | `string` | `""` | External references or tracking links. Required when `classification` is `KNOWN_BUG`. |
| `job_id` | body | `string` | `Required` | Analysis job ID. |
| `child_build_number` | body | `integer` | `0` | Child build number for child-job classifications. |

**Return value**

Returns `201 Created`.

| Field | Type | Description |
| --- | --- | --- |
| `id` | `integer` | New classification row ID. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/history/classify" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "tests.network.TestDNS.test_lookup",
    "classification": "KNOWN_BUG",
    "references": "OCPBUGS-12345",
    "job_id": "job-123"
  }'
```

### GET /history/classifications
Returns stored classification records.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `test_name` | query | `string` | `""` | Exact test-name filter. |
| `classification` | query | `string` | `""` | Exact classification filter. |
| `job_name` | query | `string` | `""` | Exact job-name filter. |
| `parent_job_name` | query | `string` | `""` | Exact parent-job filter. |
| `job_id` | query | `string` | `""` | Exact analysis job ID filter. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `classifications` | `array<object>` | Matching classification rows. |

**`classifications[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | `integer` | Classification row ID. |
| `test_name` | `string` | Test name. |
| `job_name` | `string` | Job name stored with the record. |
| `parent_job_name` | `string` | Parent pipeline job name. |
| `classification` | `string` | Stored classification value. |
| `reason` | `string` | Free-form reason. |
| `references_info` | `string` | Stored references string. |
| `created_by` | `string` | Creator username or AI marker. |
| `job_id` | `string` | Analysis job ID. |
| `child_build_number` | `integer` | Child build number. |
| `created_at` | `string` | Creation timestamp. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/history/classifications?test_name=tests.network.TestDNS.test_lookup&job_id=job-123"
```

## Auth

> **Note:** Admin-only endpoints accept either an authenticated admin session or `Authorization: Bearer <admin-api-key>`.


> **Note:** `GET /api/auth/me` always returns `200 OK`, even when no user is resolved.

### POST /api/auth/login
Authenticates a username/API-key pair and creates a session.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `username` | body | `string` | `Required` | Username to authenticate. |
| `api_key` | body | `string` | `Required` | Bootstrap admin key or a stored user API key. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `username` | `string` | Authenticated username. |
| `role` | `string` | `admin` or `user`. |
| `is_admin` | `boolean` | Admin flag. |

The response also sets session cookies.

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/auth/login" \
  -H "Content-Type: application/json" \
  -c cookies.txt \
  -d '{
    "username": "admin",
    "api_key": "<admin-api-key>"
  }'
```

### POST /api/auth/logout
Clears the active session.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `ok` | `boolean` | Always `true`. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/auth/logout" \
  -b cookies.txt -c cookies.txt
```

### GET /api/auth/me
Returns the user resolved by the server auth middleware.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `username` | `string` | Resolved username, or `""`. |
| `role` | `string` | `admin` or `user`. |
| `is_admin` | `boolean` | Admin flag. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/auth/me" \
  -H "Authorization: Bearer <admin-api-key>"
```

### GET /api/user/tokens
Returns the saved GitHub and Jira credentials for the current user.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `github_token` | `string` | Saved GitHub token, or `""`. |
| `jira_email` | `string` | Saved Jira Cloud email, or `""`. |
| `jira_token` | `string` | Saved Jira token, or `""`. |

Returns `401` when no current user is available. Returns empty strings when the resolved username has no stored user record.

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/user/tokens" \
  -b cookies.txt -c cookies.txt
```

### PUT /api/user/tokens
Updates the current user's saved tracker credentials.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `github_token` | body | `string` | `""` | GitHub token update. |
| `jira_email` | body | `string` | `""` | Jira Cloud email update. |
| `jira_token` | body | `string` | `""` | Jira token update. |

Only nonblank fields in the request body are applied. Omitted or blank fields leave existing stored values unchanged.

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `ok` | `boolean` | Always `true` on success. |

Returns `401` when no current user is available. Returns `404` when the resolved username does not have a stored user record.

**Example**

```bash
curl -sS -X PUT "https://rootcoz.example.com/api/user/tokens" \
  -H "Content-Type: application/json" \
  -b cookies.txt -c cookies.txt \
  -d '{
    "github_token": "<github-token>",
    "jira_email": "user@example.com",
    "jira_token": "<jira-token>"
  }'
```

### POST /api/validate-token
Validates a GitHub or Jira token with a live API call.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `token_type` | body | `string` | `Required` | Token type. Allowed values: `github`, `jira`. |
| `token` | body | `string` | `Required` | Token value to validate. |
| `email` | body | `string` | `""` | Jira Cloud email used with Jira token validation. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `valid` | `boolean` | Validation result. |
| `username` | `string` | Authenticated GitHub login or Jira display name, or `""`. |
| `message` | `string` | Short validation message. |

Blank tokens return `valid: false` instead of an HTTP error.

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/validate-token" \
  -H "Content-Type: application/json" \
  -d '{
    "token_type": "github",
    "token": "<github-token>"
  }'
```

### POST /api/jira-projects
Lists Jira projects available to the supplied Jira credentials.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `jira_token` | body | `string` | `""` | Jira token used for the lookup. |
| `jira_email` | body | `string` | `""` | Jira Cloud email used with `jira_token`. |
| `query` | body | `string` | `""` | Project-name search filter. |

**Return value**

Returns `200 OK` and a JSON array.

| Field | Type | Description |
| --- | --- | --- |
| `key` | `string` | Jira project key. |
| `name` | `string` | Jira project name. |

If the caller does not provide a usable Jira token, the endpoint returns the server project key when one is configured, or an empty array otherwise.

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/jira-projects" \
  -H "Content-Type: application/json" \
  -d '{
    "jira_token": "<jira-token>",
    "jira_email": "user@example.com",
    "query": "CNV"
  }'
```

### POST /api/jira-security-levels
Lists Jira security levels for one project.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `jira_token` | body | `string` | `""` | Jira token used for the lookup. |
| `jira_email` | body | `string` | `""` | Jira Cloud email used with `jira_token`. |
| `project_key` | body | `string` | `Required` | Jira project key. |

**Return value**

Returns `200 OK` and a JSON array.

| Field | Type | Description |
| --- | --- | --- |
| `id` | `string` | Jira security level ID. |
| `name` | `string` | Jira security level name. |
| `description` | `string` | Jira security level description. |

The endpoint returns an empty array when the server has no Jira URL, the request omits `project_key`, or the Jira credentials are unusable.

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/jira-security-levels" \
  -H "Content-Type: application/json" \
  -d '{
    "jira_token": "<jira-token>",
    "jira_email": "user@example.com",
    "project_key": "OCPBUGS"
  }'
```

## Admin

> **Warning:** Every endpoint in this section is admin-only.

### GET /api/admin/users
Lists tracked users.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `users` | `array<object>` | Tracked user rows. |

**`users[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | `integer` | User row ID. |
| `username` | `string` | Username. |
| `role` | `string` | `admin` or `user`. |
| `created_at` | `string` | Creation timestamp. |
| `last_seen` | `string \| null` | Most recent activity timestamp. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/admin/users" \
  -H "Authorization: Bearer <admin-api-key>"
```

### POST /api/admin/users
Creates a new admin user and returns a fresh API key.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `username` | body | `string` | `Required` | Username to create. Must be 2-50 characters, start with an alphanumeric character, and use only letters, digits, `.`, `_`, or `-`. The reserved username `admin` is rejected. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `username` | `string` | Created username. |
| `api_key` | `string` | Newly generated API key. Returned once. |
| `role` | `string` | Always `admin`. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/admin/users" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "release-admin"
  }'
```

### DELETE /api/admin/users/{username}
Deletes one admin user.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `username` | path | `string` | `Required` | Admin username to delete. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `deleted` | `string` | Deleted username. |

The endpoint rejects self-deletion and deleting the last remaining admin.

**Example**

```bash
curl -sS -X DELETE "https://rootcoz.example.com/api/admin/users/release-admin" \
  -H "Authorization: Bearer <admin-api-key>"
```

### PUT /api/admin/users/{username}/role
Changes a user's role.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `username` | path | `string` | `Required` | Username to update. |
| `role` | body | `string` | `Required` | New role. Allowed values: `admin`, `user`. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `username` | `string` | Updated username. |
| `role` | `string` | New role. |
| `api_key` | `string` | Newly generated API key. Present only when promoting a user to `admin`. |

Demotion clears the old API key and invalidates that user's sessions.

**Example**

```bash
curl -sS -X PUT "https://rootcoz.example.com/api/admin/users/alice/role" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "role": "admin"
  }'
```

### POST /api/admin/users/{username}/rotate-key
Rotates an admin user's API key.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `username` | path | `string` | `Required` | Admin username. |
| `new_key` | body | `string \| null` | `null` | Optional custom API key. Minimum length: `16`. When omitted, the server generates a new key. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `username` | `string` | Target admin username. |
| `new_api_key` | `string` | Fresh API key. Returned once. |

Key rotation invalidates existing sessions for that user.

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/admin/users/release-admin/rotate-key" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### GET /api/admin/token-usage
Returns aggregated AI token usage.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `start_date` | query | `string \| null` | `null` | Inclusive start date or timestamp filter. |
| `end_date` | query | `string \| null` | `null` | Inclusive end date or timestamp filter. Date-only values include the full day. |
| `ai_provider` | query | `string \| null` | `null` | AI provider filter. |
| `ai_model` | query | `string \| null` | `null` | AI model filter. |
| `call_type` | query | `string \| null` | `null` | Call-type filter. |
| `group_by` | query | `string \| null` | `null` | Breakdown dimension. Allowed values: `provider`, `model`, `call_type`, `day`, `week`, `month`, `job`. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `total_input_tokens` | `integer` | Summed input tokens. |
| `total_output_tokens` | `integer` | Summed output tokens. |
| `total_cache_read_tokens` | `integer` | Summed cache-read tokens. |
| `total_cache_write_tokens` | `integer` | Summed cache-write tokens. |
| `total_cost_usd` | `number` | Summed cost in USD. |
| `total_calls` | `integer` | Matching AI call count. |
| `total_duration_ms` | `integer` | Summed duration in milliseconds. |
| `breakdown` | `array<object>` | Optional grouped breakdown. Empty when `group_by` is omitted. |

**`breakdown[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `group_key` | `string` | Group label produced by `group_by`. |
| `input_tokens` | `integer` | Summed input tokens for the group. |
| `output_tokens` | `integer` | Summed output tokens for the group. |
| `cache_read_tokens` | `integer` | Summed cache-read tokens for the group. |
| `cache_write_tokens` | `integer` | Summed cache-write tokens for the group. |
| `cost_usd` | `number` | Summed cost for the group. |
| `call_count` | `integer` | AI call count for the group. |
| `avg_duration_ms` | `integer` | Average duration in milliseconds for calls with recorded durations. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/admin/token-usage?start_date=2026-05-01&end_date=2026-05-31&group_by=provider" \
  -H "Authorization: Bearer <admin-api-key>"
```

### GET /api/admin/token-usage/summary
Returns rolling token-usage summary cards.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `today` | `object` | Rolling summary for records created today. |
| `this_week` | `object` | Rolling summary for the last 7 days. |
| `this_month` | `object` | Rolling summary for the last 30 days. |
| `top_models` | `array<object>` | Highest-cost models over the last 30 days. |
| `top_jobs` | `array<object>` | Highest-cost jobs over the last 30 days. |

**Period summary fields**

| Field | Type | Description |
| --- | --- | --- |
| `calls` | `integer` | Matching AI call count. |
| `tokens` | `integer` | Total tokens. |
| `input_tokens` | `integer` | Input tokens. |
| `output_tokens` | `integer` | Output tokens. |
| `cost_usd` | `number` | Cost in USD. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/admin/token-usage/summary" \
  -H "Authorization: Bearer <admin-api-key>"
```

### GET /api/admin/token-usage/{job_id}
Returns raw token-usage records for one analysis job.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_id` | path | `string` | `Required` | Analysis job ID. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `job_id` | `string` | Requested job ID. |
| `records` | `array<object>` | Raw token-usage records for the job. |

**`records[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | `string` | Token-usage record ID. |
| `job_id` | `string` | Analysis job ID. |
| `ai_provider` | `string` | AI provider. |
| `ai_model` | `string` | AI model. |
| `call_type` | `string` | Recorded call type. |
| `input_tokens` | `integer` | Input tokens. |
| `output_tokens` | `integer` | Output tokens. |
| `cache_read_tokens` | `integer` | Cache-read tokens. |
| `cache_write_tokens` | `integer` | Cache-write tokens. |
| `total_tokens` | `integer` | Input plus output tokens. |
| `cost_usd` | `number \| null` | Estimated cost in USD. |
| `duration_ms` | `integer \| null` | Call duration in milliseconds. |
| `prompt_chars` | `integer` | Prompt character count. |
| `response_chars` | `integer` | Response character count. |
| `created_at` | `string` | Record timestamp. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/admin/token-usage/job-123" \
  -H "Authorization: Bearer <admin-api-key>"
```

## Metadata

> **Tip:** Repeat the `label` query parameter to require multiple labels, for example `?label=nightly&label=smoke`.


> **Note:** Metadata read endpoints are open. Metadata mutation endpoints are admin-only.

### GET /api/jobs/metadata
Lists stored job metadata.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `team` | query | `string` | `""` | Exact team filter. |
| `tier` | query | `string` | `""` | Exact tier filter. |
| `version` | query | `string` | `""` | Exact version filter. |
| `label` | query | `array<string>` | `[]` | Repeated label filter. All requested labels must be present. |

**Return value**

Returns `200 OK` and a JSON array.

| Field | Type | Description |
| --- | --- | --- |
| `job_name` | `string` | Jenkins job name. |
| `team` | `string \| null` | Team owner. |
| `tier` | `string \| null` | Service tier. |
| `version` | `string \| null` | Version or release label. |
| `labels` | `array<string>` | Arbitrary labels. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/jobs/metadata?team=platform&label=nightly&label=smoke"
```

### GET /api/jobs/{job_name:path}/metadata
Returns metadata for one job.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_name` | path | `string` | `Required` | Jenkins job name. Folder-style names are accepted. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `job_name` | `string` | Jenkins job name. |
| `team` | `string \| null` | Team owner. |
| `tier` | `string \| null` | Service tier. |
| `version` | `string \| null` | Version or release label. |
| `labels` | `array<string>` | Arbitrary labels. |

Returns `404` when the job has no stored metadata.

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/jobs/folder/subfolder/my-job/metadata"
```

### PUT /api/jobs/{job_name:path}/metadata
Creates or updates metadata for one job.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_name` | path | `string` | `Required` | Jenkins job name. Folder-style names are accepted. |
| `team` | body | `string \| null` | `null` | Team owner. |
| `tier` | body | `string \| null` | `null` | Service tier. |
| `version` | body | `string \| null` | `null` | Version or release label. |
| `labels` | body | `array<string>` | `[]` | Arbitrary labels. |

Omitted fields preserve their current stored values. Sending `null` for a scalar clears that field. Sending `[]` clears labels.

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `job_name` | `string` | Jenkins job name. |
| `team` | `string \| null` | Stored team owner. |
| `tier` | `string \| null` | Stored service tier. |
| `version` | `string \| null` | Stored version label. |
| `labels` | `array<string>` | Stored labels. |

**Example**

```bash
curl -sS -X PUT "https://rootcoz.example.com/api/jobs/folder/subfolder/my-job/metadata" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "team": "platform",
    "tier": "critical",
    "labels": ["nightly", "smoke"]
  }'
```

### DELETE /api/jobs/{job_name:path}/metadata
Deletes one job's metadata.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_name` | path | `string` | `Required` | Jenkins job name. Folder-style names are accepted. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `deleted` on success. |
| `job_name` | `string` | Deleted job name. |

**Example**

```bash
curl -sS -X DELETE "https://rootcoz.example.com/api/jobs/folder/subfolder/my-job/metadata" \
  -H "Authorization: Bearer <admin-api-key>"
```

### PUT /api/jobs/metadata/bulk
Bulk-imports job metadata.

> **Warning:** Bulk import replaces each submitted item completely. Omitted optional fields become `null` or `[]`.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `items` | body | `array<object>` | `Required` | Metadata rows to import. Minimum `1`, maximum `1000`. |

**`items[]` fields**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `job_name` | `string` | `Required` | Jenkins job name. |
| `team` | `string \| null` | `null` | Team owner. |
| `tier` | `string \| null` | `null` | Service tier. |
| `version` | `string \| null` | `null` | Version or release label. |
| `labels` | `array<string>` | `[]` | Arbitrary labels. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `updated` | `integer` | Number of imported metadata rows. |

**Example**

```bash
curl -sS -X PUT "https://rootcoz.example.com/api/jobs/metadata/bulk" \
  -H "Authorization: Bearer <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {
        "job_name": "job-a",
        "team": "platform",
        "labels": ["nightly"]
      },
      {
        "job_name": "job-b",
        "team": "storage",
        "tier": "critical",
        "labels": ["regression"]
      }
    ]
  }'
```

### GET /api/jobs/metadata/rules
Returns the currently loaded metadata rules.

> **Note:** When rules are evaluated, the first matching rule wins for `team`, `tier`, and `version`. `labels` accumulate across all matching rules.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `rules_file` | `string \| null` | Basename of the configured rules file, or `null`. |
| `rules` | `array<object>` | Loaded rule objects. |

**`rules[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `pattern` | `string` | Glob pattern or full regex with named groups. |
| `team` | `string \| absent` | Team value assigned by the rule. |
| `tier` | `string \| absent` | Tier value assigned by the rule. |
| `version` | `string \| absent` | Version value assigned by the rule. |
| `labels` | `array<string> \| absent` | Labels assigned by the rule. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/jobs/metadata/rules"
```

### POST /api/jobs/metadata/rules/preview
Returns the metadata that the current rules would assign to one job name.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `job_name` | body | `string` | `Required` | Jenkins job name to test against the loaded rules. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `job_name` | `string` | Echoed job name. |
| `matched` | `boolean` | `true` when at least one rule matched. |
| `metadata` | `object \| null` | Previewed metadata object, or `null`. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/jobs/metadata/rules/preview" \
  -H "Content-Type: application/json" \
  -d '{
    "job_name": "test-pytest-cnv-4.18-storage-nfs"
  }'
```

## Notifications and Mentions

> **Warning:** Web Push endpoints return `404` when push notifications are not configured and `503` when push is enabled but the VAPID keys are unavailable.

### GET /api/notifications/vapid-public-key
Returns the public VAPID key used by the browser push subscription flow.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `vapid_public_key` | `string` | Browser-visible VAPID public key. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/notifications/vapid-public-key"
```

### POST /api/notifications/subscribe
Registers a Web Push subscription for the current user.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `endpoint` | body | `string` | `Required` | Push-service endpoint URL. Must use `https://`. Maximum length: `2048`. |
| `p256dh_key` | body | `string` | `Required` | Browser public key used for payload encryption. Maximum length: `256`. |
| `auth_key` | body | `string` | `Required` | Browser authentication secret. Maximum length: `256`. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `subscribed` on success. |

The endpoint upserts the subscription and keeps only the 10 newest subscriptions for the user.

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/notifications/subscribe" \
  -H "Content-Type: application/json" \
  -b cookies.txt -c cookies.txt \
  -d '{
    "endpoint": "https://push.example.com/sub/abc123",
    "p256dh_key": "<browser-p256dh>",
    "auth_key": "<browser-auth-key>"
  }'
```

### POST /api/notifications/unsubscribe
Removes one Web Push subscription for the current user.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `endpoint` | body | `string` | `Required` | Push-service endpoint URL. Must use `https://`. Maximum length: `2048`. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `status` | `string` | Always `unsubscribed` on success. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/notifications/unsubscribe" \
  -H "Content-Type: application/json" \
  -b cookies.txt -c cookies.txt \
  -d '{
    "endpoint": "https://push.example.com/sub/abc123"
  }'
```

### GET /api/users/mentions
Returns paginated mentions for the current user.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `offset` | query | `integer` | `0` | Pagination offset. Negative values are clamped to `0`. |
| `limit` | query | `integer` | `50` | Page size. Values are clamped to the range `1-200`. |
| `unread_only` | query | `boolean` | `false` | When `true`, `1`, or `yes`, only unread mentions are returned. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `mentions` | `array<object>` | Paged mention records. |
| `total` | `integer` | Total mention count for the current query. |
| `unread_count` | `integer` | Current unread mention count for the user. |

**`mentions[]` fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | `integer` | Comment ID. |
| `job_id` | `string` | Analysis job ID. |
| `test_name` | `string` | Failure name. |
| `child_job_name` | `string` | Child job name, or `""`. |
| `child_build_number` | `integer` | Child build number, or `0`. |
| `comment` | `string` | Original comment text. |
| `username` | `string` | Comment author. |
| `created_at` | `string` | Comment timestamp. |
| `is_read` | `boolean` | Mention read state for the current user. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/users/mentions?limit=20&unread_only=true" \
  -b cookies.txt -c cookies.txt
```

### POST /api/users/mentions/read
Marks specific mentions as read.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `comment_ids` | body | `array<integer>` | `Required` | Non-empty list of comment IDs to mark as read. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `ok` | `boolean` | Always `true` on success. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/users/mentions/read" \
  -H "Content-Type: application/json" \
  -b cookies.txt -c cookies.txt \
  -d '{
    "comment_ids": [17, 18, 19]
  }'
```

### POST /api/users/mentions/read-all
Marks every unread mention as read for the current user.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `marked_read` | `integer` | Number of mentions marked as read. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/users/mentions/read-all" \
  -b cookies.txt -c cookies.txt
```

### GET /api/users/mentions/unread-count
Returns the unread mention count for the current user.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `count` | `integer` | Unread mention count. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/users/mentions/unread-count" \
  -b cookies.txt -c cookies.txt
```

### GET /api/users/mentionable
Returns the tracked usernames that can be mentioned in comments.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| None | — | — | — | No query or body parameters. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `usernames` | `array<string>` | Tracked usernames. |

**Example**

```bash
curl -sS "https://rootcoz.example.com/api/users/mentionable" \
  -b cookies.txt -c cookies.txt
```

## Feedback

> **Note:** `POST /api/feedback/preview` requires the server to have both a GitHub token and an AI provider/model configured. `POST /api/feedback/create` requires the server GitHub token.


> **Note:** Feedback preview and creation scrub sensitive strings from the submitted content before building or creating the GitHub issue.

### POST /api/feedback/preview
Builds a preview GitHub issue from end-user feedback without creating the issue.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `feedback_type` | body | `string` | `feedback` | Optional feedback type hint. |
| `description` | body | `string` | `Required` | Free-form feedback description. |
| `console_errors` | body | `array<string>` | `[]` | Browser console errors. Maximum 50 items. |
| `failed_api_calls` | body | `array<object>` | `[]` | Failed API call snapshots. |
| `page_state` | body | `object` | `{}` | Page context captured at submission time. |
| `user_agent` | body | `string` | `""` | Browser user-agent string. |

**`failed_api_calls[]` fields**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | `integer` | `0` | HTTP status code. |
| `endpoint` | `string` | `""` | API path that failed. |
| `error` | `string` | `""` | Error message or response body. |

**`page_state` fields**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | `string` | `""` | Current page URL. |
| `active_filters` | `string` | `""` | Active filter state. |
| `report_id` | `string` | `""` | Current report ID. |

**Return value**

Returns `200 OK`.

| Field | Type | Description |
| --- | --- | --- |
| `title` | `string` | Generated issue title. |
| `body` | `string` | Generated markdown issue body. Includes the rootcoz AI attribution footer. |
| `labels` | `array<string>` | Generated issue labels. Values are `bug` or `enhancement`. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/feedback/preview" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "The feedback dialog stops responding after I open a report.",
    "console_errors": [
      "TypeError: Cannot read properties of undefined (reading '\''map'\'')"
    ],
    "failed_api_calls": [
      {
        "status": 500,
        "endpoint": "/api/feedback/preview",
        "error": "Internal Server Error"
      }
    ],
    "page_state": {
      "url": "/history?job_name=nightly-e2e",
      "active_filters": "team=platform;classification=PRODUCT BUG",
      "report_id": "job-123"
    },
    "user_agent": "Mozilla/5.0"
  }'
```

### POST /api/feedback/create
Creates a GitHub issue from a feedback preview.

**Parameters**

| Name | In | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `title` | body | `string` | `Required` | Issue title. |
| `body` | body | `string` | `Required` | Issue body in markdown. |
| `labels` | body | `array<string>` | `[]` | Issue labels. Values outside `bug` and `enhancement` are ignored. |

**Return value**

Returns `201 Created`.

| Field | Type | Description |
| --- | --- | --- |
| `issue_url` | `string` | URL of the created GitHub issue. |
| `issue_number` | `integer` | GitHub issue number. |
| `title` | `string` | Created issue title. |

**Example**

```bash
curl -sS -X POST "https://rootcoz.example.com/api/feedback/create" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Feedback dialog freezes on report page",
    "body": "## Description\n\nThe feedback dialog stops responding after I open a report.",
    "labels": ["bug"]
  }'
```

## Related Pages

- [Configuration Reference](configuration-reference.html)
- [CLI Command Reference](cli-command-reference.html)
- [Automating Common Tasks with the CLI](automating-common-tasks-with-the-cli.html)
- [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html)
- [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html)