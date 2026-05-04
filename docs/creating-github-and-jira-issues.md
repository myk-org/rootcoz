# Creating GitHub and Jira Issues

Use a completed failure report to draft a GitHub issue or Jira ticket, review it, and file it under your own name. This is the fastest way to turn RootCoz analysis into tracked follow-up work without copying logs, AI output, or links by hand.

## Prerequisites
- A completed analysis report with the failure you want to file. If you need one first, see [Run Your First Analysis](run-your-first-analysis.html).
- Your RootCoz username saved in `Settings`.
- For browser-based GitHub creation, a personal access token with `repo` scope saved in `Settings`.
- For browser-based Jira creation, a Jira token saved in `Settings`; add your Jira email too when you use Jira Cloud.
- The server must have GitHub issue creation and/or Jira issue creation enabled.

> **Note:** In the browser, you can preview generated content without personal tokens. Creating the issue directly from the dialog stays disabled until your personal GitHub or Jira details are saved.

## Quick Example

### Browser
1. Open a completed report and expand the failure you want to file.
2. Click `GitHub Issue` or `Jira Ticket`.
3. Keep or edit the prompt, click `Continue`, and review the preview.
4. Click `Create GitHub Issue` or `Create Jira Ticket`.

### CLI
```bash
rootcoz preview-issue \
  job-1 \
  --test tests.TestA.test_one \
  --type github \
  --github-token "$GITHUB_TOKEN" \
  --github-repo-url "https://github.com/org/repo"

rootcoz create-issue \
  job-1 \
  --test tests.TestA.test_one \
  --type github \
  --title "Bug: login fails" \
  --body "Login returns 500" \
  --github-token "$GITHUB_TOKEN" \
  --github-repo-url "https://github.com/org/repo"
```

If your CLI server profile already stores the same tracker values, you can omit the matching flags.

## Step-by-Step

1. Open `Settings`, save your username, and add the tracker token you want to use. For Jira Cloud, save your Jira email and token together.

1. Open the completed report and expand the failure card you want to turn into tracked work.

1. If the failure card shows AI selection controls, pick the provider and model you want for issue generation. Turn on `Include links` if you want the issue body to include the Jenkins build and report links when the server can publish them.

1. Click `GitHub Issue` or `Jira Ticket`. RootCoz loads the current issue prompt first; if no default prompt is available, the prompt starts empty.

1. Edit the prompt if needed and click `Continue`. RootCoz generates a preview instead of creating the issue immediately, so you can rewrite it before anything is submitted.

| Tracker | Choices you may need to make | What RootCoz generates for you |
| --- | --- | --- |
| GitHub | `Repository`, when the analysis included more than one repo | Prompt-based title, body, and similar issues |
| Jira | `Jira Project`, `Issue Type`, and optional `Security Level` | Prompt-based title, body, and similar issues |

When the server already has a Jira project key, RootCoz pre-fills it for you.

1. Review the preview carefully. Similar issues appear at the top when RootCoz can find matches, which makes it easier to reuse existing work instead of filing a duplicate.

> **Tip:** Leave Jira `Security Level` empty when the issue should stay public. If you choose `Custom...` for Jira issue type, enter the exact issue type name before creating the ticket.

1. Update the title and body until they read the way you want, then click `Create GitHub Issue` or `Create Jira Ticket`.

1. Open the new issue from the success screen. RootCoz also adds a comment on the failure with the tracker link so the report keeps the issue reference.

> **Tip:** After a bug link is added, RootCoz may ask whether the failure should be marked as reviewed.

## Advanced Usage

### Check server support and token health
```bash
rootcoz capabilities

rootcoz validate-token github --token "$GITHUB_TOKEN"
rootcoz validate-token jira --token "$JIRA_TOKEN" --email "user@example.com"
```

Run these first when a button is disabled, a token looks suspicious, or you want to confirm the server supports the tracker you plan to use.

### Inspect or override the issue prompt
```bash
rootcoz get-issue-prompt job-1

rootcoz preview-issue \
  job-1 \
  --test tests.TestA.test_one \
  --type github \
  --ai-provider claude \
  --ai-model opus-4 \
  --issue-prompt "Include product version"
```

The browser dialog lets you edit the prompt before previewing. The CLI does the same with `--issue-prompt`, and you can also switch to a different AI provider or model for the preview.

### Look up Jira project and security values
```bash
rootcoz jira-projects \
  --jira-token "$JIRA_TOKEN" \
  --jira-email "user@example.com"

rootcoz jira-security-levels PROJ \
  --jira-token "$JIRA_TOKEN" \
  --jira-email "user@example.com"
```

Use these commands when you need valid Jira project keys or security level names before creating the ticket.

### Create issues for child-job failures or with Jira-specific options
```bash
rootcoz create-issue \
  job-1 \
  --test tests.TestA.test_one \
  --type jira \
  --title "DNS timeout" \
  --body "DNS resolution fails" \
  --child-job child-runner \
  --child-build 5 \
  --jira-project-key PROJ \
  --jira-security-level Restricted \
  --jira-issue-type Story
```

Use `--child-job` and `--child-build` when the failure belongs to a pipeline child job. For GitHub, `--github-repo-url` lets you override the target repository when needed.

For the full CLI flag list, see [CLI Command Reference](cli-command-reference.html).

## Troubleshooting

- `GitHub Issue` or `Jira Ticket` is disabled in the report: the server has that tracker flow turned off. Check `rootcoz capabilities` or ask an administrator.
- The browser dialog lets you preview but not create: save your personal token first. For Jira Cloud, save the email and token together.
- Jira project search or security levels are empty: make sure your Jira token is saved and valid. Without it, RootCoz cannot populate the full Jira lists.
- `Include links` still produces plain-text references: the server is missing a public URL for report links, so RootCoz falls back to non-clickable references.
- GitHub creation fails because no repository is available: the analysis did not include a usable repo target. Re-run the analysis with repo information, or use `--github-repo-url` from the CLI.
- Jira creation fails because no project key is available: choose a project in the dialog or pass `--jira-project-key` in the CLI.
- You get `Invalid token` or an `invalid or expired` error: run `rootcoz validate-token` and then save the updated token.
- You get `User not allowed`: ask an administrator to add your username to the server allow list. See [Managing Users and API Keys](managing-users-and-api-keys.html).

## Related Pages

- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)
- [Connecting GitHub, Jira, and Report Portal](connecting-github-jira-and-report-portal.html)
- [Updating Your Profile and Notifications](updating-your-profile-and-notifications.html)
- [CLI Command Reference](cli-command-reference.html)
- [API Endpoint Reference](api-endpoint-reference.html)