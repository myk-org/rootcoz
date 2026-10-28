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
   | Rotate own API key, manage own tokens | yes | yes | yes | yes |
   | Submit a new analysis (`POST /analyze`, ingest-only `POST /submit`) | no | no | yes | yes |
   | Re-analyze any job | no | no | yes | yes |
   | Delete own jobs | no | no | yes | yes |
   | Delete any job | no | no | no | yes |
   | Access `Reports` | only with the reports grant | only with the reports grant | only with the reports grant | always |
   | Create and delete users, change roles, grant reports access | no | no | no | yes |
   | Rotate another user's API key | no | no | no | yes |
   | Access `/api/admin/*` (Users, Tokens, Logs, Settings, admin Chat, DB tools) | no | no | no | yes |

   > **Note:** This table describes what the server currently enforces, which is not always what `AGENTS.md` describes. Rotating your own API key works for a `viewer` today because `POST /api/auth/rotate-key` checks no role, so this row reads `yes`; `AGENTS.md` reserves it for `reviewer`. Two further routes used to have the same gap — `PUT /results/{job_id}/reviewed` (which feeds auto-review and the exporter push) and `PUT /results/{job_id}/tags` — and now require `reviewer`. `POST /api/auth/rotate-key` remains tracked in [#289](https://github.com/myk-org/rootcoz/issues/289); treat the current behaviour as a bug rather than a supported capability. `GET /api/admin/token-usage/stream` is correctly admin-only: the authentication middleware rejects every non-admin request under `/api/admin/` before the handler runs, so it does not belong in this list.

   Two flags sit outside the role ladder:

   | Flag | Default | Meaning |
   | --- | --- | --- |
   | `can_view_reports` | off | Grants access to `/api/reports/*` and the `Reports` page to a non-admin. It is orthogonal to role: a `viewer` with the grant can read reports, and an `operator` without it cannot. For non-admins the flag is re-read from the users table on every request, so a grant takes effect without invalidating sessions. Admins always have effective access regardless of the stored value. |
   | `can_use_server_providers` | off | Grants the `Use server credentials` toggle on `New Analysis`, which lets the user run analyses on the server's AI keys instead of their own. Admins always have it. |
   | `ENABLE_FREE_MODELS` (server setting) | off | When on, every user may run analyses and chat on the server's keys for the admin-declared **free provider+model pairs**. Admins and users with the server-providers grant already cover it. Change it under Server Settings → AI, where the *Free AI models* card below the AI settings manages the pair list itself. |

   > **Note:** `DEFAULT_USER_ROLE` decides the role new registrations get, and it accepts only `viewer`, `reviewer`, or `operator` — it cannot grant `admin`. Setting it before startup does **not** by itself let self-registered users submit analyses: `REQUIRE_APPROVAL` defaults to `true`, and registration then creates the account with `pending` status, which cannot reach protected endpoints until an admin approves it. Set `REQUIRE_APPROVAL=false` alongside the role for that outcome.

2. **Create a user.**

   On `Users`, click `Add User`. The dialog takes a username, a role, and two switches: `Allow viewing reports` and `Allow server providers`. Click `Create`.

   | Detail | Behaviour |
   | --- | --- |
   | Username rules | Lowercased, must be non-empty, and must not start with `rootcoz` (that prefix is reserved, including the AI identity `rootcoz-ai`). |
   | Status | The account is created as `active` — admin-created users never go through approval. |
   | API key | Returned once in the response and shown once in the dialog. Copy it before closing. |
   | Reports flag | Stored exactly as submitted, even for an `admin`. Note that changing a user's role does not clear it: a user created through the API with the flag set keeps the grant after being demoted, so clear it explicitly if that is not what you want. The Settings UI does not offer the flag on admin creation, which is why the UI avoids this case. |
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
   | `Jira` | `jira_email`, `jira_api_token`, `jira_cloud_url`, `jira_cloud_ssl_verify`, `jira_pat`, `jira_server_url`, `jira_server_ssl_verify`, `jira_oauth_client_secret`, `atlassian_oauth_cloud_id`, `atlassian_oauth_client_id`, `jira_atst_token`, `jira_oauth_ssl_verify`, `jira_site_url`, `jira_project_key`, `jira_max_results`, `enable_jira`, `enable_jira_issues` |
   | `GitHub` | `github_token`, `tests_repo_url`, `tests_repo_token`, `enable_github_issues` |
   | `Report Portal` | `reportportal_url`, `reportportal_api_token`, `reportportal_project`, `reportportal_verify_ssl`, `enable_reportportal`, `rp_push_classifications`, `rp_push_rootcoz_url`, `rp_push_tracker_links` |
   | `Datadog` | `datadog_api_key`, `datadog_app_key`, `datadog_site`, `datadog_service`, `datadog_env`, `enable_datadog`, `dd_push_classifications`, `dd_push_rootcoz_url`, `dd_push_tracker_links`, `dd_allow_unreviewed_push` |
   | `Auth & Security` | `admin_key` (displayed masked, read-only), `secure_cookies`, `trust_proxy_headers`, `require_approval`, `admin_wait_approve_msg`, `allowed_users`, `default_user_role` |
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
   | Masked and encrypted (`_SENSITIVE_SETTINGS`) | `jenkins_password`, `jenkins_user`, `jira_api_token`, `jira_pat`, `jira_email`, `jira_oauth_client_secret`, `jira_atst_token`, `github_token`, `tests_repo_token`, `reportportal_api_token`, `admin_key`, `vapid_private_key` |
   | Requires a restart (`_RESTART_REQUIRED_SETTINGS`) | `default_user_role`, `secure_cookies`, `trust_proxy_headers`, `metadata_rules_file`, `vapid_public_key`, `vapid_private_key`, `vapid_claim_email` |

   Masked settings are stored encrypted when you save them from this page. The one exception is `admin_key`: it displays masked but is read-only — it is a server-only bootstrap secret, and every write path rejects it.

   Everything else applies to the running server as soon as you save. The exceptions are the server-only settings, which this page cannot save at all: `PUT /api/admin/settings` rejects every one of them with `400 Server-only settings (env var only)`, so they must be set in the deployment environment instead. They are `admin_key` (bootstrap secret for the `admin` superuser), `db_path`, and `debug`. A setting described elsewhere on this page as editable, or as needing only a restart, is still rejected here if it appears in that list — the restart-required distinction only applies to settings that can be saved at all.

   > **Note:** Saving an empty value is an explicit blank override, not a reset. The empty value is stored in the database and beats the environment variable — this is how you clear a setting the deployment environment still sets (e.g. `CLI_AGENTS`). To fall back to the environment variable or default instead, use the row's reset action (DELETE), which removes the override. Non-string settings (booleans, integers) reject empty saves with a 400 error.

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
- **Export and import server settings.** The Server Settings page has `Export settings` / `Import settings` buttons in the header. Export downloads a JSON document of the **database overrides only** — environment-only values are deployment config, not server settings, and stay out. Sensitive settings (API keys, tokens, passwords) are excluded unless you tick `Include secrets` and confirm the warning; the file then holds them as plaintext (re-encrypted values would only be usable with the same `ROOTCOZ_ENCRYPTION_KEY`), so store and transfer it securely and delete it once imported. Without the secrets opt-in the export is also scrubbed of embedded credentials: `@token` suffixes in `additional_repos` entries and username/password userinfo in URLs are stripped, so no credential leaves without the opt-in. Import validates every key and value with the same rules as saving a single setting, applies the whole document **atomically** — one invalid value rejects the entire import with `422` and nothing is applied, and the write itself is a single database transaction — and reports a per-setting diff plus which changed settings need a restart. Sensitive values are masked (`***`) in the diff, and credential substrings (URL userinfo, repo tokens) in non-sensitive values are masked too. A scrubbed export also never strips credentials from the target on import: a setting whose imported value matches the scrubbed form of its current value is skipped and reported as unchanged, so the target's credential-bearing value (for example a repo `@token`) wins. If validation fails, the confirmation dialog stays open with the error — but retrying resends the same parsed document, so fixing the file on disk changes nothing until you select the file again. The CLI mirrors both: `rootcoz admin settings export [--include-sensitive] [--output FILE]` and `rootcoz admin settings import --input FILE [--dry-run]`, where `--dry-run` validates and shows the diff without applying anything. An `--output` file is written with owner-only `0600` permissions because it can hold decrypted secrets; a missing directory or unwritable destination is a clean CLI error, not a traceback.
- **Audit trail.** Logins, registrations, key rotations, approvals, role changes, report grants, and deletions are all written to the audit log with the acting admin's username, and both actions and log lines are visible on the `Logs` page.
- **Automate with the CLI.** User creation, role changes, `can_view_reports`, and pending approvals are all available from the CLI (`admin users ...`). See [CLI Command Reference](cli-reference.html) for details.
- **Give analysis more context.** The `AI`, `GitHub`, and `Server` categories hold most of the knobs that change how much context an analysis gets. See [Configuring Analysis Context](configure-analysis-context.html) for details.
- **Declare free models.** In Server Settings → AI, the *Free AI models* card below the settings rows manages the free provider+model pair list. Pick a provider and model from the server catalog and click `Add`; remove pairs with the per-row `Remove` button. "Free" is this list — never the catalog's cost fields — and every pair is validated against the server catalog at save time. The `ENABLE_FREE_MODELS` toggle above it decides whether the list is live. The CLI mirrors it: `admin models free-list` / `free-add` / `free-remove`.
- **Scope a server provider's models to its key.** When a stored server provider key supports model listing, adding or rotating it captures the models that key can actually access — the provider's model selection (and every user's model picker on server credentials) then offers exactly that set, marked with a `Key-scoped` badge. A keyed provider without a listing endpoint shows a *static catalog* hint and keeps the catalog-based behavior. `Refresh models` on a provider row re-runs the listing with the stored key to pick up permission drift without re-entering it; the CLI mirrors it: `admin server-providers refresh-models --provider PROVIDER`.
- **Keep the bootstrap key reachable.** `ADMIN_KEY` authenticates a superuser that lives outside the database. If you rotate it while signed in as a database admin, keep a copy of the new value or you can lock yourself out.
- **Encryption key rotation.** `ROOTCOZ_ENCRYPTION_KEY` is not a server setting in the UI. Changing it re-derives both the Fernet key used for at-rest secrets and the HMAC secret used to hash API keys. Existing API key hashes stop matching, so every user must be given a new key; stored user tokens, AI credentials, and encrypted settings become undecryptable and must be re-entered. Sessions survive, because a session row stores only the username, admin flag, role, and expiry.

## Troubleshooting

- `Users` or `Settings` is missing from the sidebar.  
  You are not an admin. Ask an admin to change your role; only an admin can change your own role via another admin.

- `Admin access required` (403).  
  The signed-in account is not an admin, or the session predates a role change. Sign out and back in.

- A new registration does not appear as pending.  
  `REQUIRE_APPROVAL` is off, so registrations are created `active` immediately. To require approval, set it in the deployment environment — it is a server-only setting, so saving it through Server Settings is rejected with `400 Server-only settings (env var only)` and restart the server afterwards.

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

- A settings import was rejected with `Invalid settings: ...`.
  Every key and value is checked before anything is applied, and one invalid entry rejects the whole import. The error lists the offending entries — fix them in the export file (or on the source server) and import again. Nothing was partially applied.

- Everyone's logins and tokens broke after an infrastructure change.  
  `ROOTCOZ_ENCRYPTION_KEY` changed. Sessions survive, but stored API key hashes and encrypted tokens do not — rotate keys and re-enter tokens.

- Bootstrap `admin` login fails after you edited `admin_key`.  
  The login path compares the username `admin` against the current value in constant time. Confirm the value you saved, and remember that `admin_key` is server-only — it cannot be saved or reset from this page at all.

## Related Pages

- [Managing Your Account and Notifications](manage-account-and-notifications.html)
- [Configuration Reference](configuration-reference.html)
- [Deploying RootCoz](deploy-rootcoz.html)
- [Configuring Analysis Context](configure-analysis-context.html)
- [Exploring History and Reports](explore-history-and-reports.html)
- [CLI Command Reference](cli-reference.html)