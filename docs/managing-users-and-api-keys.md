# Managing Users and API Keys

Use this page when you need to give someone admin access, replace an admin key without surprises, or limit write access to a known set of users. The goal is to keep RootCoz usable for your team while making access changes predictable and easy to recover from.

This page covers RootCoz admin access keys only. For GitHub, Jira, and Report Portal credentials, see [Connecting GitHub, Jira, and Report Portal](connecting-github-jira-and-report-portal.html).

## Prerequisites

- A RootCoz server URL.
- A current admin credential: either the server's `ADMIN_KEY` or an existing delegated admin API key.
- The `rootcoz` CLI for command-line examples, or browser access to the RootCoz UI.
- Permission to update server environment variables and restart RootCoz if you need to change `ADMIN_KEY`, `ALLOWED_USERS`, `TRUST_PROXY_HEADERS`, or `SECURE_COOKIES`.

## Quick Example

```bash
export ROOTCOZ_SERVER="https://rootcoz.example.com"
export ROOTCOZ_API_KEY="your-current-admin-key"

rootcoz auth whoami
rootcoz admin users create newadmin
rootcoz admin users list
```

This authenticates the CLI for the current shell, creates a delegated admin named `newadmin`, and shows the users the server currently knows about.

## Step-by-Step

1. Authenticate as an admin.

```bash
export ROOTCOZ_SERVER="https://rootcoz.example.com"
export ROOTCOZ_API_KEY="your-current-admin-key"

rootcoz auth whoami
```

| What you have | How to use it |
| --- | --- |
| server `ADMIN_KEY` | Sign in in the browser as username `admin`, or pass the key with `ROOTCOZ_API_KEY` or `--api-key` in the CLI |
| delegated admin API key | Sign in in the browser with that admin's username, or pass the key with `ROOTCOZ_API_KEY` or `--api-key` in the CLI |

In the browser, enter the admin username and API key on the setup screen or in `Settings`. After a successful admin sign-in, the `Users` section becomes available.

> **Note:** Use the server `ADMIN_KEY` to bootstrap or recover access. For day-to-day admin work, create named admin users and use their delegated keys instead.

2. Create a delegated admin.

```bash
rootcoz admin users create newadmin
```

This creates a named admin account and returns a fresh API key once. Save it immediately before you close the terminal or dialog.

In the browser, open `Users`, choose **Create Admin User**, enter the username, and copy the key before closing the dialog.

> **Tip:** Create at least two delegated admins. RootCoz will not let you demote or delete the last admin in the user list.

3. Rotate a delegated admin API key.

```bash
rootcoz admin users rotate-key newadmin
```

Rotation replaces the user's old admin key immediately. Any current admin browser sessions for that user are also ended, so update saved CLI credentials or automation right away.

> **Warning:** The replacement key is shown once. If it is lost, rotate the key again and redistribute the new value.

4. Promote or demote a user.

```bash
rootcoz admin users change-role alice admin
rootcoz admin users change-role alice user
```

Promoting a known user to `admin` generates a delegated admin key for that user. Demoting an admin back to `user` removes their admin key and ends their current admin sessions.

Use promotion when the person already appears in the user list. If they are brand new, ask them to open RootCoz once so they appear in the list, or create them directly as an admin instead.

5. Control who can submit or modify data.

```bash
ALLOWED_USERS=alice,bob,carol
```

Set `ALLOWED_USERS` in the server environment and restart RootCoz. When it is empty, write access is open. When it is set, only the listed users can perform write actions such as submitting analyses, adding comments, marking tests reviewed, and overriding classifications.

| `ALLOWED_USERS` | Who can write |
| --- | --- |
| empty or not set | Any user with a username |
| `alice,bob,carol` | `alice`, `bob`, or `carol` |
| any value, authenticated as admin | Admins always bypass the allow list |

Matching is case-insensitive, so `Alice` and `alice` are treated the same. Read-only views are not restricted by the allow list.

For regular CLI usage, include a username with `--user` or `ROOTCOZ_USERNAME`. Without that identity, write actions can fail when the allow list is enabled.

## Advanced Usage

- Save an admin key in a CLI profile:

```toml
[servers.prod]
url = "https://rootcoz.example.com"
api_key = "rootcoz_your_admin_key"
```

```bash
rootcoz --server prod admin users list
```

This is useful if you administer the same server often. After a key rotation, update the saved `api_key` before you run the next admin command. See [CLI Command Reference](cli-command-reference.html) for the full flag list.

- Reduce or remove admin access:

```bash
rootcoz admin users change-role oldadmin user
rootcoz admin users delete oldadmin
```

Demote a person to `user` if they should keep using RootCoz without admin privileges. Delete is for delegated admin accounts that should be removed entirely.

- Let a trusted proxy identify regular users automatically:

```bash
TRUST_PROXY_HEADERS=true
```

Enable this only behind a trusted reverse proxy that sends `X-Forwarded-User`. RootCoz will use that username for normal users automatically, but it will not accept a proxy-supplied `admin` as the bootstrap admin account.

- Understand what each access change does:

| Action | Immediate effect |
| --- | --- |
| create admin | Generates a new delegated admin key |
| rotate key | Old key stops working and current admin sessions for that user end |
| promote user to admin | Generates a delegated admin key |
| demote admin to user | Revokes the admin key and ends current admin sessions for that user |
| delete admin | Removes delegated admin access and ends current sessions for that user |

## Troubleshooting

- `Cannot demote the last admin user` or `Cannot delete the last admin user`: create another delegated admin first, then retry.
- `User 'name' not found` when changing roles: the user has not appeared in RootCoz yet. Ask them to open RootCoz once, or create them directly as an admin.
- `Invalid username or API key` with the bootstrap key: in the browser, use username `admin`. With a delegated key, use the matching admin username.
- `Cannot delete your own account` or `Cannot change your own role`: sign in as a different admin and make the change from that account.
- `User not allowed. Contact an administrator to be added to the allow list.`: add the username to `ALLOWED_USERS`, or make sure the CLI command includes `--user` or `ROOTCOZ_USERNAME`.
- Browser admin login does not stick on local HTTP: set `SECURE_COOKIES=false` for local development, restart RootCoz, and sign in again.

## Related Pages

- [Updating Your Profile and Notifications](updating-your-profile-and-notifications.html)
- [Configuration Reference](configuration-reference.html)
- [CLI Command Reference](cli-command-reference.html)
- [API Endpoint Reference](api-endpoint-reference.html)
- [Automating Common Tasks with the CLI](automating-common-tasks-with-the-cli.html)