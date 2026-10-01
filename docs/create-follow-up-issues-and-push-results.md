# Creating Follow-Up Issues and Pushing Results

Turn a reviewed failure into real follow-up work: file a GitHub issue or Jira ticket from the report, link a failure to a ticket that already exists, and push the finished classifications into Report Portal. Every action here starts from a result page, and the issue content is generated from the same AI analysis you already reviewed.

## Prerequisites

- A completed analysis result, ideally with its failures reviewed. See [Reviewing and Classifying Failures](review-and-classify-failures.html) for details.
- A `GitHub Token` or Jira credentials on your own profile. Issue creation always uses your token, never a stored server credential. See [Managing Your Account and Notifications](manage-account-and-notifications.html) for details.
- `reviewer` or above to link an existing issue with `Track`.
- `operator` or `admin` to push results to an exporter. Push is rejected for reviewers.
- Report Portal enabled on the server (`ENABLE_REPORTPORTAL`, with `REPORTPORTAL_URL`, `REPORTPORTAL_API_TOKEN`, and `REPORTPORTAL_PROJECT`) if you want to push classifications. See [Configuration Reference](configuration-reference.html) for details.
- For the CLI examples, an authenticated CLI profile. See [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html) for details.

## Quick Example

```bash
rootcoz preview-issue JOB_ID --test "tests.test_auth.test_login" --type github --include-links
rootcoz create-issue JOB_ID --test "tests.test_auth.test_login" --type github \
  --title "Login fails after token refresh" --body "$(cat issue-body.md)"
rootcoz exporters
rootcoz push JOB_ID --plugin reportportal
```

This generates and reviews issue content, creates the issue, confirms which exporters are available, and pushes the reviewed classifications.

## Step-by-Step

1. Finish the review first.

   Issue content and pushed classifications are generated from the current analysis, so correct the labels before you file anything. The `Create` action on a failure card also offers `Mark as reviewed?` right after the issue is created.

2. Generate a preview from the failure card.

   Expand a failure, then use `GitHub Issue` or `Jira Ticket` in the `Actions` row. RootCoz opens the issue prompt, which comes from the `issue_prompt` stored with the job or from `.rootcoz/ROOTCOZ_ISSUE_PROMPT.md` in the test repository. Edit it if you want to steer the title and body, then click `Continue`.

   RootCoz calls `POST /results/{job_id}/preview-github-issue` or `POST /results/{job_id}/preview-jira-bug`, which draft a title and body, and search for issues that look like duplicates of the same bug.

   `Include links` adds clickable report and build URLs. `AI for issue generation` lets you pick a different provider or model for the draft. The preview also lists any similar issues it found, so you can link existing work instead of filing a duplicate.

   > **Note:** `Include links` only has an effect when the server has a public base URL configured, because relative links would resolve against the tracker instead of RootCoz. See [Configuration Reference](configuration-reference.html) for details.

3. Set the Jira specifics when you file a Jira ticket.

   In the preview step, pick or type the `Jira Project` (the server searches your projects as you type), choose an `Issue Type` from `Bug`, `Task`, `Story`, `Epic`, `Sub-task`, or `Custom...`, and optionally pick a `Security Level`. A project key must be available on the server or in the request, otherwise ticket creation fails.

   GitHub issues need a repository: RootCoz always uses the deployment's `TESTS_REPO_URL`. It deliberately ignores the job's additional repositories, so you cannot retarget issue creation at another repo — a job's `request_params` are never consulted for this. To file against a different repository, change `TESTS_REPO_URL` on the server (or the `GITHUB` category in Server Settings) first.

4. Create the issue and let RootCoz record the link.

   Edit the `Title` and `Body` if the draft is not right, then click `Create GitHub Issue` or `Create Jira Ticket`. On success RootCoz automatically:

   - adds a comment on the failure containing the new issue link, and
   - adds a tracked-in link for that issue, shown as a `GitHub` or `Jira` badge under the failure, which you can remove later like any other tracked link.

   Creation uses your own credentials. A missing or expired token returns an error that points back to your profile settings.

   > **Warning:** GitHub and Jira issue creation can be disabled per server with `ENABLE_GITHUB_ISSUES` and `ENABLE_JIRA_ISSUES`. When disabled, the buttons are greyed out and the endpoints refuse the request.

5. Link a failure to an issue that already exists.

   Use the `Track` button on the `Classify` row, paste the `Issue URL` into the `Track In` dialog, and click `Save`. RootCoz detects `GitHub` or `Jira` from the URL itself and shows the detected type before you save. You can link several issues to the same failure.

   Creators can remove their own links; admins can remove any link.

6. Review what Jira search already found during analysis.

   When Jira is configured, analysis for a `PRODUCT BUG` already searched for related tickets before you opened the report:

   1. The AI generates search keywords as part of the failure analysis.
   2. Identical keyword sets are deduplicated into one Jira search, and the searches run in parallel.
   3. Each candidate is scored for relevance by the AI.
   4. Only relevant matches are attached to the result, under `Matching Jira Issues` in the `Bug Report` section, with their status.

   Jira problems never break an analysis. Search failures are logged and swallowed, so a result without matching tickets usually means the lookup failed or found nothing relevant — not that the analysis is wrong.

7. Push reviewed classifications to Report Portal.

   Click `Push to Report Portal` in the report header, confirm, and RootCoz matches the job to its Report Portal launch, updates the matching test items, and reports how many classifications were pushed, plus any unmatched items and errors.

   You can also check what is available before pushing:

   ```bash
   rootcoz exporters
   ```

   The list shows each exporter plugin with its display name and whether it is ready. Report Portal is reported as disabled when the integration is unconfigured, when every push-content toggle is off, or when `PUBLIC_BASE_URL` is missing while the rootcoz-URL toggle is on.

   Report Portal push content is controlled by three toggles:

   | Toggle | Effect when enabled |
   | --- | --- |
   | `RP_PUSH_CLASSIFICATIONS` | Maps RootCoz classifications to Report Portal defect types |
   | `RP_PUSH_ROOTCOZ_URL` | Adds the RootCoz report URL as a comment on the test item |
   | `RP_PUSH_TRACKER_LINKS` | Adds linked GitHub and Jira issues as external system issues |

   At least one of the three must be enabled or the push is refused.

## Advanced Usage

Push through the generic exporter endpoint when you want the plugin dispatch rather than the Report Portal shortcut. `POST /results/{job_id}/push/{plugin_name}` takes the same optional `child_job_name` and `child_build_number` parameters and returns the exporter's own result fields plus `success` and `message`. The Report Portal-specific endpoint `POST /results/{job_id}/push-reportportal` remains available and is what the UI button uses.

Let the pipeline push for you. When `AUTO_PUSH_EXPORTERS` is set to a comma-separated plugin list and auto-review is enabled, RootCoz pushes to those exporters once every failure in the job is reviewed, recording the push under the reserved `rootcoz-ai` identity. An empty `AUTO_PUSH_EXPORTERS` disables this, and auto-push failures are logged rather than surfaced in the UI. This is a server setting and can be changed without a restart.

Create issues from the terminal, including for nested child-job failures:

```bash
rootcoz get-issue-prompt JOB_ID
rootcoz preview-issue JOB_ID --test "TEST_NAME" --type jira --jira-project-key PROJ
rootcoz create-issue JOB_ID --test "TEST_NAME" --type jira \
  --jira-project-key PROJ --jira-issue-type Bug --jira-security-level "Internal"
rootcoz create-issue JOB_ID --test "TEST_NAME" --type github \
  --child-job "CHILD_JOB" --child-build 12345
rootcoz push-reportportal JOB_ID --child-job-name "CHILD_JOB" --child-build-number 12345
rootcoz push JOB_ID --plugin reportportal --json
```

Tracker flags fall back to the values in your CLI server profile, so pass them explicitly only when you need to override. See [CLI Command Reference](cli-reference.html) for details.

Send feedback about RootCoz itself through the `Feedback` button in the application header. You describe the problem, RootCoz collects browser context and recent console and API errors automatically, then drafts a GitHub issue you can edit before it is created. This flow is separate from failure issues and requires feedback to be enabled on the server.

For raw endpoints, see [API Endpoint Reference](api-reference.html) for details.

## Troubleshooting

- The `GitHub Issue` or `Jira Ticket` button is greyed out.  
  Issue creation is disabled for this server, or the job was analyzed without a repository or Jira project to target. Ask an administrator to check `ENABLE_GITHUB_ISSUES` and `ENABLE_JIRA_ISSUES`.

- Creation fails with `GitHub token is required` or `Jira token is required`.  
  Issue creation uses only your own credentials. Add the token on your profile settings page and try again.

- The preview has no `Include links` effect.  
  The server has no public base URL configured. Set `PUBLIC_BASE_URL` so report links can be absolute.

- No `Matching Jira Issues` appear on a product bug.  
  The search may have found nothing, or the lookup failed. Failures are logged and ignored, so the analysis result is still valid.

- `Exporters` shows Report Portal as disabled.  
  Run `rootcoz exporters` and check the reason: the integration is off, all three `RP_PUSH_*` toggles are off, or `PUBLIC_BASE_URL` is missing while `RP_PUSH_ROOTCOZ_URL` is on.

- The push returns `Operator access required.`  
  Exporter pushes need `operator` or `admin`. Reviewers can create and link issues, but not push.

- The push reports unmatched items.  
  RootCoz matched the launch but could not pair some failures with Report Portal test items. Check the reported item names against the launch, and confirm the job name and build number match the Report Portal launch.

## Related Pages

- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Collaborating on Results](collaborate-on-results.html)
- [Managing Your Account and Notifications](manage-account-and-notifications.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)
- [CLI Command Reference](cli-reference.html)
