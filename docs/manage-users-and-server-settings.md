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
   | `AI` | `ai_provider`, `ai_model`, `ai_call_timeout`, `max_concurrent_ai_calls`, `peer_ai_configs`, `peer_analysis_max_rounds` |
   | `Jira` | `jira_email`, `jira_api_token`, `jira_cloud_url`, `jira_cloud_ssl_verify`, `jira_pat`, `jira_server_url`, `jira_server_ssl_verify`, `jira_oauth_client_secret`, `atlassian_oauth_cloud_id`, `atlassian_oauth_client_id`, `jira_atst_token`, `jira_oauth_ssl_verify`, `jira_site_url`, `jira_project_key`, `jira_max_results`, `enable_jira`, `enable_jira_issues` |
   | `GitHub` | `github_token`, `tests_repo_url`, `tests_repo_token`, `enable_github_issues` |
   | `Report Portal` | `reportportal_url`, `reportportal_api_token`, `reportportal_project`, `reportportal_verify_ssl`, `enable_reportportal`, `rp_push_classifications`, `rp_push_rootcoz_url`, `rp_push_tracker_links` |
   | `Auth & Security` | `admin_key` (displayed masked, read-only), `secure_cookies`, `trust_proxy_headers`, `require_approval`, `admin_wait_approve_msg`, `allowed_users`, `default_user_role`, `audit_retention_days`, `audit_max_rows`, `audit_auth_failure_max_per_minute` |
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

13. **Review the audit log.**

   Every audited action is written as one row in the audit trail, newest first. Each event records the actor, the actor's role and admin flag, the action id, an outcome (`success`, `failure`, or `denied`), an optional target (`user`, `job`, `setting`, `provider`), the client IP when `trust_proxy_headers` is on, and a `details` object with the non-secret specifics.

   | What is recorded | Action ids |
   | --- | --- |
   | Sign-in, registration, and own keys | `auth.login`, `auth.login_failed`, `auth.login_blocked`, `auth.logout`, `auth.register`, `user.self_key_rotate`, `user.ai_credential_set`, `user.ai_credential_delete` |
   | User and grant administration | `admin.user_create`, `admin.user_delete`, `admin.user_role_change`, `admin.user_key_rotate`, `admin.user_approve`, `admin.user_reject`, `admin.user_can_view_reports`, `admin.user_can_use_server_providers`, `user.tracker_token_set`, `user.tracker_token_delete` |
   | Server settings and AI providers | `settings.update`, `settings.reset`, `settings.import`, `admin.setting_persist`, `provider.key.store`, `provider.key.promote`, `provider.key.rotate`, `provider.remove`, `provider.models_set`, `provider.models_refresh`, `provider.deployment_refresh`, `provider.selection_seed`, `provider.free_selection_auto_include`, `free_models.set` |
   | Jobs, results, and trackers | `admin.job_delete`, `user.job_delete`, `user.abort_job`, `analysis.submit`, `analysis.rerun`, `review.delete`, `classification.override`, `pattern.override`, `comment.add`, `comment.enrich`, `comment.delete`, `issue.create`, `export.push`, `job.tags_set`, `job.metadata_set`, `job.metadata_delete`, `tracked_in.set`, `tracked_in.delete` |
   | Admin tooling and exports | `transcript.list`, `transcript.download`, `admin.db_schema`, `admin.db_query` (results are masked, and the row records the count and a query hash, never the rows), `admin.settings_export`, `audit.export`, `feedback.create` |

   **What is audited, and what is not.** An event is recorded when it changed stored state on the server or is security-relevant. Transient user activity is deliberately **not** recorded: chat sessions and messages, mention read-state, notification toggles, saved filter presets, connection tests and read-only previews. The rule is the question an administrator actually asks — "who did that / what changed / who read that" — so a settings export that can carry plaintext secrets is recorded (`admin.settings_export`, with `include_sensitive`), and so is exporting the trail itself (`audit.export`, with the format and row count), while opening a chat is not. `tests/test_audit_coverage.py` enforces this: a new mutating endpoint fails the build unless it writes a row or is exempt with a written reason.

   Some of those carry the specifics that explain the event later. `analysis.rerun` has two shapes. A whole-job re-run targets the job and records `provider`, `model`, `trigger: "manual"`; a single-failure re-run uses the same action with `trigger: "failure"` and records `failure_uuid` and `test_name` instead, while still targeting the parent job so a job filter returns both. The whole-job shape adds `previous_classification` — the most common classification among the job's failures before the re-run — when the job had one; the re-runs queued by `POST /results/{job_id}/analyze` and `POST /re-analyze/{job_id}` both write it, with outcome `success` when the run was queued and `failure` when queueing raised. `provider.key.promote` is written by `Store API key` on a deployment-credential row, so a credential promoted from the environment is never mistaken for a key an admin typed (`provider.key.store`) or replaced (`provider.key.rotate`). `user.tracker_token_set` / `user.tracker_token_delete` record **which field** changed — `{"field": "jira_email"}`, `jira_token`, `jira_sa_token` or `github_token` — one row per field that actually changed, so the answer is "user X changed the Jira token" and never what it was changed to. `user.ai_credential_set` records the provider, the verification `model`, and `replaced` (true when a credential for that provider was already stored), so adding a key is distinguishable from rotating one afterwards. `tracked_in.set` records the link type and id but not the URL, because a URL can carry credentials in its userinfo. `comment.add` records the job and test name, never the comment text. `comment.delete` targets the comment id and records only `job_id`. Two events from that list have no endpoint to record: comment editing (there is no edit endpoint) and session revocation (there is none — sessions end by logging out, rotating a key, or deleting the account, and each of those is recorded).

   **Sensitive values are never stored.** A settings event names the setting and records that it changed — `{"key": "jenkins_password", "changed": true, "redacted": true}` — and never its value (`redacted` appears only for a sensitive key, so a reader can tell a value was written without the trail naming or carrying it; a non-sensitive key is just `{"key": "log_level", "changed": true}`). Provider events name the provider only. A stored key, token, or password is dropped before the row is written, by the same sensitive-name check the settings page uses, so a newly added credential field needs no second list. Text values are also scanned for credential-shaped content — a PEM private key block, a known credential prefix (`sk-`, `ddsat_`, `ddapp_`, `ghp_`, `github_pat_`, `xox[baprs]-`, `AKIA`), or a long unbroken token — and only the matched substring becomes `[redacted]`, so a key pasted into a free-text reason never lands in the table. A run of 24 or more unbroken characters counts as credential-shaped when its case changes on at least half of its letter-to-letter transitions — a random token alternates case on nearly every character, while camelCase identifiers, PascalCase class names, all-lowercase prose and dot-separated test names do not, so those survive. A digit alone is not enough: it only makes a run secret-shaped when the run is also mixed case, or a bare lowercase hex/blob of 32+ characters. That keeps the evidence worth keeping — `uuid4` job ids, dated model ids like `claude-sonnet-4-5-20250929`, and test names with build numbers — while still catching real tokens. **Identity is never redacted.** The actor, the target and the message are stored exactly as they happened, because the trail's job is to answer "what did user X do" and "which username is being attacked". A failed login for `svc-ci-pipeline-prod2024-runner` and one for a pasted token both appear verbatim, and the actor filter finds them. A username longer than 256 characters is bounded to its first 256 characters plus `…` so a single row cannot be inflated, and the attempt still answers with the same `401` as any wrong key. The `[AUDIT]` line in the server log carries the same verbatim identity, and there is no per-request lookup behind it: identity is recorded, not resolved.

   **Tamper evidence.** Every row carries `prev_hash` and `entry_hash`: the hash covers the row's fields plus the previous row's hash, so editing or deleting one row invalidates every row after it. `rootcoz admin audit verify` (or `GET /api/admin/audit/verify`) recomputes the chain and names the first broken event; rows written before the chain existed are reported as `legacy` and skipped rather than counted as broken — a row is only legacy if it is older than the first chained row, so clearing a hash to hide an edit is reported as a break. Retention pruning removes the oldest rows, which takes the head of the chain with it, so the surviving prefix is reported as `truncated` (with the id it was truncated before) instead of being called broken — but only when the boundary matches the stored prune checkpoint (the id and hash of the last row retention deleted). Deleting old rows any other way does not match it and is reported as a break, so a hand-pruned trail is still caught; a genuine edit inside the surviving window always fails. `created_at` is stored as UTC ISO-8601 with an explicit `Z`, so `--since`/`--until` filters mean the same instant in any timezone: a date-only value is a UTC day, an explicit offset is honoured, and a timestamp without an offset is read as UTC.

   **Retention and the size ceiling.** `audit_retention_days` (Server Settings → Auth & Security, default `90`) decides how long rows are kept. `0` keeps them forever. `audit_max_rows` (default `0`, no cap) keeps at most that many rows, deleting the oldest first once the trail grows past it. Both limits are applied together in the background at most once a day, at startup and then hourly if a day has passed, and each deletion is logged with its count.

   **Failed logins are throttled, not silently lost.** A brute-force run must not be able to fill the trail, so `audit_auth_failure_max_per_minute` (default `20`, `0` = unlimited) caps how many `auth.login_failed` and `auth.login_blocked` rows can be added per minute — this many per username and ten times this many server-wide, and the check shares a transaction with the insert so parallel attempts cannot slip past it. The two actions share the budget, and cycling usernames does not reset it. The server-wide figure is larger on purpose: with equal budgets a junk-username flood would exhaust the shared allowance and the trail would then drop the rows for a targeted account, exactly the evidence an investigation needs. On top of that, an attempt against an existing account always produces at least one row per minute even when both budgets are spent — so a flood of invented usernames cannot erase the first sign of an attack on a real one — and the rescued rows are capped at this same limit per window server-wide. The worst case is therefore 11 × the limit failed-login rows per minute: an attacker cannot farm extra rows by registering accounts, and the rescue cannot become a flood itself. Past the limit the row is skipped — but the attempt still produces its normal `[AUDIT]` line, so the log stream shows every attempt, one summary line per minute states how many rows were dropped, and the running total appears as `suppressed_auth_events` in `checks.audit` on `GET /api/health`. No other action is ever throttled: a successful login, a settings change or a job deletion is always stored.

   **Retain evidence before it is deleted.** Nothing deleted can be verified afterwards, so export first: `rootcoz admin audit export --format json --output audit.json` (or `--format csv`, plus the same `--actor`/`--action`/`--since`/`--until` filters as `audit list`) streams the complete matching set — uncapped by the list page size and without loading it all into memory. Both export formats carry `prev_hash`, `entry_hash` and `hash_payload` — the exact bytes that were hashed — so a retained export verifies itself offline with `sha256(hash_payload + prev_hash) == entry_hash` even after the live rows are pruned; `hash_version` names the field list the payload covers (1 = the original set, 2 = plus the rescue counter, so even that budget is tamper-evident), and a mixed trail verifies row by row because no stored hash is ever rewritten. A row without `hash_payload` (written before the chain, or withheld because its stored bytes would expose a credential) is "not verifiable", not verified. The API equivalent is `GET /api/admin/audit/export?format=json|csv`.

   **Editors are visible, and buttons mean something.** The repositories and peer-analysis editors are always on the page — no "Not configured · Configure …" link that hides them. While nothing has changed there is nothing to save, so neither `Save` nor `Cancel` is shown; they appear with the first add or edit, `Cancel` restores exactly what was stored (and removes the buttons again when there was nothing before), and a successful save returns the row to its clean state. `Configure provider` / `Configure model` are different: they open a credential dialog, not a hidden form.

**Empty is shown as empty.** A setting with no value renders `(empty)`. A sensitive value that is *not* set shows the same `(empty)` — never masked dots and never a reveal control, because those imply a stored secret that does not exist; once a value is saved the row shows the mask and the reveal eye. The API reports `is_set` for every setting so the page never has to guess.

**An "ambient" provider must really be ambient.** `AMBIENT_AUTH_PROVIDERS` lists providers the *deployment* authenticates itself — an env key (`<PROVIDER>_API_KEY`), a manifest `"$VAR"` credential, or a provider the sidecar reports as already authenticated (ADC/OAuth/CLI login). Saving an id with no deployment credential is rejected with `400` naming it, because declaring it ambient would make provider resolution fail later instead of here. When the deployment cannot be inspected (cold sidecar, empty catalog) the save is accepted with a warning rather than blocking the admin.

**Settings are named for humans.** Every row is titled with a readable name — "Wait for completion", "Audit retention (days)", "Jira authentication mode" — never the machine key. The environment variable (`WAIT_FOR_COMPLETION`) is shown beneath the title as a small, copyable line, so an operator can still map the row to the deployment config and to this reference. Enumerated settings present their choices instead of a text box: a select for roles, log level and Jira auth mode; a picker of real exporters for `auto_push_exporters` (one that is not configured is visible but not selectable); the provider catalog for `ambient_auth_providers`; and, for `allowed_users`, suggestions from existing accounts **plus** free entry, because SSO users are provisioned later. The API rejects a value outside an enumerated set rather than storing it, and `allowed_users` is the one place an unknown name is legitimate.

**Rows are readable and clickable.** A target is shown by its human name — the job's display name, `comment on <job>` for a comment, the username, the setting key — and links to the place it refers to: a job target opens its report (including when the row names the job rather than its id), a comment opens the report at that comment, a user opens Users, a setting or provider opens Server Settings. The raw `type:id` stays in a tooltip and in the row, so it is still copyable for the CLI or a query. Clicking an actor applies the actor filter, which answers "what did this user do" in one click. Nothing is stored as a URL: links are derived from the ids at read time.

**Every row says what happened, not just who did it.** Rows describe the substance: a metadata delete lists the values it cleared (`{"cleared": {"team": "mtv"}}`), a tag update lists what was added and removed, a pattern override records `from` → `to` like a classification override, a failed login carries its reason (`unknown_user`, `bad_key`, `blocked`, `overlong`), a login records the method it used, a user deletion records the role and grants the account had, a key rotation says sessions were invalidated, an analysis submission carries the job name and the provider/model actually used, and a transcript download names the session. Credential values are never recorded, and a row is not written with empty details — a test enforces that every audited action carries at least one meaningful field.

**Audit page filters live in the URL.** Actor, action, outcome, free-text search and the date bounds are part of the query string (alongside `limit` and `live`), so a link you copy shows the same rows for whoever opens it — including when you navigate Back or Forward, which re-reads the query instead of keeping the controls on the old filters. Changing a filter rewrites the URL in place rather than adding a history entry, and an empty filter is removed instead of being kept as `actor=`.

   **The page is honest about time and about gaps.** Stored timestamps and the date filters are UTC and the column header says so, while the rows themselves show your local time. If the live stream cannot keep up, the page says how many events were missed and offers a refresh instead of presenting an incomplete view as live; and if your session expires, the error offers a sign-in action rather than a bare message.

   Read the trail with `rootcoz admin audit list` / `rootcoz admin audit actions` / `rootcoz admin audit verify` / `rootcoz admin audit export`, or over the API at `GET /api/admin/audit`, `GET /api/admin/audit/actions`, `GET /api/admin/audit/verify`, `GET /api/admin/audit/export`, and the `GET /api/admin/audit/stream` SSE feed. All are admin-only; see [API Reference](api-reference.html#audit-events).

   **Watch the write path.** An audit row that fails to persist is a compliance hole, so the failure is logged at `ERROR`, counted for the life of the process, and surfaced as `checks.audit` in `GET /api/health` (`{"status": "degraded", "write_failures": N, "suppressed_auth_events": M}`) and as the `rootcoz_audit_write_failures` gauge in `GET /metrics`.

## Advanced Usage

- **Reset to environment values.** Database overrides always win over environment variables. Reset the setting in the UI (or delete it with `DELETE /api/admin/settings/{key}`) when you want the deployment's env var back in charge.
- **Export and import server settings.** The Server Settings page has `Export settings` / `Import settings` buttons in the header. Export downloads a JSON document of the **database overrides only** — environment-only values are deployment config, not server settings, and stay out. Sensitive settings (API keys, tokens, passwords) are excluded unless you tick `Include secrets` and confirm the warning; the file then holds them as plaintext (re-encrypted values would only be usable with the same `ROOTCOZ_ENCRYPTION_KEY`), so store and transfer it securely and delete it once imported. Without the secrets opt-in the export is also scrubbed of embedded credentials: `@token` suffixes in `additional_repos` entries and username/password userinfo in URLs are stripped, so no credential leaves without the opt-in. Import validates every key and value with the same rules as saving a single setting, applies the whole document **atomically** — one invalid value rejects the entire import with `422` and nothing is applied, and the write itself is a single database transaction — and reports a per-setting diff plus which changed settings need a restart. Sensitive values are masked (`***`) in the diff, and credential substrings (URL userinfo, repo tokens) in non-sensitive values are masked too. A scrubbed export also never strips credentials from the target on import: a setting whose imported value matches the scrubbed form of its current value is skipped and reported as unchanged, so the target's credential-bearing value (for example a repo `@token`) wins. If validation fails, the confirmation dialog stays open with the error — but retrying resends the same parsed document, so fixing the file on disk changes nothing until you select the file again. The CLI mirrors both: `rootcoz admin settings export [--include-sensitive] [--output FILE]` and `rootcoz admin settings import --input FILE [--dry-run]`, where `--dry-run` validates and shows the diff without applying anything. An `--output` file is written with owner-only `0600` permissions because it can hold decrypted secrets; a missing directory or unwritable destination is a clean CLI error, not a traceback.
- **Lockout, disable and unlock.** A lock is a **login guard, not a revocation**: it refuses new sign-ins while the account's existing sessions and API keys keep working until they expire or are rotated, so five wrong keys typed by someone who knows a username cannot become a remote kill-switch for that account's CI access. `Disable` is the deliberate version — it refuses every path, because taking an account out of service is a decision, not a side effect. Repeated failed logins against one account lock it once `LOGIN_LOCKOUT_THRESHOLD` (default `5`, `0` disables) consecutive attempts fail — the response stays the same generic `401` as a wrong key, so a locked account is indistinguishable from a mistyped one. A lock persists until an administrator presses `Unlock` on the Users page; it does not expire on its own, because a timer is exactly what an attacker waits for. `Disable` takes an account out of service without deleting it: it cannot sign in, while its jobs, comments and audit history stay intact, and `Enable` restores it. The Users page shows the state per row (`active`, `disabled`, or `locked (3/5)`), and every one of these actions is audited (`auth.account_locked`, `auth.login_blocked` with `reason: locked`/`disabled`, `admin.user_unlock`, `admin.user_disabled`, `admin.user_enabled`). This is separate from `AUDIT_AUTH_FAILURE_MAX_PER_MINUTE`, which only bounds how many failure **rows** are stored — `0` there means "log everything", `0` here means "never lock", and neither substitutes for the other.
- **Audit trail.** Logins, registrations, key rotations, approvals, role changes, report grants, settings changes, and deletions are all written to the audit trail with the acting account's username, readable through `rootcoz admin audit` and `GET /api/admin/audit`. The matching `[AUDIT]` lines also show up on the `Logs` page.
- **Automate with the CLI.** User creation, role changes, `can_view_reports`, and pending approvals are all available from the CLI (`admin users ...`). See [CLI Command Reference](cli-reference.html) for details.
- **Give analysis more context.** The `AI`, `GitHub`, and `Server` categories hold most of the knobs that change how much context an analysis gets. See [Configuring Analysis Context](configure-analysis-context.html) for details.
- **Declare free models.** In Server Settings → AI, the *Free AI models* card below the settings rows manages the free provider+model pair list. Pick a provider and model from the server catalog and click `Add`; remove pairs with the per-row `Remove` button. "Free" is this list — never the catalog's cost fields — and every pair is validated against the server catalog at save time. The `ENABLE_FREE_MODELS` toggle above it decides whether the list is live. The CLI mirrors it: `admin models free-list` / `free-add` / `free-remove`.
- **Scope a server provider's models to its key.** When a stored server provider key supports model listing, adding or rotating it captures the models that key can actually access — the provider's model selection (and every user's model picker on server credentials) then offers exactly that set, marked with a `Key-scoped` badge. A keyed provider without a listing endpoint shows a *static catalog* hint and keeps the catalog-based behavior. `Refresh models` on a provider row re-runs the listing with the stored key to pick up permission drift without re-entering it; the CLI mirrors it: `admin server-providers refresh-models --provider PROVIDER`.
- **Providers keyed from the deployment env.** A custom provider whose `models.json` key is an environment reference (`"$VAR"`) and that is flagged `trustKeyScopedListing` is listed live too — RootCoz captures that listing with the env-resolved key, so the row shows the gateway's real models under a `Listing from deployment credential` badge while `has_key` stays `false` and nothing is stored. The API distinguishes the cases with `key_scoped_source`: `deployment` for a listing captured with the deployment credential, `stored` for one captured with a key you added here, `null` when no listing was captured. The capture is refreshed at startup, on page load (at most once every five minutes), by `Refresh models`, and by a global model refresh; a failure keeps the last captured listing rather than falling back to the static manifest. Such a row offers both `Refresh models` — re-running the listing with the deployment key — and `Store API key`, which promotes that deployment credential into the encrypted store, so nothing is pasted: you pick a verification model, RootCoz verifies the credential it already resolves with a real sidecar turn, and stores it (CLI: `admin server-providers store-deployment-key --provider PROVIDER --model MODEL`). Afterwards the row is a normal stored-key provider — `has_key` and `verified` become `true`, the listing source becomes `stored`, and the `Rotate` / `Remove` actions replace `Store API key`. `can_refresh_models` is `true` for these rows as well as for stored-key rows. The whole capture is gated on the sidecar's own capability answer: a provider it does not report as supporting session API keys is never asked for a key-scoped listing — `trustKeyScopedListing` does not override that — so the row keeps its static catalog, `can_refresh_models` stays `false`, and `Refresh models` answers with a `400` instead of calling the sidecar. See [Configuration Reference](configuration-reference.html#key-scoped-model-listing-trustkeyscopedlisting).
  The same applies with no manifest at all: a provider authenticated from the environment — `OPENAI_API_KEY` for `openai`, `GEMINI_API_KEY` (then `GOOGLE_API_KEY`) for `google`, otherwise `<PROVIDER_ID>_API_KEY` with every non-alphanumeric character folded to `_` (`my-gateway` → `MY_GATEWAY_API_KEY`) — is listed live too, captured and refreshed the same way with `has_key` still `false` and `key_scoped_source: "deployment"`. Nothing is stored and the value never appears in a log line or API response. Unset the variable and the capture, the live listing, and `Refresh models` all go with it, so the row falls back to its static catalog.
- **Providers with no catalog model.** The Server providers list carries every provider the sidecar registers as accepting a session API key, not only the ones with a model in the server catalog. A registered provider with no catalog model appears with an empty model list and its `Add key` action, so you can store a key for it; once the key is verified its own key-scoped listing supplies the models. A provider the sidecar reports as not accepting session API keys (e.g. `github-copilot`) is never listed.
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