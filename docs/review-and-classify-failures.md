# Reviewing and Classifying Failures

Use the report page when you need to decide whether RootCoz got a failure right and save the final answer your team should trust. This is where you compare the AI’s reasoning, finish human review, and correct the labels before the result drives follow-up work or downstream reporting.

## Prerequisites
- A completed analysis result.
- `reviewer`, `operator`, or `admin` access to mark failures reviewed or change their labels.
- `operator` or `admin` access if you plan to re-analyze a job or a single failure.
- If you want to use the CLI examples, a configured CLI profile.

See [Tracking Analysis Progress](track-analysis-progress.html) for details. See [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html) for details.

## Quick Example
```bash
rootcoz results review-status JOB_ID
rootcoz results set-reviewed JOB_ID --test "TEST_NAME" --reviewed
rootcoz override-classification JOB_ID --test "TEST_NAME" --classification "PRODUCT BUG"
rootcoz override-pattern JOB_ID --test "TEST_NAME" --pattern "REGRESSION"
```

Use this when you already know the job ID and test name and only need to confirm review state, mark the failure reviewed, and correct both labels. The rest of this page shows the same workflow in the web report, where you can inspect grouped failures and compare AI reasoning first.

## Step-by-Step
1. Open the result you want to review.

   Start from a completed job in the dashboard or history. If you need to find an older run or a recurring failure first, see [Exploring History and Reports](explore-history-and-reports.html) for details.

2. Triage the report header before you open individual failures.

   The header shows total failures, review progress, and the AI used for the run. If peer analysis was enabled, the `Peer Analysis` summary near the top also shows how many debates reached `Consensus` and how many did not.

3. Expand one failure card and confirm the scope.

   A card can represent one failing test or several tests with the same error. When a group contains more than one test, RootCoz shows `Affected Tests` and gives you a group-level `Review All` button.

> **Note:** Review state is tracked per test, but classification and pattern changes apply to the whole same-error group. Check `Affected Tests` before you change a grouped card.

4. Compare the current answer with the evidence.

   Start with `Analysis`, then read `Artifacts Evidence`. If the failure has been re-run before, open `Previous Analysis` to compare the earlier answer with the current one.

   If peer analysis was enabled, expand `Peer Analysis` on the failure card to inspect each round and see whether the models agreed. This is the fastest way to decide whether the current answer looks solid or needs correction.

5. Mark the review complete.

   Click `Review` on a single failure when you are satisfied with it. On grouped cards, use `Review All` when the whole group is ready, or review individual tests from the `Affected Tests` list if only part of the group is ready.

   The button changes to `Reviewed` and shows who reviewed it. Click it again if you need to reopen the failure.

6. Correct the labels when the default answer is wrong.

   Use the `Classify` row on the failure card to change the root cause and the failure pattern.

   | Change this | Choices | Use it when |
   | --- | --- | --- |
   | Root cause | `CODE ISSUE`, `PRODUCT BUG`, `INFRASTRUCTURE` | The AI blamed the wrong owner or system |
   | Pattern | `NEW`, `REGRESSION`, `FLAKY`, `INTERMITENT`, `KNOWN BUG`, `PERSISTENT` | The root cause is right, but the failure's behavior over time is wrong |

7. Repeat the same flow inside child jobs when the report contains nested failures.

   If the result includes a `Child Jobs` section, expand the relevant job and review it the same way. Review and classification controls stay scoped to that child job instead of the top-level failure list.

8. Finish the job after everything is reviewed.

   When every failure in the result is marked reviewed and Report Portal is available, RootCoz prompts you to update Report Portal. If you need to create or link follow-up issues after the review is final, see [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html) for details.

## Advanced Usage
Use re-analysis when you want a better answer, not just a different badge. `Re-Analyze` in the page header creates a new result for the whole job, while `Re-analyze` on a failure card updates only that failure in place and keeps the earlier answer under `Previous Analysis`.

If peer analysis showed disagreement, or if the explanation is thin, re-run with different AI settings instead of stacking manual overrides. You can change the AI provider and model, peer analysis settings, tests repo, additional repositories, Jira search, artifact collection, and raw prompt. See [Configuring Analysis Context](configure-analysis-context.html) for details.

For child-job failures, add child scope in the CLI:

```bash
rootcoz results set-reviewed JOB_ID --test "TEST_NAME" --reviewed --child-job "CHILD_JOB" --child-build 12345
rootcoz override-pattern JOB_ID --test "TEST_NAME" --pattern "KNOWN_BUG" --child-job "CHILD_JOB" --child-build 12345
```

> **Tip:** The UI label `KNOWN BUG` uses the CLI value `KNOWN_BUG`.

Use `--not-reviewed` instead of `--reviewed` when you need to reopen a failure from the terminal. See [CLI Command Reference](cli-reference.html) for details.

## Troubleshooting
- `I can open the report but I can't change anything.`  
  You likely have viewer access. Review and classification actions require reviewer access, and re-analysis requires operator or admin access.

- `My change affected several tests.`  
  That card represents a same-error group. RootCoz applies classification and pattern changes across the whole group, so confirm the `Affected Tests` list before you save.

- `The label changed, but the explanation still needs work.`  
  Overrides update the labels immediately, but they do not replace the full AI narrative. Use `Re-analyze` if you want a fresh explanation that matches your corrected label.

- `A section disappeared after I changed the classification.`  
  That is expected. Switching to `CODE ISSUE` removes old bug-report content, switching to `PRODUCT BUG` removes old suggested-fix content, and switching to `INFRASTRUCTURE` removes both.

- `I never see the Report Portal prompt.`  
  The `All failures reviewed. Update Report Portal?` prompt only appears when every failure in the job is reviewed and Report Portal is enabled for the server.# Reviewing and Classifying Failures

Use the report page when you need to decide whether RootCoz got a failure right and save the final answer your team should trust. This is where you compare the AI’s reasoning, finish human review, and correct the labels before the result drives follow-up work or downstream reporting.

## Prerequisites
- A completed analysis result.
- `reviewer`, `operator`, or `admin` access to mark failures reviewed or change their labels.
- `operator` or `admin` access if you plan to re-analyze a job or a single failure.
- If you want to use the CLI examples, a configured CLI profile.

See [Tracking Analysis Progress](track-analysis-progress.html) for details. See [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html) for details.

## Quick Example
```bash
rootcoz results review-status JOB_ID
rootcoz results set-reviewed JOB_ID --test "TEST_NAME" --reviewed
rootcoz override-classification JOB_ID --test "TEST_NAME" --classification "PRODUCT BUG"
rootcoz override-pattern JOB_ID --test "TEST_NAME" --pattern "REGRESSION"
```

Use this when you already know the job ID and test name and only need to confirm review state, mark the failure reviewed, and correct both labels. The rest of this page shows the same workflow in the web report, where you can inspect grouped failures and compare AI reasoning first.

## Step-by-Step
1. Open the result you want to review.

   Start from a completed job in the dashboard or history. If you need to find an older run or a recurring failure first, see [Exploring History and Reports](explore-history-and-reports.html) for details.

2. Triage the report header before you open individual failures.

   The header shows total failures, review progress, and the AI used for the run. If peer analysis was enabled, the `Peer Analysis` summary near the top also shows how many debates reached `Consensus` and how many did not.

3. Expand one failure card and confirm the scope.

   A card can represent one failing test or several tests with the same error. When a group contains more than one test, RootCoz shows `Affected Tests` and gives you a group-level `Review All` button.

> **Note:** Review state is tracked per test, but classification and pattern changes apply to the whole same-error group. Check `Affected Tests` before you change a grouped card.

4. Compare the current answer with the evidence.

   Start with `Analysis`, then read `Artifacts Evidence`. If the failure has been re-run before, open `Previous Analysis` to compare the earlier answer with the current one.

   If peer analysis was enabled, expand `Peer Analysis` on the failure card to inspect each round and see whether the models agreed. This is the fastest way to decide whether the current answer looks solid or needs correction.

5. Mark the review complete.

   Click `Review` on a single failure when you are satisfied with it. On grouped cards, use `Review All` when the whole group is ready, or review individual tests from the `Affected Tests` list if only part of the group is ready.

   The button changes to `Reviewed` and shows who reviewed it. Click it again if you need to reopen the failure.

6. Correct the labels when the default answer is wrong.

   Use the `Classify` row on the failure card to change the root cause and the failure pattern.

   | Change this | Choices | Use it when |
   | --- | --- | --- |
   | Root cause | `CODE ISSUE`, `PRODUCT BUG`, `INFRASTRUCTURE` | The AI blamed the wrong owner or system |
   | Pattern | `NEW`, `REGRESSION`, `FLAKY`, `INTERMITTENT`, `KNOWN BUG`, `PERSISTENT` | The root cause is right, but the failure's behavior over time is wrong |

7. Repeat the same flow inside child jobs when the report contains nested failures.

   If the result includes a `Child Jobs` section, expand the relevant job and review it the same way. Review and classification controls stay scoped to that child job instead of the top-level failure list.

8. Finish the job after everything is reviewed.

   When every failure in the result is marked reviewed and Report Portal is available, RootCoz prompts you to update Report Portal. If you need to create or link follow-up issues after the review is final, see [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html) for details.

## Advanced Usage
Use re-analysis when you want a better answer, not just a different badge. `Re-Analyze` in the page header creates a new result for the whole job, while `Re-analyze` on a failure card updates only that failure in place and keeps the earlier answer under `Previous Analysis`.

If peer analysis showed disagreement, or if the explanation is thin, re-run with different AI settings instead of stacking manual overrides. You can change the AI provider and model, peer analysis settings, tests repo, additional repositories, Jira search, artifact collection, and raw prompt. See [Configuring Analysis Context](configure-analysis-context.html) for details.

For child-job failures, add child scope in the CLI:

```bash
rootcoz results set-reviewed JOB_ID --test "TEST_NAME" --reviewed --child-job "CHILD_JOB" --child-build 12345
rootcoz override-pattern JOB_ID --test "TEST_NAME" --pattern "KNOWN_BUG" --child-job "CHILD_JOB" --child-build 12345
```

> **Tip:** The UI label `KNOWN BUG` uses the CLI value `KNOWN_BUG`.

Use `--not-reviewed` instead of `--reviewed` when you need to reopen a failure from the terminal. See [CLI Command Reference](cli-reference.html) for details.

## Troubleshooting
- `I can open the report but I can't change anything.`  
  You likely have viewer access. Review and classification actions require reviewer access, and re-analysis requires operator or admin access.

- `My change affected several tests.`  
  That card represents a same-error group. RootCoz applies classification and pattern changes across the whole group, so confirm the `Affected Tests` list before you save.

- `The label changed, but the explanation still needs work.`  
  Overrides update the labels immediately, but they do not replace the full AI narrative. Use `Re-analyze` if you want a fresh explanation that matches your corrected label.

- `A section disappeared after I changed the classification.`  
  That is expected. Switching to `CODE ISSUE` removes old bug-report content, switching to `PRODUCT BUG` removes old suggested-fix content, and switching to `INFRASTRUCTURE` removes both.

- `I never see the Report Portal prompt.`  
  The `All failures reviewed. Update Report Portal?` prompt only appears when every failure in the job is reviewed and Report Portal is enabled for the server.

## Related Pages

- [Tracking Analysis Progress](track-analysis-progress.html)
- [Collaborating on Results](collaborate-on-results.html)
- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [Exploring History and Reports](explore-history-and-reports.html)
- [Configuring Analysis Context](configure-analysis-context.html)