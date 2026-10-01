# Managing Your Account and Notifications

This page covers everything you control about your own RootCoz identity: registering, the API key you log in with, the personal tracker tokens and AI keys stored against your account, and how you get told when someone mentions you in a comment.

## Prerequisites

- A running RootCoz server. See [Quickstart](quickstart.html) or [Deploying RootCoz](deploy-rootcoz.html) for details.
- For the tracker tokens, a GitHub personal access token with the `repo` scope, or a Jira Cloud API token (plus your Atlassian account email) or a Jira Server/Data Center personal access token.
- For browser push notifications, a browser that supports Web Push (Chrome, Edge, Firefox, or Safari on a supported platform) and permission to show notifications.

## Quick Example

1. Open `http://localhost:8000` and click `Register`.
2. Enter a username, click `Register`, then copy the API key from the `Registration Complete` screen.
3. Open `Settings`, paste a `GitHub Token`, and click `Save`.
4. Under `Push Notifications`, click `Enable` to receive browser notifications when you are mentioned.

The registration key is the only credential that signs you in; the tracker token is what lets RootCoz create GitHub or Jira issues under your name.

## Step-by-Step

1. **Register an account.**

   Registration takes a username and nothing else — there is no password. Usernames are lowercased before storage, so `JDoe` and `jdoe` are the same account.

   | Rule | Behaviour |
   | --- | --- |
   | Reserved names | Any username starting with `rootcoz` is rejected. This protects the reserved AI identity `rootcoz-ai`, which RootCoz uses for its own comments and chat. |
   | Allowed users | If the server sets `allowed_users`, registration fails with `Registration is restricted. Contact an admin.` unless your username is on the list. |
   | New role | The account gets `DEFAULT_USER_ROLE` (`reviewer` unless the server changes it). |
   | Approval | With `REQUIRE_APPROVAL=true` the account is created as `pending` and cannot log in until an admin approves it. |

   You can optionally supply `GitHub Token`, `Jira Email`, and `Jira Token` on the registration form. They are saved with the account and reused on the `Settings` page.

2. **Save the API key.**

   Registration returns the key once, on the `Registration Complete` screen, under the warning `Save this API key — you won't see it again!`. Click `Copy`, store it in a password manager, then click `I've saved my key — Continue`.

   | Detail | Value |
   | --- | --- |
   | Key format | `rootcoz_` followed by a random token |
   | Storage on the server | Only an HMAC-SHA256 hash, so the raw key cannot be recovered |
   | Reused for | Logging in and as the API credential for the CLI |

   > **Warning:** The key is displayed exactly once. If you lose it, an admin has to generate a new one for you. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.

3. **Log in and manage sessions.**

   `Log in` takes the same username plus the API key. On success RootCoz sets an HTTP-only `rootcoz_session` cookie; `Log out` deletes the session server-side and clears the cookie.

   | Session behaviour | Detail |
   | --- | --- |
   | Lifetime | `SESSION_TTL_HOURS`, default 720 hours (30 days) |
   | Cookie flags | `httponly`, `samesite=lax`, and `secure` when the server sets `secure_cookies` |
   | Key rotation | Invalidates every session for that username, then issues a fresh session so you stay signed in |
   | `can_view_reports` | Re-read from the users table on each request, so an admin grant takes effect without signing in again |

   Behind a trusted identity proxy the server can also accept an `X-Forwarded-User` header instead of a key; that behaviour is controlled by the `trust_proxy_headers` server setting.

4. **Keep your profile and tracker tokens current.**

   `Settings` shows your username as read-only, followed by an `API Key` section, `Tracker Tokens`, and `Push Notifications`.

   | Field | What it is used for |
   | --- | --- |
   | `GitHub Token` | Personal access token with `repo` scope. Used to create GitHub issues and to enrich comments with PR status. |
   | `Jira Email` | Your Atlassian account email, required for Jira Cloud API-token auth. |
   | `Jira Token` | Jira Cloud API token, or a personal access token on Jira Server/Data Center. |

   Validation happens through `POST /api/validate-token`, and a failed validation leaves the stored value unchanged. The two entry points differ, though. The Settings form (`ProfileForm.handleSubmit`) validates any non-empty GitHub or Jira token that is not already known-good and **aborts the save** if the check fails, so a bad token typed there is never stored. The registration flow does not: `RegisterPage` sends whatever you typed straight to `PUT /api/user/tokens`, which stores it as supplied. A mistyped or already-revoked token entered at registration therefore persists and only fails later, when a tracker action tries to use it. They are kept in your browser for convenience and synced to the server encrypted at rest, so a second browser picks them up automatically.

   > **Warning:** Stored tokens cannot be cleared from this page or from the API. `PUT /api/user/tokens` treats an empty field as "keep what is already stored" and merges it back over your existing values, and a request with all three fields empty is dropped without saving. Clearing every field in the UI is skipped outright for the same reason. A stored GitHub or Jira credential therefore stays on the server until it is overwritten by a different value, and there is no `DELETE` route for it. If you need the credential gone, register a fresh account or ask an administrator to delete the account with `DELETE /api/admin/users/{username}`; the tokens are columns on the `users` row, so removing the row removes them.

   > **Tip:** Without a tracker token you can still preview generated issue content, but you cannot submit it. See [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html) for details.

5. **Rotate your own API key.**

   In `Settings`, under `API Key`, click `Rotate API Key`. The old key stops working immediately, all your other sessions are signed out, and the new key is shown once with a `Copy` button.

   Two things to know before you rotate:

   - The bootstrap `admin` account cannot rotate through this page. Its key is the `ADMIN_KEY` environment or server setting.
   - Any script or CLI config using the old key breaks until you update it. See [CLI Command Reference](cli-reference.html) for details.

6. **Store your own AI provider keys (reviewer and above).**

   If your role is not `viewer`, `Settings` also shows an `AI provider keys` card. Pick a provider, pick the model to verify against (type the model ID by hand if it is not in the list), paste the key, and save.

   | Outcome | What happens |
   | --- | --- |
   | `accepted` | Key stored for that provider; existing keys are never shown again |
   | `rejected` | Existing credential left unchanged; check the key and the selected model |
   | `inconclusive` | Provider could not be reached; retry when it is available |

   Use `Replace` to swap a key for a provider that already has one, or `Remove key` to delete it — removing a key also revokes the AI sessions that used it. Keys are limited to 8–1024 characters and are stored encrypted on the server.

   > **Note:** The `Use server credentials` toggle on `New Analysis` is a separate, admin-controlled permission. Without that grant you must bring your own provider key. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.

7. **Get notified when you are mentioned.**

   Mentions are plain `@username` text in job comments. RootCoz scans each new comment for `@username` tokens, ignoring anything that looks like an email address, and the mentioned user gets an unread badge in the sidebar.

   | Where | What it does |
   | --- | --- |
   | `Mentions` page | Lists every comment that mentions you, newest first, with an unread count and `Mark all as read`. Opening a mention marks it read and jumps to the exact comment on the report. |
   | Sidebar badge | Live unread count, pushed over the `/api/navbar/stream` SSE channel. |
   | `Push Notifications` toggle | Browser notifications titled `Mentioned by @author`. The notification link is built as `/results/{job_id}`, so a tap opens the report for that job. Use the `Mentions` page or the sidebar badge until that is corrected. |

   Enabling push notifications asks the browser for permission and registers the device with the server. Delivery is best effort: a subscription the push service reports as gone is deleted automatically, and at most 10 subscriptions are kept per user.

   > **Note:** Push notifications need VAPID keys. RootCoz generates a key pair automatically when none are configured, so the toggle normally works out of the box. An administrator can also set them under `Web Push` in Server Settings. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.

## Advanced Usage

- **Check who can be mentioned.** The mention picker on a comment lists existing usernames. Mention detection is word-boundary aware, so `@jdoe` does not match `@jdoe_extra`.
- **Self-mentions are not pushed.** If you mention yourself in a comment, no browser notification is sent to you.
- **Mention endpoints.** `GET /api/users/mentions` supports `offset`, `limit`, and `unread_only`. `POST /api/users/mentions/read` marks a specific list of comment IDs, and `POST /api/users/mentions/read-all` clears the badge. See [API Endpoint Reference](api-reference.html) for details.
- **Public auth endpoints.** `/api/auth/needs-key` reports whether the current browser already has a key, and `/api/auth/pending-status` returns the waiting message shown to pending accounts. Neither requires a session.
- **Custom pending message.** Administrators can set `admin_wait_approve_msg`; if present, it appears on the pending page and on a blocked login attempt.

## Troubleshooting

- Registration fails with `Usernames starting with 'rootcoz' are reserved for system use`.  
  Pick a username that does not start with `rootcoz`. The `rootcoz-ai` identity is reserved for the AI.

- Registration fails with `Registration is restricted. Contact an admin.`  
  The server has an allow list configured. Ask an admin to add your username, or have them create the account for you.

- Registration succeeds but `Log in` says `Your account is awaiting admin approval.`  
  `REQUIRE_APPROVAL` is on. An admin has to approve you from the Users page. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.

- Login says `Invalid username or API key.`  
  The username must match the registered one (case-insensitive), or you are using a key that was rotated. If the admin rotated `ROOTCOZ_ENCRYPTION_KEY`, every stored key hash is invalidated and all users must be given new keys.

- `Rotate API Key` is missing, or returns `Bootstrap admin cannot rotate key via this endpoint.`  
  You are signed in as the bootstrap `admin`. That key is managed through the `ADMIN_KEY` setting, not the UI.

- Push notifications show `Push notifications are not configured on server.`  
  Web Push is off. An admin must configure both `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY`, either under `Web Push` in Server Settings or in the environment. Keys are resolved in the order Server Settings, then environment, then an auto-generated key file, so a key saved in Server Settings is the one used for signing. Setting only one of the pair is treated as unset.

- Push notifications show `Notifications blocked.`  
  Your browser denied permission for this site. Re-enable notifications in the browser's site settings, then click `Enable` again.

- A stored GitHub or Jira token disappeared after a server restart.  
  Tokens are encrypted with `ROOTCOZ_ENCRYPTION_KEY`. If that key changed, previously stored tokens can no longer be decrypted — enter them again.

- Your CLI or a script suddenly returns `401`.  
  Your key was rotated or an admin rotated it for you, which signs out every session for that user. Use the newest key.

## Related Pages

- [Managing Users and Server Settings](manage-users-and-server-settings.html)
- [Quickstart](quickstart.html)
- [Collaborating on Results](collaborate-on-results.html)
- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [API Endpoint Reference](api-reference.html)