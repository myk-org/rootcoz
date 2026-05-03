# Push Classifications to Report Portal

You want the classifications already saved in JJI to show up on the matching Report Portal failures so your triage stays consistent without relabeling the same failures twice. This guide shows the fastest push flow and how to interpret launches that only partially matched.

## Prerequisites
- A saved JJI analysis that already contains failures, plus its `job_id`
- Report Portal configured on the JJI server with `REPORTPORTAL_URL`, `REPORTPORTAL_PROJECT`, `REPORTPORTAL_API_TOKEN`, and `PUBLIC_BASE_URL`
- A Report Portal token that can update the target launch
- If your Report Portal uses a self-signed certificate, `REPORTPORTAL_VERIFY_SSL=false`

## Quick example

```bash
jji push-reportportal job-123
```

```text
Pushed 3 classification(s) to Report Portal
Launch ID: 42
```

Run this when the analysis is already saved in JJI. JJI finds the matching Report Portal launch, updates the matched failed items, and prints the launch it touched.

## Step-by-step

1. Configure the server-side Report Portal settings.

```dotenv
REPORTPORTAL_URL=https://reportportal.example.com
REPORTPORTAL_PROJECT=my-project
REPORTPORTAL_API_TOKEN=your-rp-token
PUBLIC_BASE_URL=https://jji.example.com

# Optional for self-signed certificates
REPORTPORTAL_VERIFY_SSL=false
```

`REPORTPORTAL_URL`, `REPORTPORTAL_PROJECT`, and `REPORTPORTAL_API_TOKEN` make the feature available. `PUBLIC_BASE_URL` is required because each push includes a link back to the JJI report.

> **Tip:** You usually do not need `ENABLE_REPORTPORTAL=true`. JJI enables the integration automatically when the URL, project, and token are set. `ENABLE_REPORTPORTAL=false` forces it off.

See [Configuration and Environment Reference](configuration-and-environment-reference.html) for the full setting list.

2. Push the saved analysis from the CLI.

```bash
jji push-reportportal job-123
```

JJI looks up the Report Portal launch by the Jenkins build URL stored with that analysis. It then updates the matched failed items with the JJI classification and a link back to the report.

3. Use the web UI when you prefer point-and-click.

Open the saved report and click **Push to Report Portal**. For reports without child jobs, the button appears in the main report header.

4. Push a specific child job when the analysis contains pipeline or nested child runs.

```bash
jji push-reportportal job-123 \
  --child-job-name "my-child" \
  --child-build-number 42
```

Use the same child job name and build number shown in the JJI report. The same approach works for nested child jobs too.

> **Warning:** `--child-job-name` and `--child-build-number` are a pair. If you provide the child job name, you must also provide the child build number.

In the UI, use the **Push to Report Portal** button on the child job section you want to sync.

5. Switch to JSON output when you need the exact result.

```bash
jji --json push-reportportal job-123
```

Use the returned fields to tell whether the push fully succeeded, partially succeeded, or needs follow-up. See [CLI Command Reference](cli-command-reference.html) for the command variants and global options.

## Advanced Usage

```bash
jji --json push-reportportal job-123
```

| Common outcome | Result pattern | What it means | What to do next |
| --- | --- | --- | --- |
| Successful push | `pushed > 0`, `unmatched = []`, `errors = []` | Everything JJI could update was pushed cleanly. | No follow-up needed. |
| `Some classifications could not be pushed.` | `pushed > 0` with `unmatched` or `errors` | JJI updated some Report Portal items, but not all of them. | Review the unmatched names or errors, fix the cause, then run the same push again. |
| `No classifications could be matched.` | `pushed = 0`, `unmatched` populated, `errors = []` | JJI finished the push attempt, but none of the remaining items could be turned into a Report Portal update. | Check the unmatched names and the classification mapping below. |
| `Failed to push classifications to Report Portal.` | `pushed = 0`, `errors` populated | JJI could not complete the push. | Fix the reported problem, then retry. |

For automation, read the JSON fields instead of relying on the process exit code alone. A push can return structured `unmatched` or `errors` details without turning into a CLI failure.

JJI publishes these classification mappings into Report Portal:

| JJI classification | Sent to Report Portal as |
| --- | --- |
| `PRODUCT BUG` | Product Bug |
| `CODE ISSUE` | Automation Bug |
| `INFRASTRUCTURE` | System Issue |

Matching is tolerant of common naming differences. Exact test-name matches work, and short Report Portal item names can still match fully qualified names from JJI, but unrelated naming schemes remain unmatched.

Only failed Report Portal items are updated. If a failure already has Jira matches attached to a product-bug report, JJI pushes those issue links along with the classification. See [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html) for that workflow.

> **Note:** If a saved history classification marks a test as `INFRASTRUCTURE`, JJI pushes it to Report Portal as a System Issue even when the latest AI classification says something else.

If you prefer a shorter command, `jji push-rp job-123` performs the same push.

## Troubleshooting

- `Report Portal integration is disabled or not configured`: set `REPORTPORTAL_URL`, `REPORTPORTAL_PROJECT`, and `REPORTPORTAL_API_TOKEN`, and make sure `ENABLE_REPORTPORTAL` is not forcing the feature off.
- `PUBLIC_BASE_URL must be set`: point `PUBLIC_BASE_URL` at the public JJI URL users actually open in the browser.
- The **Push to Report Portal** button is missing: the server is not currently exposing Report Portal support, or the report contains child jobs and you need the button inside the child job section instead.
- `Job ... not found`: use the JJI `job_id`, not the Jenkins build number.
- `Child job ... not found`: recheck both the child job name and child build number from the report.
- `No failures to push to Report Portal.`: the selected report or child run has no failures, so JJI stops before contacting Report Portal.
- `No Report Portal launch found`: make sure the matching Report Portal launch description contains the Jenkins build URL from the analyzed run.
- `Ambiguous RP launch`: more than one launch matches the same Jenkins build URL. Remove the duplicate launches, then retry.
- `No failed test items found in RP launch.`: the launch exists, but there were no failed items for JJI to update.
- `No overlap` or many `unmatched` items: keep test names aligned between JJI and Report Portal. Short-name versus fully qualified-name differences are fine; unrelated names are not.
- `403` or `Not a launch owner`: use a Report Portal token that has permission to update that launch.
- TLS or certificate errors: set `REPORTPORTAL_VERIFY_SSL=false` when your Report Portal uses a self-signed certificate.

## Related Pages

- [Review and Classify Failures](review-and-classify-failures.html)
- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html)
- [CLI Command Reference](cli-command-reference.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)