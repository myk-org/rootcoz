# Managing Users and Server Settings

This page is for RootCoz administrators. It covers the role ladder, what each role is allowed to do, the day-to-day user management actions, and the Server Settings page — including which values are masked and which need a restart before they take effect.

## Prerequisites

- You are signed in as an `admin`.
- The bootstrap `admin` account exists. It authenticates with the username `admin` and the `ADMIN_KEY` value, and it lives outside the users table, so it is not affected by user deletion. It exists **only** while `ADMIN_KEY` is non-empty: login compares the supplied key against `ADMIN_KEY` and refuses an empty one, so a deployment left at the empty default has no bootstrap path into admin at all. Set `admin.key` in the Helm chart, which auto-generates a key on first install when the value is empty — `chart/README.md` shows how to read it back out of the Secret.
- For server settings changes, you have a current server value for anything you are changing, because sensitive values are stored masked.

## Quick Example

1. Open `Users` from the admin sidebar.
2. Click `Add User`, enter a username, pick `operator`, and click `Create`.
3. Copy the API key from the confirmation dialog and send it to the new user over a channel you trust.
4. Open `Settings` in the admin sidebar, expand a category, change a value, and save.

That creates an active operator who can submit analyses, and one server setting change without a restart.

## Step-by-Step

1. **Understand the role ladder.**

   There are four roles. Each one is everything below it plus its own column.

   | Capability | `viewer` | `reviewer` | `operator` | `admin` |
   | --- | --- | --- | --- | --- |
   | View jobs and results | yes | yes | yes | yes |
   | Chat about a job | no | yes | yes | yes |
   | Comment on a job, mentions | no | yes | yes | yes |
   | Rotate own API key, manage own tokens | no | yes | yes | yes |
   | Submit a new analysis (`POST /analyze`, ingest-only `POST /submit`) | no | no | yes | yes |
   | Re-analyze any job | no | no | yes | yes |
   | Delete own jobs | no | no | yes | yes |
   | Delete any job | no | no | no | yes |
   | Access `Reports` | only with the reports grant | only with the reports grant | only with the reports grant | always |
   | Create and delete users, change roles, grant reports access | no | no | no | yes |
   | Rotate another user's API key | no | no | no | yes |
   | Access `/api/admin/*` (Users, Tokens, Logs, Settings, admin Chat, DB tools) | no | no | no | yes |

   Two flags sit outside the role ladder:

   | Flag | Default | Meaning |
   | --- | --- | --- |
   | `can_view_reports` | off | Grants access to `/api/reports/*` and the `Reports` page to a non-admin. It is orthogonal to role: a `viewer` with the grant can read reports, and an `operator` without it cannot. For non-admins the flag is re-read from the users table on every request, so a grant takes effect without invalidating sessions. Admins always have effective access regardless of the stored value. |
   | `can_use_server_providers` | off | Grants the `Use server credentials` toggle on `New Analysis`, which lets the user run analyses on the server's AI keys instead of their own. Admins always have it. |

   > **Note:** `DEFAULT_USER_ROLE` decides the role new registrations get, and it accepts only `viewer`, `reviewer`, or `operator` — it cannot grant `admin`. Set it before startup if you want self-registered users to submit analyses immediately.

2. **Create a user.**

   On `Users`, click `Add User`. The dialog takes a username, a role, and two switches: `Allow viewing reports` and `Allow server providers`. Click `Create`.

   | Detail | Behaviour |
   | --- | --- |
   | Username rules | Lowercased, must be non-empty, and must not start with `rootcoz` (that prefix is reserved, including the AI identity `rootcoz-ai`). |
   | Status | The account is created as `active` — admin-created users never go through approval. |
   | API key | Returned once in the response and shown once in the dialog. Copy it before closing. |
   | Reports flag | Stored exactly as submitted, even for an `admin`. Demoting an admin later does not leave an accidental grant behind. |
   | Your session | Creating a user does not change or replace your own session. |

3. **Change a role.**

   Use the role dropdown in the user's row. Every user except yourself and the reserved `admin` account can be changed; the server rejects `Cannot change your own role`. Confirm in the `Change Role to …` dialog.

   | Transition | What happens |
   | --- | --- |
   | To `admin` | If the user has no API key yet, one is generated and shown once. Otherwise the existing key is kept. |
   | Any other change | The existing API key is preserved. |

4. **Approve or reject a pending registration.**

   Pending users appear with a `pending` badge and are counted in the `{n} pending` pill next to the `User Management` heading.

   | Action | Effect |
   | --- | --- |
   | `Approve` | Sets the status to `active` so the user can log in. The approval dialog also decides whether that user may use server AI credentials. |
   | `Reject` | Sets the status to `rejected`. The user cannot log in and is told to contact an admin. |

   Pending users only exist when the server runs with `REQUIRE_APPROVAL=true`.

5. **Rotate another user's key.**

   Use the key icon in the user's row. The dialog warns `The old key will stop working immediately.` and, on completion, shows the new key once with a copy button.

   | Detail | Behaviour |
   | --- | --- |
   | Effect on the old key | Invalidated at once. |
   | Effect on sessions | All sessions for that user are deleted, so they are signed out everywhere. |
   | Admin accounts | An admin-role user gets a generated key; the bootstrap `admin` key is `ADMIN_KEY` and is not touched by this action. |

6. **Grant or revoke reports access.**

   Use the `View reports` switch in the user's row. Admins always have effective access, so the switch is a courtesy for downgrading later. The same access is available from the `Reports` page in the sidebar and from the CLI.

7. **Delete a user.**

   Use the delete icon in the user's row and confirm in the `Delete user` dialog. Deleting removes the account together with its stored tokens and AI credentials. You cannot delete your own account, and the bootstrap `admin` is not a row you can delete — it exists outside the users table.

8. **Read the Users table.**

   | Column | Meaning |
   | --- | --- |
   | `Username` | Lowercase account name; admins are marked with a shield. |
   | `Status` | `active`, `pending`, or `rejected`. |
   | `Role` | Current role, or a dropdown when you are allowed to change it. |
   | `View reports` | Effective reports access for that user. |
   | `Server providers` | Whether the user may use the server's AI credentials. Pending users cannot be toggled until they are active. |
   | `Created`, `Last Seen` | Registration time and most recent authenticated activity. |

9. **Change a server setting.**

   Open `Settings` in the admin sidebar. Settings are grouped into collapsible categories, and the header reports how many settings and categories exist. Search narrows the list across all categories.

   | Category | Settings it holds |
   | --- | --- |
   | `Jenkins` | `jenkins_url`, `jenkins_user`, `jenkins_password`, `jenkins_ssl_verify`, `jenkins_timeout`, `jenkins_artifacts_max_size_mb`, `get_job_artifacts` |
   | `AI` | `ai_provider`, `ai_model`, `force_server_credentials`, `ai_call_timeout`, `max_concurrent_ai_calls`, `peer_ai_configs`, `peer_analysis_max_rounds` |
   | `Jira` | `jira_url`, `jira_email`, `jira_api_token`, `jira_pat`, `jira_project_key`, `jira_ssl_verify`, `jira_max_results`, `enable_jira`, `enable_jira_issues` |
   | `GitHub` | `github_token`, `tests_repo_url`, `tests_repo_token`, `enable_github_issues` |
   | `Report Portal` | `reportportal_url`, `reportportal_api_token`, `reportportal_project`, `reportportal_verify_ssl`, `enable_reportportal`, `rp_push_classifications`, `rp_push_rootcoz_url`, `rp_push_tracker_links` |
   | `Auth & Security` | `admin_key`, `secure_cookies`, `trust_proxy_headers`, `require_approval`, `admin_wait_approve_msg`, `allowed_users`, `default_user_role` |
   | `Prow` | `prow_url`, `gcs_bucket` |
   | `Server` | `public_base_url`, `additional_repos`, `max_concurrent_repo_clones`, `wait_for_completion`, `poll_interval_minutes`, `max_wait_minutes`, `metadata_rules_file`, `enable_auto_review`, `auto_push_exporters` |
   | `Web Push` | `vapid_public_key`, `vapid_private_key`, `vapid_claim_email` |

   Each row shows the environment variable name, a source badge, and where relevant three other signals:

   | Signal | Meaning |
   | --- | --- |
   | Source `default` | Nothing set this value; the built-in default is in effect. |
   | Source `env` | The environment variable is in effect. |
   | Source `db` | A database override set from this page is in effect. Database values take priority over environment variables. |
   | Warning triangle next to the source | An environment variable is also set. The tooltip shows whether it matches the database override — if it does, the override is redundant. |
   | ⚠️ badge | The setting requires a server restart to take effect. |

10. **Understand masked and restart-required settings.**

   Some settings hold credentials and are never sent to the browser in cleartext. They display as `••••••••` until you use the reveal button, which asks the server for that one value on demand. Change history entries for those settings are masked too, and saving stores them encrypted.

   | Group | Settings |
   | --- | --- |
   | Masked and encrypted (`_SENSITIVE_SETTINGS`) | `jenkins_password`, `jenkins_user`, `jira_api_token`, `jira_pat`, `jira_email`, `github_token`, `tests_repo_token`, `reportportal_api_token`, `admin_key`, `vapid_private_key` |
   | Requires a restart (`_RESTART_REQUIRED_SETTINGS`) | `default_user_role`, `secure_cookies`, `trust_proxy_headers`, `metadata_rules_file`, `vapid_public_key`, `vapid_private_key`, `vapid_claim_email` |

   Everything else, including `enable_auto_review` and `auto_push_exporters`, applies to the running server as soon as you save.

   > **Warning:** Saving an empty value is a reset, not a blank value. The database override is deleted and resolution falls back to the environment variable, then the default.

11. **Review and reset individual settings.**

   Each row offers a history view (who changed it, when, and the previous value) and a reset action that deletes the database override. Resetting a setting that has no override fails rather than silently doing nothing. Because masked values are shown as `••••` in history, capture the real value elsewhere before you reset one.

12. **Watch usage and logs.**

   Two more admin pages round out the picture:

   | Page | Shows |
   | --- | --- |
   | `Tokens` | Token usage broken down by provider, model, job, or day, with calls, input, output, cache read/write, cost, and average duration. Filter by date range and provider. |
   | `Logs` | The live server log tail, filterable by level and text, streamed over SSE. |

## Advanced Usage

- **Reset to environment values.** Database overrides always win over environment variables. Reset the setting in the UI (or delete it with `DELETE /api/admin/settings/{key}`) when you want the deployment's env var back in charge.
- **Audit trail.** Logins, registrations, key rotations, approvals, role changes, report grants, and deletions are all written to the audit log with the acting admin's username, and both actions and log lines are visible on the `Logs` page.
- **Automate with the CLI.** User creation, role changes, `can_view_reports`, and pending approvals are all available from the CLI (`admin users ...`). See [CLI Command Reference](cli-reference.html) for details.
- **Give analysis more context.** The `AI`, `GitHub`, and `Server` categories hold most of the knobs that change how much context an analysis gets. See [Configuring Analysis Context](configure-analysis-context.html) for details.
- **Keep the bootstrap key reachable.** `ADMIN_KEY` authenticates a superuser that lives outside the database. If you rotate it while signed in as a database admin, keep a copy of the new value or you can lock yourself out.
- **Encryption key rotation.** `ROOTCOZ_ENCRYPTION_KEY` is not a server setting in the UI. Changing it re-derives both the Fernet key used for at-rest secrets and the HMAC secret used to hash API keys. Existing API key hashes stop matching, so every user must be given a new key; stored user tokens, AI credentials, and encrypted settings become undecryptable and must be re-entered. Sessions survive, because a session row stores only the username, admin flag, role, and expiry.

## Troubleshooting

- `Users` or `Settings` is missing from the sidebar.  
  You are not an admin. Ask an admin to change your role; only an admin can change your own role via another admin.

- `Admin access required` (403).  
  The signed-in account is not an admin, or the session predates a role change. Sign out and back in.

- A new registration does not appear as pending.  
  `REQUIRE_APPROVAL` is off, so registrations are created `active` immediately. Turn it on in `Auth & Security` to require approval.

- A registered username was rejected.  
  Usernames starting with `rootcoz` are reserved. Pick a different username.

- `DEFAULT_USER_ROLE` refuses to save `admin`.  
  It only accepts `viewer`, `reviewer`, or `operator`. Grant `admin` to a specific user from the Users page instead.

- Reports still do not appear for a user you just granted.  
  The grant is read per request, so reload the page. If the user has not signed in again and still sees nothing, confirm they are not a pending account.

- A masked setting looks empty after you reveal it.  
  There is no database override, so there is nothing stored to reveal. Reveal uses the environment value when one is set.

- A setting did not take effect after saving.  
  Check for the ⚠️ restart badge. `default_user_role`, `secure_cookies`, `trust_proxy_headers`, `metadata_rules_file`, and the three `Web Push` settings only apply after the server restarts.

- Everyone's logins and tokens broke after an infrastructure change.  
  `ROOTCOZ_ENCRYPTION_KEY` changed. Sessions survive, but stored API key hashes and encrypted tokens do not — rotate keys and re-enter tokens.

- Bootstrap `admin` login fails after you edited `admin_key`.  
  The login path compares the username `admin` against the current value in constant time. Confirm the value you saved, and remember that setting an empty value resets the override instead of storing a blank.

## Related Pages

- [Managing Your Account and Notifications](manage-account-and-notifications.html)
- [Configuration Reference](configuration-reference.html)
- [Deploying RootCoz](deploy-rootcoz.html)
- [Configuring Analysis Context](configure-analysis-context.html)
- [Exploring History and Reports](explore-history-and-reports.html)
- [CLI Command Reference](cli-reference.html)