# Configure Your Profile and Notifications

Set the `Username` JJI should use for you, save your own GitHub or Jira credentials, and turn on browser alerts for `@mentions`. This keeps follow-up work tied to your account and helps you catch comment pings without watching the page.

## Prerequisites
- Access to the JJI web UI.
- A GitHub personal access token with `repo` scope if you want GitHub issue creation from the web UI.
- For Jira Cloud, your Atlassian email plus an API token. For Jira Server/Data Center, a personal access token.
- A browser that supports notifications if you want `@mention` alerts.

## Quick Example
```bash
jji validate-token github --token "<your-github-token>"
```

If the command prints `Valid`, open `/register`, enter a `Username`, paste the token into `GitHub Token`, and click **Save**. After you land on the dashboard, accept the notification prompt, or open `/settings` later and enable notifications there.

> **Tip:** Choose a simple username like `jdoe`, `jdoe-dev`, or `jdoe_1` if you want reliable `@mentions`. Email-style names do not work well with mentions.

## Step-by-Step
1. Open your profile page.

   Use `/register` the first time. After that, click the settings icon in the user badge and open `/settings`.

> **Note:** `API Key` is separate from your GitHub and Jira tokens. Leave it empty unless your administrator gave you an admin key.

2. Enter your `Username`.

   JJI uses this name for comments, the `Mentions` list, and the attribution it adds when it creates a GitHub issue or Jira bug for you.

3. Fill in only the tracker fields you need.

| You want to do | Fill in | What to use |
| --- | --- | --- |
| Create GitHub issues from the web UI | `GitHub Token` | GitHub personal access token with `repo` scope |
| Create Jira bugs on Jira Cloud | `Jira Email` and `Jira Token` | Your Atlassian account email and API token |
| Create Jira bugs on Jira Server/Data Center | `Jira Token` | A personal access token, with `Jira Email` left blank |

4. Click **Save**.

   JJI validates new or changed tracker tokens when you save. When a token checks out, the form shows `Authenticated as ...`.

> **Note:** JJI saves tracker tokens in the current browser and also syncs them to the server, encrypted at rest, so they can be restored when you use the same username in another browser.

5. Enable browser notifications.

   After you already have a username, JJI can show an `Enable Notifications?` prompt on the dashboard. If you skip it, open `/settings` and use the `Push Notifications` section to enable or disable notifications manually.

6. Check your mentions.

   When someone mentions your username in a comment, JJI can show a browser notification and increments the unread badge on `Mentions`. Open `Mentions` to review the list and mark items as read.

## Advanced Usage
```bash
jji validate-token jira --token "<your-jira-token>" --email "you@example.com"
jji validate-token jira --token "<your-server-or-dc-token>"
jji --json validate-token github --token "<your-github-token>"
```

Use `--email` for Jira Cloud and leave it out for Jira Server/Data Center. Add `--json` when you want machine-readable output. See [CLI Command Reference](cli-command-reference.html) for more command examples.

| Setting | Shared across browsers? | Notes |
| --- | --- | --- |
| Saved GitHub or Jira tokens | Yes, when you use the same username | JJI can restore the saved server copy for that username into another browser. |
| Notification permission and push subscription | No | Enable notifications separately in each browser or device. |
| Automatic dashboard prompt | No | Clicking `Not now` is remembered only in the current browser. |

Saving only one tracker field later does not erase the other saved tracker fields. Self-mentions still appear in `Mentions`, but they do not trigger a push notification.

For automation outside the web UI, see [REST API Reference](rest-api-reference.html).

## Troubleshooting
- **`The username 'admin' is reserved`:** use a different username unless you have an admin API key.
- **GitHub or Jira validation says the token is invalid:** generate a fresh token and try again. For Jira Cloud, make sure `Jira Email` matches the same Atlassian account as the token.
- **You never see a notification prompt:** open `/settings` and check `Push Notifications` directly. The automatic prompt only appears once per browser, and only when browser notifications are available.
- **Enabling notifications says they are not configured on the server:** browser notifications are not enabled on this JJI server yet. Ask your administrator to check the notification setup; see [Configuration and Environment Reference](configuration-and-environment-reference.html).
- **Notifications are blocked:** re-enable this site's notification permission in your browser settings, then try again from `/settings`.
- **Notifications fail in Brave with a push service error:** turn on `Use Google services for push messaging` in Brave, then retry.
- **Saved tokens do not show up in another browser:** make sure you used the same username there. Saved tracker credentials are tied to the username you chose.
- **People cannot `@mention` you reliably:** switch to a simple username such as `jdoe`, `jdoe-dev`, or `jdoe_1` instead of an email-style name.

## Related Pages

- [Review and Classify Failures](review-and-classify-failures.html)
- [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html)
- [CLI Command Reference](cli-command-reference.html)
- [REST API Reference](rest-api-reference.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)