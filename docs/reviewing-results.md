# Reviewing Analysis Results

After submitting a test failure analysis, you need to review the AI's findings, verify its classifications, and track which failures your team has addressed. This guide walks you through navigating the report page, understanding what the AI found, correcting classifications when needed, and marking failures as reviewed.

## Prerequisites

- A completed analysis job (see [Analyzing Test Failures](analyzing-failures.html))
- A rootcoz account with **reviewer** role or higher (see [Managing Users and Roles](managing-users.html))
- Viewers can see results but cannot comment, override classifications, or mark failures as reviewed

## Quick Example

Open any completed job from the dashboard and you'll see the report page:

1. Click a job row on the dashboard to open its report
2. Read the **Key Takeaway** summary at the top
3. Expand a failure card to see the full AI analysis
4. Use the classification dropdown to correct the AI if needed
5. Click **Review** to mark the failure as reviewed

From the CLI:

```bash
rootcoz results show <job-id>
```

Or with full JSON output:

```bash
rootcoz results show <job-id> --full
```

## Understanding the Report Page

### Header Bar

The sticky header at the top shows:

- **Job name** and build number (linked to Jenkins when available)
- **Status chip** — `completed`, `timeout`, etc.
- **Failure count** — total number of failed tests across all child jobs
- **Review progress** — color-coded badge showing how many failures have been reviewed:
  - 🟢 Green "Reviewed" — all failures reviewed
  - 🟠 Orange "3/5 Reviewed" — partially reviewed
  - 🔴 Red "Needs Review" — no failures reviewed yet
- **AI provider/model** — which AI analyzed the job
- **Chat** button — opens the AI chat for follow-up questions
- **Re-Analyze** button — re-run analysis with different settings (operators only)
- **Jenkins** link — opens the original Jenkins build

### Key Takeaway

The orange-bordered summary box below the header gives you a one-paragraph overview of the most important findings. Read this first to understand the overall picture before diving into individual failures.

### Metadata Row

Below the summary, a row of metadata shows:

- Who submitted the analysis
- When it was created and completed
- Analysis duration
- AI provider/model used
- Linked repositories (with tooltips showing full URLs)

## Reviewing Individual Failures

Each failure appears as a collapsible card. Click anywhere on the card header to expand it.

### Failure Card Header

The collapsed card shows at a glance:

- **Test name** — the failing test (hover for the full name, click the copy icon to copy)
- **Group count** — if multiple tests failed with the same error, you'll see "+N more with same error"
- **Classification badge** — the AI's root cause classification
- **Pattern badge** — the failure pattern (if set)
- **Review toggle** — mark individual tests or the entire group as reviewed
- **Comment count** — number of comments on this failure

### Expanded Card Sections

When you expand a failure card, you'll see:

1. **Error** — the raw error message and stack trace, shown in a red-bordered box. Use the copy button to grab it for bug reports.

2. **Analysis** — the AI's detailed explanation of what went wrong, with clickable links to source files when repositories are configured.

3. **Artifacts Evidence** — verbatim log lines from build artifacts that support the AI's conclusion. This is the raw evidence, not a summary.

4. **Suggested Fix** (Code Issue only) — when the AI classifies a failure as a code issue, it suggests a specific fix including file path, line number, and before/after code. File paths link directly to your repository when configured.

5. **Bug Report** (Product Bug only) — when the AI classifies a failure as a product bug, it generates a structured bug report with title, severity, description, and any matching Jira issues it found.

6. **Previous Analyses** — if the failure was re-analyzed, previous AI analyses are preserved in collapsible sections so you can compare.

7. **Peer Debate** — if multi-AI peer analysis was used, the debate trail shows each AI's position and how they reached (or didn't reach) consensus. See [Using Multi-AI Peer Analysis](peer-analysis.html) for details.

8. **Comments** — team discussion thread for this failure group.

### Grouped Failures

When multiple tests fail with the same error signature, rootcoz groups them into a single card. The card header shows the representative test name with "+N more with same error."

Expanding a grouped failure card reveals:

- **Review All** button — mark all tests in the group as reviewed/unreviewed in one click
- **Affected Tests** list — every test in the group, each with its own individual review toggle
- The analysis, suggested fix, and bug report apply to all tests in the group since they share the same root cause

## Understanding AI Classifications

Every failure is classified along two independent axes:

### Root Cause (Classification)

| Classification | Meaning | What the AI Provides |
|---|---|---|
| **CODE ISSUE** | The test code itself is wrong — an assertion error, incorrect setup, outdated expectation | Suggested code fix with file, line, and before/after code |
| **PRODUCT BUG** | The product under test has a defect — the test is correct but the product isn't behaving as expected | Structured bug report with title, severity, and matching Jira issues |
| **INFRASTRUCTURE** | An environment, cluster, or resource problem — timeouts, network failures, missing dependencies | Root cause details (no fix or bug report) |

### Pattern

| Pattern | Meaning |
|---|---|
| **NEW** | First occurrence or initial analysis (default) |
| **REGRESSION** | Failure started after a recent change |
| **FLAKY** | Passes and fails unpredictably |
| **INTERMITTENT** | Similar to flaky, fails sporadically |
| **KNOWN_BUG** | Matches a known, reported bug |
| **PERSISTENT** | Consistently failing across many runs |

> **Note:** The pattern is initially set to `NEW` by default. Use [failure history](tracking-history.html) and manual overrides to refine it as you learn more about the failure.

## Overriding Classifications

The AI isn't always right. You can correct both the root cause classification and the failure pattern.

### Override via the Web UI

1. Expand the failure card
2. At the bottom of the card, find the **classification dropdown** (shows the current classification)
3. Select the correct value: `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE`
4. The change saves immediately and updates the card display

To override the pattern:

1. Find the **pattern dropdown** next to the classification dropdown
2. Select: `NEW`, `REGRESSION`, `FLAKY`, `INTERMITTENT`, `KNOWN_BUG`, or `PERSISTENT`

> **Tip:** For grouped failures, overriding the classification or pattern applies to all tests in the group at once.

### Override via the CLI

```bash
rootcoz override-classification <job-id> \
  --test "com.example.MyTest.testMethod" \
  --classification "PRODUCT BUG"
```

```bash
rootcoz override-pattern <job-id> \
  --test "com.example.MyTest.testMethod" \
  --pattern "REGRESSION"
```

For failures inside a child job, add `--child-job` and `--child-build`:

```bash
rootcoz override-classification <job-id> \
  --test "com.example.MyTest.testMethod" \
  --classification "INFRASTRUCTURE" \
  --child-job "my-child-job" \
  --child-build 42
```

### What Happens When You Override

- The classification badge updates immediately in the UI
- For **classification** overrides: switching away from `CODE ISSUE` clears the suggested fix; switching away from `PRODUCT BUG` clears the bug report. This prevents stale data from showing under the wrong classification.
- The override is recorded in the classification history and appears in [analytics reports](tracking-history.html)

## Tracking Review Progress

Marking failures as reviewed helps your team track which findings have been addressed.

### Mark a Single Failure as Reviewed

Click the **Review** button on the right side of any failure card header. It turns green and shows "Reviewed by *username*."

Click it again to unmark it.

### Mark an Entire Group as Reviewed

For grouped failures, click the **Review N/M** button in the header. This marks all tests in the group in one operation.

Inside the expanded card, you can also use the **Review All** button, or toggle individual tests from the "Affected Tests" list.

### Mark as Reviewed via the CLI

```bash
rootcoz results set-reviewed <job-id> \
  --test "com.example.MyTest.testMethod" \
  --reviewed
```

To unmark:

```bash
rootcoz results set-reviewed <job-id> \
  --test "com.example.MyTest.testMethod" \
  --not-reviewed
```

Check the review status summary for a job:

```bash
rootcoz results review-status <job-id>
```

### Smart Review Suggestions

rootcoz can detect when your actions imply a failure has been reviewed:

- **After commenting** — if your comment suggests the failure is resolved (e.g., "Filed JIRA-123 for this" or "Fixed in commit abc123"), rootcoz prompts: *"Your comment suggests this failure has been reviewed. Would you like to mark it as reviewed?"*
- **After creating a bug** — when you create a GitHub issue or Jira ticket from a failure, rootcoz prompts: *"A bug issue was linked to this failure. Would you like to mark it as reviewed?"*

These are suggestions only — click "Yes" to accept or "No" to dismiss.

### Dashboard Progress Tracking

Back on the dashboard, each job row shows a **Reviewed** column with color-coded progress:

- 🟢 Green — all failures reviewed
- 🟠 Orange — some reviewed
- 🔴 Red — none reviewed

Sort by the "Reviewed" column to find jobs that need attention.

## Adding Comments

Comments let your team discuss failures directly on the report page. Each failure group has its own comment thread.

1. Expand a failure card
2. Scroll to the **Comments** section at the bottom
3. Type your comment in the text area (supports `@mentions` to notify team members)
4. Click **Post**

Comments appear in real-time for other users viewing the same report. Links to Jira tickets and GitHub PRs in comments are automatically enriched with their current status (e.g., "Open," "Merged," "Closed").

> **Tip:** You can delete your own comments by hovering over them and clicking the trash icon. A confirmation dialog prevents accidental deletions.

For more on using comments to collaborate, see [Chatting with AI About Failures](chatting-with-ai.html) for AI-powered follow-up questions.

## Navigating Child Jobs

Jenkins pipeline jobs often produce child job failures. These appear in a separate **Child Jobs** section below the top-level failures.

Each child job is a collapsible section showing:

- Child job name and build number
- Failure count badge
- Link to the child's Jenkins build
- Its own set of failure cards (with all the same review, comment, and override features)

Child jobs can be nested — a child job may contain its own child jobs. Use the **Expand All / Collapse All** buttons to quickly navigate deep hierarchies.

### Deep Linking

Click on a child job section header to add a hash fragment to the URL (e.g., `#child-job-name--42`). Share this URL to link teammates directly to a specific child job within the report.

## Advanced Usage

### Peer Analysis Summary

When [multi-AI peer analysis](peer-analysis.html) was used, a **Peer Analysis** section appears between the key takeaway and the failure list. It shows:

- Which AI providers participated in the debate
- Consensus results — how many failure groups reached consensus vs. disagreed
- Per-failure debate timelines showing each AI's position across rounds

Expand the section to audit the AI debate trail before accepting a classification.

### Re-Analyzing Failures

If you disagree with the AI's analysis, you have two options:

- **Re-analyze the entire job** — click the **Re-Analyze** button in the header bar to re-run with different settings (e.g., a different AI provider). Available to operators only.
- **Re-analyze a single failure** — expand the failure card, and click the **Re-analyze** button in the actions bar to re-run analysis for just that failure.

During re-analysis, the failure card shows a pulsing "Re-analyzing" badge. When complete, the new analysis replaces the current one, and the previous analysis is preserved in a "Previous Analysis" section.

### Creating Bug Reports

From any failure card, you can create a GitHub issue or Jira ticket directly:

- Click **GitHub Issue** or **Jira Ticket** in the expanded card's action bar
- Optionally select which AI provider/model to use for generating the issue content
- Check **Include links** to add source file links to the generated issue

See [Creating Bug Reports from Analysis](creating-issues.html) for the full workflow.

### Report Portal Integration

When Report Portal is configured, a **Push to Report Portal** button appears in the header. After reviewing all failures, rootcoz prompts: *"All failures reviewed. Update Report Portal?"* Click "Yes" to push your classifications back to Report Portal.

### Viewing Results from the CLI

List all analysis results:

```bash
rootcoz results list
```

Filter by limit:

```bash
rootcoz results list --limit 10
```

Get full JSON output for scripting:

```bash
rootcoz results show <job-id> --json
```

## Troubleshooting

**I can't override classifications or mark as reviewed**
You need the **reviewer** role or higher. Viewers can only read results. See [Managing Users and Roles](managing-users.html).

**The "Re-Analyze" button is missing**
Re-analysis requires the **operator** role. Also, the button is disabled while an analysis is already running.

**Comments aren't appearing in real-time**
rootcoz uses Server-Sent Events (SSE) for real-time updates. If your network blocks SSE connections, the page falls back to polling — comments will still appear, just with a short delay.

**Classification override didn't stick**
Verify you have network connectivity — the override is saved immediately via an API call. If the save fails, a red error message appears next to the dropdown. Refresh the page to confirm the current state.

## Related Pages

- [Analyzing Test Failures](analyzing-failures.html)
- [Chatting with AI About Failures](chatting-with-ai.html)
- [Creating Bug Reports from Analysis](creating-issues.html)
- [Tracking Failure History and Reports](tracking-history.html)
- [Managing Users and Roles](managing-users.html)