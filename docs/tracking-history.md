# Tracking Failure History and Reports

Spot recurring test failures, understand per-test failure trends, and generate team-level analytics to measure classification accuracy and review progress.

## Prerequisites

- A rootcoz server with at least one completed analysis
- A registered user account (see [Managing Users and Roles](managing-users.html))
- **Admin** role to access analytics reports (the Reports page is admin-only)
- For CLI usage, a configured server connection (see [Setting Up the CLI](cli-setup.html))

## Quick Example

List all recorded failures from the CLI:

```bash
rootcoz history failures --limit 10
```

Check how often a specific test has been failing:

```bash
rootcoz history test "com.example.tests.LoginTest.testTimeout"
```

Pull a summary report of all analyzed jobs, failures, and review counts:

```bash
rootcoz reports totals --from 2026-06-01 --to 2026-06-22
```

## Browsing Failure History

Every completed analysis automatically records its failures into a searchable history. You can browse this history to find recurring patterns across all analyzed jobs.

### Using the Web UI

1. Click **History** in the navigation bar.
2. Use the filters at the top to narrow results:
   - **Search** — free-text search across test names, error messages, and job names
   - **Classification** dropdown — filter by root cause (CODE ISSUE, PRODUCT BUG, INFRASTRUCTURE)
   - **Date range** — select a preset (Last 7 days, Last 30 days) or pick custom dates
3. Click any row to jump to the full analysis result for that job.
4. Click a **test name** link to open its detailed timeline page.

> **Tip:** The table columns (Test Name, Job, Classification, Date) are sortable — click any column header to sort.

### Using the CLI

Browse paginated failure history:

```bash
rootcoz history failures --limit 50 --offset 0
```

Filter by classification or job name:

```bash
rootcoz history failures --classification "PRODUCT BUG" --job-name "my-pipeline"
```

Search across test names and error messages:

```bash
rootcoz history failures --search "NullPointerException"
```

Add `--json` to any command for machine-readable output:

```bash
rootcoz history failures --search "timeout" --json
```

## Viewing Per-Test Timelines

When you notice a test appearing in multiple analyses, drill into its full history to understand whether it's a one-off or a persistent problem.

### Using the Web UI

1. From the **History** page, click a test name to open the **Test History** page.
2. The page shows four key metrics at a glance:
   - **Failure Rate** — percentage of analyzed builds where this test failed
   - **Total Runs** — number of builds analyzed
   - **Failures** — total recorded failure count
   - **Consecutive** — total consecutive failure records

3. Below the stats, review:
   - **Classification breakdown** — how many times the test was classified as each root cause type
   - **Recent runs** — a table of recent failures with links to each analysis job
   - **Comments** — related comments from reviewers across all jobs where this test appeared

> **Note:** Failure history only records failures, not passes. When you filter by job name, the failure rate is calculated as failures ÷ total analyzed builds for that job. Without a job name filter, pass/fail stats are unavailable.

### Using the CLI

```bash
rootcoz history test "com.example.tests.LoginTest.testTimeout"
```

Filter by a specific job to get failure rate:

```bash
rootcoz history test "com.example.tests.LoginTest.testTimeout" --job-name "my-pipeline"
```

Increase the number of recent runs returned:

```bash
rootcoz history test "com.example.tests.LoginTest.testTimeout" --limit 50
```

Example output:

```
Test: com.example.tests.LoginTest.testTimeout
Failures: 12
Failure rate: 40.0%
Last classification: INFRASTRUCTURE

Recent runs:
JOB_NAME             BUILD  CLASSIFICATION   DATE
my-pipeline          #142   INFRASTRUCTURE   2026-06-20
my-pipeline          #138   INFRASTRUCTURE   2026-06-18
my-pipeline          #135   CODE ISSUE       2026-06-15
```

## Searching by Error Signature

Every failure has an error signature — a hash of its error message and stack trace. Use signature search to find all tests that fail with the same underlying error, even if the test names differ.

### Using the CLI

```bash
rootcoz history search --signature "a1b2c3d4e5f6..."
```

The output shows:

- **Total occurrences** — how many times this exact error has appeared
- **Unique tests** — how many different test names hit this error
- **Test list** — each affected test with its occurrence count

> **Tip:** You can find error signatures in the analysis result details. When you see the same error across unrelated tests, it often points to a shared infrastructure issue.

## Viewing Job-Level Statistics

Get aggregate statistics for a specific job to understand its overall health and trend.

```bash
rootcoz history stats "my-pipeline"
```

This returns:

- **Builds analyzed** — total completed builds for this job
- **Builds with failures** — how many builds had at least one failure
- **Failure rate** — ratio of failing builds to total builds
- **Most common failures** — top 10 most frequently failing tests with their classifications
- **Recent trend** — `improving`, `worsening`, or `stable` (compares last 7 days vs. previous 7 days)

## Generating Analytics Reports

Analytics reports provide team-level metrics for tracking classification trends, review progress, and AI accuracy. Reports are available to **admin** users only.

### Report Types

| Report | What it shows | When to use it |
|--------|--------------|----------------|
| **Total Failures** | Job count, failure count, review count with per-job breakdown | Track overall team throughput and review progress |
| **Classification Overrides** | Where users changed the AI's classification, grouped by from→to transition, plus AI accuracy percentage | Measure AI reliability and identify systematic misclassification patterns |
| **Issues Created** | GitHub issues and Jira tickets created from analyses, split by tracker type | Track how many bugs were filed from analysis results |

### Using the Web UI

1. Click **Reports** in the admin navigation section.
2. Use the sidebar to switch between the three report tabs: **Total Failures**, **Classification Overrides**, and **Issues Created**.
3. Apply filters to scope the report:
   - **Search** — filter the detail tables by test name or job name
   - **Team / Tier / Version** — metadata filters (requires job metadata to be configured)
   - **Status** — filter by job status (defaults to completed)
   - **Date range** — restrict to a time window
   - **Labels** — include or exclude jobs by label

> **Note:** Metadata filters (team, tier, version) only work when job metadata is configured. See [Reviewing Analysis Results](reviewing-results.html) for details on job metadata.

#### Reading the Total Failures Report

The summary cards show three numbers:

- **Total Jobs** — number of analysis jobs matching your filters
- **Total Failures** — sum of all failures across those jobs
- **Total Reviewed** — how many failures have been marked as reviewed

Expand **Job Details** to see the per-job breakdown with failure and review counts.

#### Reading the Classification Overrides Report

The summary cards show:

- **Total Reviewed** — failures that have been reviewed
- **AI Correct** — reviewed failures where the user kept the AI's classification
- **Total Overrides** — reviewed failures where the user changed the classification
- **AI Accuracy** — percentage of reviewed failures where the AI was correct

Overrides are grouped by transition (e.g., "CODE ISSUE → PRODUCT BUG"). Expand any group to see individual test-level details. Each override shows its **axis** — whether the user changed the **Root Cause** (CODE ISSUE, PRODUCT BUG, INFRASTRUCTURE) or the **Pattern** (FLAKY, REGRESSION, KNOWN_BUG, etc.).

#### Reading the Issues Created Report

Shows all GitHub issues and Jira tickets created from analysis results, with counts split by tracker type. Each row links to the external issue and the originating analysis job.

### Using the CLI

All three reports support the same filter flags:

| Flag | Description |
|------|-------------|
| `--team` | Filter by team metadata |
| `--tier` | Filter by tier metadata |
| `--version` | Filter by version metadata |
| `--from` | Start date (YYYY-MM-DD) |
| `--to` | End date (YYYY-MM-DD) |
| `--status` | Filter by job status |
| `--tags` | Comma-separated tags to include |
| `--exclude-tags` | Comma-separated tags to exclude |

**Total failures:**

```bash
rootcoz reports totals --from 2026-06-01 --to 2026-06-22
```

**Classification overrides:**

```bash
rootcoz reports overrides --team "platform"
```

**Issues created:**

```bash
rootcoz reports issues --from 2026-06-01
```

Add `--json` for machine-readable output suitable for dashboards or scripts:

```bash
rootcoz reports totals --from 2026-06-01 --json
```

## Advanced Usage

### Sharing Report URLs

The Reports page encodes all filters in the URL query string. Copy the browser URL to share a specific filtered view with teammates — they'll see the same filters pre-applied when they open the link.

Filter parameters in the URL include `report`, `team`, `tier`, `version`, `from`, `to`, `status`, `label`, `exclude_label`, and `search`.

### Asking AI About Reports

Admins can use the **Admin Chat** to ask AI questions about failure trends. The AI has access to all three report endpoints and can query them with filters, generate summaries, and produce downloadable HTML reports.

Example prompts:

- "Show me the AI accuracy trend for the last month"
- "Which teams have the most classification overrides?"
- "Generate a report of all PRODUCT BUG failures from last week"

See [Chatting with AI About Failures](chatting-with-ai.html) for more on using the chat feature.

### Classifying Tests from History

You can classify tests directly from the CLI. This is useful when reviewing historical failures:

```bash
rootcoz classify "com.example.tests.LoginTest.testTimeout" \
  --type FLAKY \
  --job-id "abc123" \
  --reason "Intermittent network timeout in CI"
```

Valid classification types: `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, `KNOWN_BUG`, `INTERMITTENT`.

> **Warning:** `KNOWN_BUG` requires a `--references` flag with bug URLs or ticket keys.

See [Reviewing Analysis Results](reviewing-results.html) for the full classification workflow.

### Excluding a Specific Job

When investigating a test, you may want to exclude the current job's results from the history to see only *other* occurrences. All history commands support `--exclude-job-id`:

```bash
rootcoz history test "com.example.SomeTest" --exclude-job-id "current-job-uuid"
rootcoz history stats "my-pipeline" --exclude-job-id "current-job-uuid"
rootcoz history search --signature "abc123..." --exclude-job-id "current-job-uuid"
```

### Exporting Report Data

Combine `--json` with standard tools to export report data:

```bash
# Export totals to a CSV
rootcoz reports totals --from 2026-06-01 --json | \
  jq -r '.jobs[] | [.job_name, .failure_count, .reviewed_count, .created_at] | @csv'

# Count overrides by transition type
rootcoz reports overrides --json | \
  jq '.groups[] | "\(.from) → \(.to): \(.count)"'
```

See [CLI Recipes](cli-recipes.html) for more export patterns.

## Troubleshooting

**No failures appear in the History page**
- History is populated automatically when analyses complete. If you just started the server, a one-time backfill runs on startup to index all previously completed jobs. Check the server logs for "Backfilling failure_history" messages.

**Failure rate shows "N/A" on the test history page**
- Failure rate requires a job name filter. Without it, the system cannot calculate total runs because it only tracks failures, not passes. Filter by job name in the CLI with `--job-name` or by clicking through from a specific job's results.

**Reports page returns 403 Forbidden**
- Analytics reports are restricted to admin users. Ask your administrator to upgrade your role. See [Managing Users and Roles](managing-users.html).

**Report data seems incomplete after classification changes**
- The Classification Overrides report only includes overrides for failures that have been marked as **reviewed**. Make sure reviewers are completing the review step after changing classifications. See [Reviewing Analysis Results](reviewing-results.html).

## Related Pages

- [Reviewing Analysis Results](reviewing-results.html)
- [CLI Recipes](cli-recipes.html)
- [Chatting with AI About Failures](chatting-with-ai.html)
- [Analyzing Test Failures](analyzing-failures.html)
- [CLI Command Reference](cli-reference.html)