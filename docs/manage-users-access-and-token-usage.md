# Manage Users, Access, and Token Usage

You want to run JJI as a shared service without passing one long-lived admin secret around the team. The safest pattern is to use `ADMIN_KEY` only to bootstrap named admins, optionally restrict write access with `ALLOWED_USERS`, and review AI token usage from the admin tools.

## Prerequisites
- Access to the JJI server configuration so you can set environment variables and restart or redeploy the service.
- A running JJI instance you can reach in a browser or with `jji`.
- `jji` installed if you want to use the CLI examples.
- Either the server `ADMIN_KEY`, or an existing named admin key.

## Quick Example

```dotenv
ADMIN_KEY=change-this-admin-secret
ALLOWED_USERS=alice,bob,carol
SECURE_COOKIES=true
```

```bash
export JJI_SERVER=https://jji.example.com
export JJI_API_KEY="$ADMIN_KEY"

jji admin users create alice
jji admin users list
jji admin token-usage
```

That is enough to bootstrap a shared instance, create the first named admin, and open the admin-only AI token usage tools.

## Step-by-Step

1. Set the bootstrap key and, if needed, the writer allow list.

```dotenv
ADMIN_KEY=change-this-admin-secret
ALLOWED_USERS=alice,bob,carol
SECURE_COOKIES=true
```

Restart or redeploy JJI after changing server environment variables. Keep `ALLOWED_USERS` empty when you want open write access.

| `ALLOWED_USERS` value | Result |
| --- | --- |
| empty | Anyone with a username can submit or modify data |
| `alice,bob,carol` | Only those users can submit or modify data |
| any value | Admin users still bypass the allow list |

> **Note:** The allow list is case-insensitive and affects write actions only. Read-only pages and queries still work.

2. Use the bootstrap admin once to create a named admin.

```bash
jji --server https://jji.example.com --api-key "$ADMIN_KEY" admin users create alice
```

JJI prints the new admin key once. Save it immediately and hand it to the person who will own that account.

> **Warning:** JJI does not let you retrieve an admin key later. If it is lost, rotate it and distribute the replacement.

If you prefer the browser, sign in at `/register` as `admin` with the same `ADMIN_KEY`, then open `Users` and choose `Create Admin`.

3. Switch day-to-day admin work to the named admin key.

```bash
export JJI_SERVER=https://jji.example.com
export JJI_API_KEY="paste-alice-key-here"

jji auth whoami
jji admin users list
```

Use named admin keys for normal operations instead of sharing `ADMIN_KEY`. In the browser, the same key works at `/register` with username `alice`.

> **Note:** `jji auth login` validates credentials, but later CLI commands only keep admin access when you pass `--api-key`, set `JJI_API_KEY`, or save `api_key` in CLI config.

Once the browser recognizes an admin, `Users` and `Token Usage` appear in the navigation.

4. Promote, demote, rotate, or remove admin access.

```bash
jji admin users list
jji admin users change-role bob admin
jji admin users rotate-key alice
jji admin users change-role carol user
jji admin users delete oldadmin --force
```

`jji admin users list` shows named admins and regular users JJI has already seen. Promote a regular user when they need admin access. Rotate a key when you suspect exposure or during a handoff.

| Task | Result |
| --- | --- |
| Promote to `admin` | JJI generates a new admin key |
| Demote to `user` | The admin key is revoked and active admin sessions end |
| Rotate an admin key | The old key stops working immediately |
| Delete a named admin | The account is removed and active admin sessions end |

> **Tip:** There is no separate “create regular user” command. If the person you want to promote is not listed yet, have them open JJI once and save a username.

5. Monitor AI token usage from the browser or the CLI.

```bash
jji admin token-usage
jji admin token-usage --group-by provider
jji admin token-usage --job-id abc-123
```

`Token Usage` is admin-only. The browser view shows Today, Last 7 Days, and Last 30 Days, plus top models, top jobs, and a breakdown table you can group by model, provider, call type, day, week, month, or job.

The default CLI summary reports the same rolling windows. Use `--job-id` when you want the call-by-call breakdown for one analysis.

## Advanced Usage

### Save admin auth in CLI config

```toml
[default]
server = "prod"

[servers.prod]
url = "https://jji.example.com"
username = "alice"
api_key = "paste-admin-key-here"
```

After that, commands like `jji admin users list` and `jji admin token-usage --group-by model` use the saved profile automatically.

> **Warning:** Treat `~/.config/jji/config.toml` as sensitive. It can contain live admin credentials.

### Rotate the bootstrap `ADMIN_KEY`

```dotenv
ADMIN_KEY=replace-this-bootstrap-secret
```

Create and verify at least one named admin first, then change `ADMIN_KEY` on the server and restart or redeploy JJI. Rotating `ADMIN_KEY` only changes the reserved `admin` login; existing named admin keys keep working.

### Filter or export AI token usage

```bash
jji admin token-usage --period month --group-by model
jji admin token-usage --provider claude --group-by job
jji admin token-usage --group-by provider --format csv
```

Use `--period` for quick rolling windows, `--group-by` for spend breakdowns, and `--format csv` when you want to export the grouped data.

See [CLI Command Reference](cli-command-reference.html) for the full `jji admin ...` option list. See [Configuration and Environment Reference](configuration-and-environment-reference.html) for all server-side settings. See [REST API Reference](rest-api-reference.html) if you want to automate the same tasks.

## Troubleshooting

- **`Invalid username or API key`**: For the bootstrap login, the username must be exactly `admin`. For named admins, the username and key must belong to the same account.
- **`Admin access required`**: Your browser session is not admin, or the CLI request is missing `--api-key` or `JJI_API_KEY`.
- **`User not allowed`**: The username is not in `ALLOWED_USERS`. Add it to the allow list, or use an admin key.
- **The user you want to promote is missing**: Have them open JJI once and save a username, then refresh `Users`.
- **A demote or delete action is blocked**: JJI does not let you change or delete your own active admin account, and it will not let you remove the last admin.
- **Username creation fails**: Managed usernames must be 2-50 characters, start with a letter or digit, and may include `.`, `_`, and `-`. The name `admin` is reserved.
- **Browser admin login does not stick on local HTTP**: `SECURE_COOKIES` may still be `true`. Use HTTPS, or set it to `false` only for local HTTP development.

## Related Pages

- [Configure Your Profile and Notifications](configure-your-profile-and-notifications.html)
- [Organize Jobs with Metadata](organize-jobs-with-metadata.html)
- [CLI Command Reference](cli-command-reference.html)
- [REST API Reference](rest-api-reference.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)