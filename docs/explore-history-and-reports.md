# Exploring History and Reports

Use RootCoz's history views when you need to answer two practical questions quickly: has this failure happened before, and is it part of a broader trend? These screens help you move from one test failure to per-test history and then out to filtered team summaries.

## Prerequisites
- You are signed in to RootCoz.
- At least one analysis result already exists. See [Submitting Analyses](submit-analyses.html) for details.
- Your account has access to `Reports` if you want the team summary views.

## Quick Example
```bash
rootcoz history failures --search tests.test_auth.test_login
rootcoz history test "tests.test_auth.test_login"
rootcoz reports totals --team alpha --from 2025-01-01 --to 2025-06-01 --review-status reviewed
```

Use the first command to find matching failures, the second to inspect one test over time, and the third to compare reviewed work for one team in a date window.

## Step-by-Step
1. Start in the right place.

| If you want to... | Open | Best for |
| --- | --- | --- |
| Check one failing test or result | `History` | Repeats, classifications, and recent runs |
| Compare filtered job totals across teams or releases | `Reports` | Dates, metadata, labels, and review progress |
| See where reviewers changed RootCoz's answer | `Reports` | Classification override patterns |
| See what follow-up work already exists | `Reports` | GitHub and Jira issue summaries |

2. Search recurring failures in `History`.
Open `History`, use the search box to narrow the table by test name, then add a classification filter or date range if the list is still noisy. Click a row to open that result, or click the test name itself to open that test's dedicated history page.

3. Read the test history page.
Start with `Failure Rate`, `Total Runs`, `Failures`, and `Consecutive` to understand whether the test is stable, degrading, or repeatedly broken. Then read the classification badges, comments, `First seen`, and `Last seen`, and open any row in `Recent Runs` to jump back to a specific result.

4. Move to `Reports` for broader trends.
Choose `Total Failures` when you want summary counts, `Classification Overrides` when you want review changes, or `Issues Created` when you want follow-up visibility. Use the filters across the top to narrow by `Team`, `Tier`, `Version`, `Status`, `Review status`, date range, and label include/exclude chips.

> **Note:** If you leave the status filter empty, report summaries start with completed jobs only.

5. Read the report detail that matches your question.
In `Total Failures`, expand `Job Details` to compare jobs in the current slice. In `Classification Overrides`, expand a `from -> to` group to see which tests changed and who changed them; in `Issues Created`, open the external issue link or the linked result to inspect the original analysis.

When you find a specific result you want to confirm or correct, see [Reviewing and Classifying Failures](review-and-classify-failures.html) for details.

## Advanced Usage
Use the CLI when you want repeatable lookups or want to save filters in shell history.

```bash
rootcoz history failures --classification "INFRASTRUCTURE" --limit 50
rootcoz history test "tests.test_auth.test_login" --limit 50
rootcoz history stats "test-job"
rootcoz reports overrides --status completed --tags nightly,smoke
rootcoz reports issues --team alpha --review-status reviewed
```

> **Tip:** The `Reports` page keeps its active tab and filters in the URL, so you can refresh, bookmark, or share the same filtered view.

See [CLI Command Reference](cli-reference.html) for all flags, or see [API Endpoint Reference](api-reference.html) if you want to script the same lookups.

## Troubleshooting
- I do not see `Reports` in the sidebar.  
  Ask an administrator to grant report access, then reload the page.

- `History` is empty for a test I expect to find.  
  Remove the classification filter, widen the date range, or search with the full test name.

- A report looks smaller than expected.  
  Add the statuses you care about, then re-check your team, version, and label filters.

- There is no data to explore yet.  
  Run an analysis first. See [Submitting Analyses](submit-analyses.html) for details.

## Related Pages

- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [Submitting Analyses](submit-analyses.html)
- [Use Server Chat for Cross-Job Analysis](use-server-chat-for-cross-job-analysis.html)
- [CLI Command Reference](cli-reference.html)