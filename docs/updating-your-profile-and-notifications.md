# Updating Your Profile and Notifications

Use `Settings` to change the name RootCoz uses for you, refresh saved GitHub and Jira credentials, and turn mention alerts on or off without breaking your access. The same flow also lets you add admin access only when you need it.

## Prerequisites
- A saved RootCoz username, or access to the welcome/setup form if this is your first time.
- Your GitHub personal access token if you want RootCoz to use your GitHub identity.
- Your Jira token, and your Atlassian email if you use Jira Cloud.
- An API key for an admin account if you need admin access. See [Managing Users and API Keys](managing-users-and-api-keys.html) for details.
- If you still need to generate tracker tokens, see [Connecting GitHub, Jira, and Report Portal](connecting-github-jira-and-report-portal.html).

## Quick Example
1. Click the `Settings` gear in your user badge.
2. Change `Username`.
3. Paste a new value into `GitHub Token`.
4. Click `Save`.
5. In `Push Notifications`, click `Enable`.

This is enough to keep your profile current and start getting browser alerts when someone mentions you in a comment.

## Step-by-Step
1. Open the profile form.  
   If you are already signed in, click the `Settings` gear in the user badge. If you are not signed in yet, use the same form on the welcome screen.

2. Update your username.  
   Enter the name you want RootCoz to use for comments, mentions, and personal settings.

   > **Warning:** The username `admin` is reserved. Use it only with the API key that belongs to `admin`.

3. Save your GitHub and Jira credentials.  
   Fill in only the fields you use:
   - `GitHub Token`: GitHub personal access token with `repo` scope.
   - `Jira Email`: required for Jira Cloud; use the same email as your Atlassian account.
   - `Jira Token`: Jira Cloud API token or Jira Server/Data Center personal access token.

   Click `Save`. When RootCoz can validate a non-empty token, it shows an `Authenticated as ...` message under that field. If you update only one token, RootCoz leaves the other saved tracker fields alone.

   > **Note:** Saved GitHub and Jira credentials stay in this browser and are also synced to the server with encryption at rest, so RootCoz can restore them in another browser after you sign in with the same username.

4. Add or refresh admin access.  
   In `Admin Authentication`, enter the `Username` and `API Key` that belong together, then click `Save`.

   If the login succeeds, RootCoz saves that username and starts an admin session. The `Settings` page shows your role badge when that session is active.

5. Turn browser notifications on or off.  
   After you have saved a username once, use the `Push Notifications` section:
   - Click `Enable` to allow mention alerts.
   - Click `Disable` to stop them.
   - If notifications are already blocked in your browser, change the site permission first, then return to RootCoz.

   RootCoz only sends browser notifications when someone mentions you in a comment. See [Commenting and Mentioning Teammates](commenting-and-mentioning-teammates.html) for the mention workflow.

6. Confirm the result.  
   After you save, RootCoz returns you to the dashboard. Reopen `Settings` if you want to verify your username, token status, admin badge, or notification state.

## Advanced Usage
> **Tip:** RootCoz may also show an `Enable Notifications?` prompt on the dashboard. If you click `Not now`, the pop-up stops appearing, but you can still enable notifications later from `Settings`.

| Change | What RootCoz does |
| --- | --- |
| Save one tracker token | Keeps the other saved tracker fields unchanged. |
| Leave a tracker field blank | Keeps the existing saved server copy instead of deleting it. |
| Save your own tracker token on a server that already has shared credentials | Uses your personal token for your tracker actions. |
| Log out | Clears the local username and local token copy in that browser. |
| Sign in again with the same username | Reloads saved tracker tokens from the server when they are available. |
| Keep using an admin session | Renews the admin session automatically while you stay active. |

A few extra cases are worth knowing:
- On a first-time profile setup, you save the username first and then turn on notifications after RootCoz returns you to the dashboard.
- Jira Cloud works best when `Jira Email` and `Jira Token` are saved together.
- Email-style usernames work normally. Use the raw value, not a URL-encoded version such as `user%40example.com`.

## Troubleshooting
| Problem | What to do |
| --- | --- |
| `The username 'admin' is reserved` | Use a different username, or enter the API key that belongs to `admin` and save again. |
| `Invalid username or API key` | Make sure the `Username` matches the API key you were given. |
| GitHub or Jira validation fails | Check that the token is current, that the GitHub token has `repo` scope, and that Jira Cloud includes the correct `Jira Email`. If Jira says it is not configured, ask your administrator to enable Jira on the server. |
| `Push notifications are not supported in this browser` | Use a browser that supports browser push notifications. |
| `Notifications blocked` | Re-enable notifications for the site in your browser settings, then return to `Settings` and click `Enable` again. |
| Notifications fail in Brave | Turn on `Use Google services for push messaging` in Brave, then try `Enable` again. |
| You logged out and your tokens seem gone | Sign in again with the same username. RootCoz reloads your saved tracker tokens from the server when it can. |

## Related Pages

- [Commenting and Mentioning Teammates](commenting-and-mentioning-teammates.html)
- [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html)
- [Connecting GitHub, Jira, and Report Portal](connecting-github-jira-and-report-portal.html)
- [Managing Users and API Keys](managing-users-and-api-keys.html)
- [Run Your First Analysis](run-your-first-analysis.html)