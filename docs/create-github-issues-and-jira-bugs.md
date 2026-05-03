# Create GitHub Issues and Jira Bugs

You want to turn a failed test into a GitHub issue or Jira bug without rewriting the failure by hand. JJI generates a draft from the analysis, surfaces likely duplicates, and lets you file the ticket in the tracker that owns the problem.

## Prerequisites
- A completed analysis report with the failure you want to file.
- GitHub or Jira ticket creation must be available on your server. Run `jji capabilities` if you want to check before you start. See [Configuration and Environment Reference](configuration-and-environment-reference.html) for server-side setup details.
- In the browser, save a personal tracker token in `Settings`. You can preview a draft without one, but the dialog will not enable `Create` until the matching token is saved.
- For GitHub, have a personal access token with `repo` scope and a repo target.
- For Jira Cloud, have a Jira token, your Jira email, and a project key. For Jira Server/Data Center, use a Jira token and project key.

## Quick Example

```bash
jji preview-issue job-1 \
  --test tests.TestA.test_one \
  --type github \
  --github-token "<your-github-token>" \
  --github-repo-url "https://github.com/org/repo"
```

```bash
jji create-issue job-1 \
  --test tests.TestA.test_one \
  --type github \
  --title "Bug fix" \
  --body "Details..." \
  --github-token "<your-github-token>" \
  --github-repo-url "https://github.com/org/repo"
```

The preview command prints the generated title, body, and any `Similar issues`. The create command returns the new issue URL and the comment id JJI added back on the failure.

> **Tip:** Add `--json` to either command if you want machine-readable output.

The report page uses the same preview-edit-create flow directly from each failure card.

## Step-by-Step
1. Open the completed report and expand the failure you want to file.
2. Click `GitHub Issue` or `Jira Bug` on that failure.
3. Review the generated draft.
   - JJI opens a preview dialog with an editable title and body.
   - Use this step to tighten the summary, remove noise, or add extra context before you submit.
4. Check likely duplicates.
   - Review the `Similar issues` section before you create a new ticket.
   - Each result links to the existing issue and shows its current status when the tracker returns one.
   - An empty list does not block creation.
5. Choose the destination.
   - GitHub: if the analysis used more than one repo, choose the correct repo from the dropdown. If the analysis used only one repo, JJI uses it automatically.
   - Jira: enter or pick the project key that should receive the bug. If your Jira account exposes issue security levels, choose the level you want before you create.
6. Create the ticket.
   - Click `Create GitHub Issue` or `Create Jira Bug`.
   - JJI opens the new ticket and adds a comment on the failure linking back to it.

> **Note:** In the browser, preview works without a personal GitHub or Jira token, but the `Create` button stays disabled until the matching token is saved in `Settings`.


> **Tip:** Treat duplicate matching as a fast sanity check, not a hard blocker. Review the shortlist, then use your judgment.

## Advanced Usage
### Check server support first
```bash
jji capabilities --json
```

Use this when you want to confirm whether GitHub or Jira creation is enabled before you script anything. The JSON output also shows whether the server already has tracker credentials configured.

### Validate tokens and confirm Jira access
```bash
jji validate-token github --token "<your-github-token>"
jji validate-token jira --token "<your-jira-token>" --email "you@example.com"
```

```bash
jji jira-projects --jira-token "<your-jira-token>" --jira-email "you@example.com" --query PROJ
jji jira-security-levels PROJ --jira-token "<your-jira-token>" --jira-email "you@example.com"
```

Use these commands when the browser dialog does not show the Jira project you expect, or when you want to confirm access before filing a bug. For Jira Server/Data Center, omit `--email`.

### Add links or change the AI draft
```bash
jji preview-issue job-1 \
  --test tests.TestA.test_one \
  --type github \
  --include-links \
  --ai-provider claude \
  --ai-model opus-4
```

`--include-links` asks JJI to add Jenkins and analysis links to the draft. If the server does not have a public base URL configured, the draft still works, but the references fall back to plain text instead of clickable links.

If the report page shows AI provider and model selectors next to the issue buttons, you can use those instead of CLI flags to change how the draft is written.

### Create tickets for child-job failures
```bash
jji preview-issue job-1 \
  --test tests.TestA.test_one \
  --type jira \
  --child-job child-runner \
  --child-build 5
```

Use `--child-job` and `--child-build` when the failing test came from a pipeline child job rather than the top-level Jenkins job.

### Reuse CLI defaults or automate the flow
If you create tracker tickets often, store defaults such as `github_token`, `github_repo_url`, `jira_token`, `jira_email`, `jira_project_key`, and `jira_security_level` in your `jji` profile instead of repeating them on every command. See [CLI Command Reference](cli-command-reference.html) for the full option list.

> **Note:** `preview-issue` and `create-issue` only send the credentials for the selected `--type`. GitHub runs ignore Jira credentials, and Jira runs ignore GitHub credentials.

If you need to call the same flow from another tool, see [REST API Reference](rest-api-reference.html).

## Troubleshooting
- The browser dialog will not let me create the ticket: save the matching personal token in `Settings`. Preview still works without it.
- GitHub creation says no repository URL is available: the analysis did not include a repo target, so JJI has nowhere to file the issue. Re-run the analysis with repository context, or use `--github-repo-url` from the CLI. See [Copy Common Analysis Recipes](copy-common-analysis-recipes.html) for working analysis patterns.
- Jira project or issue security choices are missing: validate your Jira token first, and for Jira Cloud include your email. `jji jira-projects` and `jji jira-security-levels` show exactly what your account can access.
- The tracker says your token is invalid or expired: run `jji validate-token` and update the saved token or CLI flag.
- `Similar issues` is empty: duplicate lookup is best effort. You can still review the draft and create the ticket manually.
- The GitHub or Jira button is disabled, or `jji capabilities` shows the tracker as unavailable: the integration is turned off on the server. See [Configuration and Environment Reference](configuration-and-environment-reference.html) for the server-side requirements.

## Related Pages

- [Review and Classify Failures](review-and-classify-failures.html)
- [Configure Your Profile and Notifications](configure-your-profile-and-notifications.html)
- [CLI Command Reference](cli-command-reference.html)
- [REST API Reference](rest-api-reference.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)