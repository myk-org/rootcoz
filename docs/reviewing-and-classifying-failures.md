# Reviewing and Classifying Failures

Use the report page to decide which failing tests are already understood, which need a different classification, and which still need more work. Doing this consistently keeps the run’s review status trustworthy and prevents the same underlying failure from being triaged more than once.

## Prerequisites
- A completed run with failures. See [Finding Runs on the Dashboard](finding-runs-on-the-dashboard.html).
- If the run is still queued or running, wait for it to finish first. See [Tracking Analysis Progress](tracking-analysis-progress.html).

## Quick Example
1. Open a completed run.
2. Expand the first failure card.
3. If the current bucket is wrong, change `Override classification` to `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE`.
4. Click `Review` for a single failure, or `Review All` for a grouped card.
5. Repeat until the badge at the top changes from `Needs Review` to `Reviewed`.

That is the whole review loop: confirm the classification, then mark the failure done. The rest of this page shows how to do that confidently when one card covers several tests, peer analysis is available, or failures are nested under child jobs.

## Step-by-Step
1. Check the run-level review badge first.

At the top of the report, rootcoz shows `Needs Review`, `X/Y Reviewed`, or `Reviewed`. That total includes failures in child jobs, so it is the fastest way to tell whether you are actually finished.

| If you see | What it means | What to do |
| --- | --- | --- |
| `Review` | This card represents one failing test. | Review it individually. |
| `Review All (2/5)` and `Affected Tests (5)` | Several tests share the same underlying failure. | Decide once, then update the whole group. |
| `Peer Analysis` | Additional AI reviewers weighed in. | Open it when the classification is unclear or disputed. |
| `Child Jobs (N)` | Downstream jobs have failures of their own. | Review those sections before expecting the run to be fully reviewed. |

2. Treat grouped failures as one problem first.

If a card shows `Affected Tests`, those tests failed for the same reason. Use that list to confirm how many tests are affected, then use `Review All` when the whole group is ready to close.

> **Tip:** On grouped cards, the classification menu updates the group, not just the one test name shown at the top.

3. Read the card in the order rootcoz presents it.

Start with `Error`, then `Analysis`. If present, use `Artifacts Evidence` when you want the supporting logs or evidence, `Suggested Fix` when the failure looks actionable in code, and `Bug Report` when the failure is better treated as a product issue.

4. Use peer-analysis output when you want a second opinion.

Open the top `Peer Analysis` section to scan the whole run, or the `Peer Analysis` block inside a single failure card when you are working one issue at a time. The summary shows which models participated, how many debates reached consensus, and how many rounds were used.

If an entry says `+N tests with same error`, one peer-analysis result covers more than one affected test. Inside each round, compare the `Main AI` and `Peer` entries, their classifications, and whether a peer `Agrees` or `Disagrees`.

5. Save the right classification.

Use `Override classification` only when the current bucket is wrong. The available choices are `CODE ISSUE`, `PRODUCT BUG`, and `INFRASTRUCTURE`.

| Action | Use it for | What changes |
| --- | --- | --- |
| `Review` or `Review All` | You have finished triage for this failure. | Review status only |
| `Override classification` | The failure is in the wrong bucket. | Saved classification for the failure group in this run |
| `Comments` | You need to leave context or hand off work. | A visible note on the failure, and sometimes a review suggestion |

> **Note:** Marking a failure as reviewed does not change its classification, and changing the classification does not mark it reviewed.

6. Mark the failure reviewed.

Use `Review` for a single test or `Review All` for a grouped card. After you click it, the button changes to `Reviewed`, and the UI can show who marked it.

If you leave a comment that clearly says the failure has been handled, rootcoz may offer `Mark as reviewed?`. Accept it only if the failure truly no longer needs triage.

7. Repeat the same process for child jobs.

Open `Child Jobs`, expand each failed job, and review those cards just like the top-level failures. Child jobs can be nested, so keep expanding until you reach the leaves.

> **Warning:** A classification change inside a child job applies to the specific child build you are reviewing. If the same job fails again in a different build, review that build separately.

8. Finish the run and update external status if needed.

When every failure in the run is reviewed, the top badge changes to `Reviewed`. If Report Portal is available on your server, rootcoz can then prompt you to update it, or you can use the `Push to Report Portal` button where it appears.

## Advanced Usage
Use the CLI when you want repeatable review updates or need to fix a specific item without going back through the UI.

```bash
rootcoz results review-status job-123
rootcoz results set-reviewed job-123 --test "test_a" --reviewed
rootcoz override-classification job-123 --test "test_a" --classification "CODE ISSUE"
rootcoz push-reportportal job-123
```

Use those commands for top-level failures in a run. The first command is useful in scripts because it reports review progress and comment count.

```bash
rootcoz results set-reviewed some-job-id --test "test_child_a" --reviewed --child-job "child-job" --child-build 42
rootcoz override-classification some-job-id --test "test_child_a" --classification "INFRASTRUCTURE" --child-job "child-job" --child-build 42
rootcoz push-reportportal some-job-id --child-job-name "child-job" --child-build-number 42
```

Use the child-job flags when you are updating a pipeline child build instead of the top-level run. For the full syntax and output options, see [CLI Command Reference](cli-command-reference.html).

The `All failures reviewed` prompt appears when you finish the last remaining review in the current session. It does not open just because you navigated to a run that was already fully reviewed.

## Troubleshooting
- I cannot see review controls yet: the run may still be queued, running, or not fully processed. See [Tracking Analysis Progress](tracking-analysis-progress.html).
- The page never reaches `Reviewed`: expand `Child Jobs` and check for nested failures. The run-level counter includes them.
- A grouped card shows more than one classification badge: not every affected test currently has the same saved classification. Reapply the desired classification from that card, or update the remaining tests individually.
- `Review All` or `Override classification` reports only partial success: retry the listed test names one by one, or use the CLI commands above for the exact failures that did not save.
- `Push to Report Portal` is missing or disabled: your server may not have that feature enabled, or the selected scope may have no failures to push.

## Related Pages

- [Tracking Analysis Progress](tracking-analysis-progress.html)
- [Finding Runs on the Dashboard](finding-runs-on-the-dashboard.html)
- [Commenting and Mentioning Teammates](commenting-and-mentioning-teammates.html)
- [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html)
- [Exploring Failure History and Test Trends](exploring-failure-history-and-test-trends.html)