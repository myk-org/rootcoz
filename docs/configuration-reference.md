# Configuration Reference

RootCoz reads its configuration from environment variables at startup and lets admins override most of it later from the Server Settings page. This page lists every variable, its default, whether it is a secret, and whether a restart is needed — organised into the same categories the Server Settings UI uses.

## Prerequisites
- A running or planned RootCoz deployment. See [Deploying RootCoz](deploy-rootcoz.html) for the Kubernetes and OpenShift paths.
- Admin access if you plan to change settings after login. See [Managing Users and Server Settings](manage-users-and-server-settings.html).
- One AI credential for `claude`, `gemini`, or `cursor` if you want analysis to run at all.

## Quick Example

```dotenv
AI_PROVIDER=claude
AI_MODEL=<a model id from GET /api/ai-models>
ANTHROPIC_API_KEY=your-anthropic-api-key
DEFAULT_USER_ROLE=operator
REQUIRE_APPROVAL=false
SECURE_COOKIES=false
```

Those six lines are enough to run the local Docker stack at `http://localhost:8000` and submit your first analysis.

Only `AI_PROVIDER` and `AI_MODEL` are hard-required. `docker-compose.yaml` fails fast without them, and the Helm chart requires `ai.provider` and `ai.model`. `ADMIN_KEY` is optional: the chart auto-generates it on first install. Everything else has a working default.

> **Note:** Fill `AI_MODEL` in from your own deployment. There is no built-in default model list — the catalogue is discovered from the Pi-sidecar at runtime, so a model id copied from elsewhere may not resolve. Run `rootcoz ai-models` (or `GET /api/ai-models`) against a running server and pick an id that is actually listed.

`AI_PROVIDER` and `AI_MODEL` are resolved as a pair at submit time, and the resolution is strict:

- An **exact** `provider/model` pair that appears in the sidecar catalogue is used as-is.
- The friendly names `claude`, `gemini`, and `cursor` are **not** provider IDs. Each maps to a sidecar provider (`claude` to `google-vertex-claude` or `cli-claude`, `gemini` to `google` or `cli-gemini`, `cursor` to any `*-cursor` provider including `cli-cursor`), and that mapping is applied only when the requested model id appears under **exactly one** catalogue provider *and* that provider is the mapped one.
- Anything else is rejected with `Unknown Pi-sidecar provider/model pair`, including an id that appears under two providers.

The practical consequence is that `ANTHROPIC_API_KEY` alone does not authenticate `AI_PROVIDER=claude`: that key authenticates the `anthropic` provider, while the `claude` alias maps to `google-vertex-claude`, so a literal model id such as `claude-sonnet-4` is rejected unless your sidecar happens to expose it solely under `google-vertex-claude`. Check `rootcoz ai-models` before choosing an id, or set `AI_PROVIDER` to the exact provider ID that endpoint lists alongside the model.

## Step-by-Step

1. **Understand the two configuration layers.**

   Environment variables (and `.env`) provide the base values. Settings saved by an admin in the Server Settings UI are stored in the database, encrypted when sensitive, and merged over the environment values at request time. Emptying a field in the UI deletes the override and falls back to the environment value, then to the code default.

   | Layer | Set by | Scope |
   | --- | --- | --- |
   | Environment variable | Deployment (`.env`, ConfigMap, Helm values) | Bootstrap defaults for the whole server |
   | Server Settings | Admin, after login | Overrides one server-wide value, applied without restart for most fields |

   Two further override layers sit above both for analysis settings: the per-request body, and `.rootcoz/settings.json` in the cloned tests repository. Resolution order is request → `settings.json` → Server Settings → environment variable. See [Configuring Analysis Context](configure-analysis-context.html).

2. **Read the tables below as a matrix.**

   Columns:

   | Column | Meaning |
   | --- | --- |
   | Variable | Environment variable name, always the field name uppercased |
   | Default | Value used when nothing else sets it |
   | Secret | Encrypted at rest and masked in API responses and the UI |
   | Restart | Server restart required for a change to take effect |
   | After login | Whether an admin can change it in Server Settings without a restart |

   Fields marked `after login: yes (restart)` are settable in Server Settings but only take effect after a server restart. `DB_PATH` and `DEBUG` remain env-only (`PUT /api/admin/settings` rejects them — they are process bootstrap with no Settings field). `ADMIN_KEY` is env-only too: it has a Settings field for read-only display, but every write path (`PUT`, persist, reset) rejects it — it is a server-only bootstrap secret, never settable at runtime.

3. **Jenkins category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `JENKINS_URL` | empty | string | no | no | yes |
   | `JENKINS_USER` | empty | string | yes | no | yes |
   | `JENKINS_PASSWORD` | empty | string | yes | no | yes |
   | `JENKINS_SSL_VERIFY` | `true` | boolean | no | no | yes |
   | `JENKINS_TIMEOUT` | `30` | integer > 0 | no | no | yes |
   | `JENKINS_ARTIFACTS_MAX_SIZE_MB` | `500` | integer > 0 | no | no | yes |
   | `GET_JOB_ARTIFACTS` | `true` | boolean | no | no | yes |

   An empty `JENKINS_URL` means Jenkins is not configured, and each request can still carry its own credentials.

4. **AI category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `AI_PROVIDER` | empty | string | no | no | yes |
   | `AI_MODEL` | empty | string | no | no | yes |
   | `ENABLE_FREE_MODELS` | `false` | boolean | no | no | **no** |
   | `AMBIENT_AUTH_PROVIDERS` | `google-vertex-claude` | comma-separated provider IDs | no | no | yes |
   | `AI_CALL_TIMEOUT` | `10` | integer > 0 (minutes) | no | no | yes |
   | `MAX_CONCURRENT_AI_CALLS` | `3` | integer > 0 | no | no | yes |
   | `PEER_AI_CONFIGS` | empty | string | no | no | yes |
   | `PEER_ANALYSIS_MAX_ROUNDS` | `3` | integer 1–10 | no | no | yes |
   | `RECORD_TRANSCRIPTS` | `false` | boolean | no | no | yes |

   `AI_PROVIDER` accepts any exact provider ID that pi-sidecar reports, provided the `provider/model` pair appears in its catalogue. `claude`, `gemini`, and `cursor` are convenience aliases that resolve through the mapping described above; they are not the only accepted values. The chart's `ai.provider` accepts the same freedom — any provider ID string, including a custom provider registered through the sidecar agent dir (the chart's install-time credential checks only apply to the three built-in aliases). Values are lowercased on load, and legacy `*-cli` names are normalised to their canonical form. To see what your sidecar actually exposes, read the provider list from `GET /api/ai-models`. `PEER_AI_CONFIGS` uses `provider:model,provider:model`; model names may contain commas inside square brackets, for example `cursor:gpt-5.4[context=272k,reasoning=medium]`.

   > **Note:** An unsupported `AI_PROVIDER` value fails at submit time, immediately, even when the model is not yet resolved. Blank provider strings mean "not configured" and defer the error to a clearer message later.

5. **Jira category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `JIRA_EMAIL` | unset | string | yes | no | yes |
   | `JIRA_API_TOKEN` | unset | string | yes | no | yes |
   | `JIRA_CLOUD_URL` | unset | string | no | no | yes |
   | `JIRA_CLOUD_SSL_VERIFY` | `true` | boolean | no | no | yes |
   | `JIRA_PAT` | unset | string | yes | no | yes |
   | `JIRA_SERVER_URL` | unset | string | no | no | yes |
   | `JIRA_SERVER_SSL_VERIFY` | `true` | boolean | no | no | yes |
   | `JIRA_OAUTH_CLIENT_SECRET` | unset | string | yes | no | yes |
   | `ATLASSIAN_OAUTH_CLOUD_ID` | unset | string | no | no | yes |
   | `ATLASSIAN_OAUTH_CLIENT_ID` | unset | string | no | no | yes |
   | `JIRA_ATST_TOKEN` | unset | string | yes | no | yes |
   | `JIRA_OAUTH_SSL_VERIFY` | `true` | boolean | no | no | yes |
   | `JIRA_SITE_URL` | unset | string | no | no | yes |
   | `JIRA_PROJECT_KEY` | unset | string | no | no | yes |
   | `JIRA_MAX_RESULTS` | `5` | integer > 0 | no | no | yes |
   | `ENABLE_JIRA` | unset | boolean | no | no | yes |
   | `ENABLE_JIRA_ISSUES` | unset | boolean | no | no | yes |

   Each Jira auth method is a **fully self-contained definition** with its own URL and TLS verification — only `JIRA_PROJECT_KEY`, `JIRA_MAX_RESULTS`, `JIRA_SITE_URL`, `ENABLE_JIRA`, and `ENABLE_JIRA_ISSUES` are shared across methods:

   - **`basic`** (Jira Cloud email + API token): `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_CLOUD_URL`, `JIRA_CLOUD_SSL_VERIFY`
   - **`pat`** (Jira Server/Data Center bearer PAT): `JIRA_PAT`, `JIRA_SERVER_URL`, `JIRA_SERVER_SSL_VERIFY`
   - **`oauth`** (Atlassian OAuth 2.0 credential): `JIRA_OAUTH_CLIENT_SECRET`, `ATLASSIAN_OAUTH_CLIENT_ID`, `ATLASSIAN_OAUTH_CLOUD_ID`, `JIRA_OAUTH_SSL_VERIFY`
   - **`atst`** (Atlassian service-account scoped token): `JIRA_ATST_TOKEN`, `ATLASSIAN_OAUTH_CLOUD_ID`, `JIRA_OAUTH_SSL_VERIFY`

   **Extracting the cloud ID (`ATLASSIAN_OAUTH_CLOUD_ID`):** the `oauth` and `atst` methods locate your site at the scoped `api.atlassian.com` gateway by cloud ID — a plain UUID, not your site name and not an Atlassian account ID. Get it with an unauthenticated request to your site:

   ```bash
   curl https://your-site.atlassian.net/_edge/tenant_info
   # {"cloudId": "...", "cloudIdEpoch": ...}
   ```

   The authenticated alternative is `POST https://api.atlassian.com/oauth/token/accessible-resources` with a valid OAuth token, which lists every site the token can access along with its `id` (the cloud ID). **Warning:** an Atlassian *account ID* — shaped `digits:uuid`, like the identifiers shown in your Atlassian profile — is **not** a cloud ID. Pasting one as `ATLASSIAN_OAUTH_CLOUD_ID` silently breaks the connection with a confusing HTTP 404 from the gateway, because no site exists at that gateway path; rootcoz rejects account-ID-shaped values at save/request time with a 422 pointing back to `_edge/tenant_info`.

   There are four connection methods — `oauth`, `basic`, `pat`, and `atst` — and the **Server Settings** `jira_auth_mode` row (UI: `Jira → Auth method`) selects which one is used; there is no `JIRA_AUTH_MODE` environment variable. `oauth` is the Atlassian **OAuth 2.0 credential for service accounts** method: an admin creates the credential in Atlassian Administration (it yields a client ID and a permanent ATOA-prefixed client secret), and rootcoz stores the secret in `JIRA_OAUTH_CLIENT_SECRET` with the client ID in `ATLASSIAN_OAUTH_CLIENT_ID`. The secret is never sent to Jira itself — on each connection rootcoz exchanges it for a short-lived access token (about 1 hour) via the `client_credentials` grant at `https://auth.atlassian.com/oauth/token`, caches it in-process, and sends that token as the Bearer against the scoped `https://api.atlassian.com/ex/jira/{cloud-id}` gateway using REST v3. `JIRA_OAUTH_CLIENT_SECRET`, `ATLASSIAN_OAUTH_CLIENT_ID`, and `ATLASSIAN_OAUTH_CLOUD_ID` are all required for this method — the cloud ID is the method's sole API locator. `JIRA_SITE_URL` supplies `/browse` links (the scoped gateway URL cannot yield a site URL); rootcoz warns when it is unset. Both the token mint and the gateway calls use `JIRA_OAUTH_SSL_VERIFY`. OAuth 2.0 credentials typically carry only the `read:jira-work`/`write:jira-work` scopes, so the connection test probes project visibility (`/rest/api/3/project`) rather than the current-user endpoint (which needs `read:jira-users` and fails with a scope-mismatch 401).

   The `jira_auth_mode` Server Setting explicitly selects the connection method: `oauth` (client ID + client secret — an access token is minted automatically and sent as the Bearer), `basic` (`JIRA_EMAIL` + `JIRA_API_TOKEN` against `JIRA_CLOUD_URL`), `pat` (`JIRA_PAT` as a Server/DC Bearer token against `JIRA_SERVER_URL`), or `atst` (`JIRA_ATST_TOKEN` — an already-minted Atlassian service-account scoped token, sent as Bearer directly against the scoped `https://api.atlassian.com/ex/jira/{cloud-id}` gateway using REST v3; no token exchange, and `ATLASSIAN_OAUTH_CLOUD_ID` is the method's sole API locator with `JIRA_SITE_URL` supplying `/browse` links, exactly like `oauth`). **An auth method selection is required for the Jira integration** — only the selected method's fields are used, with no prefix sniffing, no precedence chain, and no fallback to the other methods' credentials. When no method is selected, Jira is disabled entirely: no credentials are read, `jira_enabled` is false regardless of which credentials are configured, and the connection test reports `No Jira auth method selected — choose one in Server Settings`. When the selected method's fields are missing (URL fields included), the connection test reports the method and the exact missing fields (for example, `Jira auth method 'oauth' is selected but missing: atlassian_oauth_client_id, atlassian_oauth_cloud_id`). Setting `ENABLE_JIRA=false` forces Jira off; setting it to `true` without a selected auth method or a project key logs a warning and stays off. A typical split setup: a local insecure Jira Server via `pat` with `JIRA_SERVER_SSL_VERIFY=false`, and Atlassian Cloud via `oauth` with `JIRA_OAUTH_SSL_VERIFY=true` — the two are configured and verified independently.

   Jira credentials and per-method URLs are **Server Settings only**: `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PAT`, `JIRA_OAUTH_CLIENT_SECRET`, `JIRA_ATST_TOKEN`, `JIRA_CLOUD_URL`, `JIRA_SERVER_URL`, and the three `*_SSL_VERIFY` flags (and the `jira_auth_mode` selection) are read exclusively from the environment and the Server Settings DB row. Analysis requests — API bodies and CLI analyze flags alike — cannot carry or override them. Other Jira fields (`JIRA_PROJECT_KEY`, `JIRA_MAX_RESULTS`, `ATLASSIAN_OAUTH_CLOUD_ID`, `JIRA_SITE_URL`, `ENABLE_JIRA`) remain request-tunable. Per-user Jira tokens saved in Profile Settings are a separate path used only for bug creation, project listing, and chat, and are unaffected.

   **Upgrade note (oauth rename):** the setting previously named `jira_oauth_access_token` (`JIRA_OAUTH_ACCESS_TOKEN`) is now `jira_oauth_client_secret` (`JIRA_OAUTH_CLIENT_SECRET`), reflecting what the value really is: the permanent client secret of an Atlassian OAuth 2.0 credential, not a bearer token. On startup a one-time DB migration copies an existing `jira_oauth_access_token` Server Settings row to the new key (the old row is left in place; the new key wins once set), and the old `JIRA_OAUTH_ACCESS_TOKEN` environment variable still works as a deprecated alias when `JIRA_OAUTH_CLIENT_SECRET` is unset. `ATLASSIAN_OAUTH_CLIENT_ID` changed from an inert pass-through to a required field of the `oauth` method.

   **Upgrade note (per-method URL/SSL split):** the shared `JIRA_URL` and `JIRA_SSL_VERIFY` settings were removed — each auth method now carries its own URL and TLS verification (`jira_cloud_url`/`jira_cloud_ssl_verify` for `basic`, `jira_server_url`/`jira_server_ssl_verify` for `pat`, `jira_oauth_ssl_verify` for `oauth`; the `oauth` method's API locator is `atlassian_oauth_cloud_id`, not a URL). On startup a one-time DB migration copies an existing `jira_url` Server Settings row to both `jira_cloud_url` and `jira_server_url`, and a `jira_ssl_verify` row to all three verify flags (old rows are left in place; new keys win once set). The old `JIRA_URL` environment variable still works as a deprecated alias that feeds both URL fields when their canonical env vars (`JIRA_CLOUD_URL`, `JIRA_SERVER_URL`) are unset, and `JIRA_SSL_VERIFY` feeds all three verify flags when their canonical env vars are unset — canonical names always win. Per-request `jira_url`/`jira_ssl_verify` overrides (API bodies, CLI `--jira-url`/`--jira-ssl-verify`) were removed together with the shared settings.

   **Upgrade note (env selection removed):** older releases selected the auth method via the `JIRA_AUTH_MODE` environment variable (and, before that, inferred it from the configured credentials). Neither exists anymore — a `JIRA_AUTH_MODE` env var is ignored. Deployments that previously relied on it must select the method in the Server Settings UI (`Jira → Auth method`) or via `PUT /api/admin/settings`, or Jira will be disabled.

6. **GitHub category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `GITHUB_TOKEN` | unset | string | yes | no | yes |
   | `TESTS_REPO_URL` | unset | string | no | no | yes |
   | `TESTS_REPO_TOKEN` | unset | string | yes | no | yes |
   | `ENABLE_GITHUB_ISSUES` | unset | boolean | no | no | yes |

   With `ENABLE_GITHUB_ISSUES` unset, GitHub issue creation auto-enables when both `TESTS_REPO_URL` and `GITHUB_TOKEN` are configured. The user-facing feedback dialog stays available whenever the toggle is not explicitly `false`.

7. **Report Portal category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `REPORTPORTAL_URL` | unset | string | no | no | yes |
   | `REPORTPORTAL_API_TOKEN` | unset | string | yes | no | yes |
   | `REPORTPORTAL_PROJECT` | unset | string | no | no | yes |
   | `REPORTPORTAL_VERIFY_SSL` | `true` | boolean | no | no | yes |
   | `ENABLE_REPORTPORTAL` | unset | boolean | no | no | yes |
   | `RP_PUSH_CLASSIFICATIONS` | `true` | boolean | no | no | yes |
   | `RP_PUSH_ROOTCOZ_URL` | `true` | boolean | no | no | yes |
   | `RP_PUSH_TRACKER_LINKS` | `true` | boolean | no | no | yes |
   | `RP_ALLOW_UNREVIEWED_PUSH` | `false` | boolean | no | no | yes |

   Auto-detection mirrors GitHub: with the toggle unset, Report Portal is on when URL, token, and project are all set. `RP_PUSH_CLASSIFICATIONS` maps RootCoz classifications to Report Portal defect types (`PRODUCT_BUG`, `AUTOMATION_BUG`, `SYSTEM_ISSUE`). `RP_ALLOW_UNREVIEWED_PUSH` is the escape hatch for the manual-push review gate: by default a manual push is refused with `409 job has N unreviewed failure(s) — review them before pushing` until every failure in the job is reviewed; set it to `true` to let Report Portal pushes through regardless. It is a server-level toggle (env var or Server Settings) with no request-payload or CLI equivalent.

8. **Datadog category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `DATADOG_API_KEY` | unset | string | yes | no | yes |
   | `DATADOG_APP_KEY` | unset | string | yes | no | yes |
   | `DATADOG_SITE` | unset | string | no | no | yes |
   | `DATADOG_SERVICE` | unset | string | no | no | yes |
   | `DATADOG_ENV` | unset | string | no | no | yes |
   | `ENABLE_DATADOG` | unset | boolean | no | no | yes |
   | `DD_PUSH_CLASSIFICATIONS` | `true` | boolean | no | no | yes |
   | `DD_PUSH_ROOTCOZ_URL` | `true` | boolean | no | no | yes |
   | `DD_PUSH_TRACKER_LINKS` | `true` | boolean | no | no | yes |
   | `DD_ALLOW_UNREVIEWED_PUSH` | `false` | boolean | no | no | yes |

   Same enable semantics as Report Portal: when `ENABLE_DATADOG` is unset, the exporter is enabled automatically once `DATADOG_API_KEY`, `DATADOG_APP_KEY`, **and** `DATADOG_SITE` are all configured; set `ENABLE_DATADOG=false` to force it off. RootCoz posts to the Datadog **v2 event intake** (`https://event-management-intake.{site}/api/v2/events`), which authenticates every request with **both** keys. `DATADOG_API_KEY` is the API key (32 hex characters, sent as the `DD-API-KEY` header) that identifies the organization; `DATADOG_APP_KEY` is the application key (`ddapp…` prefix or exactly 40 hex characters, sent as the `DD-APPLICATION-KEY` header) that authorizes the request. The v2 intake rejects an API key on its own, which is why the exporter stays disabled until both are set. A missing key fails fast with its own message naming that setting. An application key pasted into `DATADOG_API_KEY` is rejected with an error telling you to move it to `DATADOG_APP_KEY`, and a 32-hex API key pasted into `DATADOG_APP_KEY` is rejected the same way. Service Access Tokens (`ddsat…` prefix — no event write scope exists in Datadog) are rejected in either slot. `DATADOG_SITE` is the Datadog site hostname (`datadoghq.eu`, `datadoghq.com`, …) — there is no built-in default, you choose your regional site — and it is used both for the intake host and for the connection test. `DATADOG_SERVICE` and `DATADOG_ENV` optionally feed the `service:` and `env:` tags on emitted events; the tags are omitted when unset. The Server Settings **Test connection** button validates the API key only, with the documented `GET https://api.{site}/api/v1/validate`. There is no documented side-effect-free probe for the event-write permission the intake needs, so the application key is checked for presence and shape (prefix/hex format, right slot, not a Service Access Token) and reported as present; Datadog validates its write permission when the first event is published — a failed first push is the only true test of that permission.

   The three `DD_PUSH_*` toggles mirror the `RP_PUSH_*` trio and control what goes into each event: `DD_PUSH_CLASSIFICATIONS` adds the `tfa.classification` tag, the `[classification]` title prefix, and the classification line in the event message; `DD_PUSH_ROOTCOZ_URL` adds the RootCoz report link to the event message (only when `PUBLIC_BASE_URL` is set); `DD_PUSH_TRACKER_LINKS` adds the tracked-in Jira/GitHub issue links to the event message. Every event is sent with `category: alert` — the v2 schema's `category` is an enum of `change` | `alert` with no generic event category, and a failed test is an alert — and the exporter never sets a severity: classification (root cause) and severity are unrelated axes. Datadog's documented v2 limits are enforced: a title over 500 characters or a message over 4000 is truncated and marked `… [truncated]`, never silently cut. `DD_ALLOW_UNREVIEWED_PUSH` is the Datadog-side escape hatch for the manual-push review gate: by default a manual push is refused with `409 job has N unreviewed failure(s) — review them before pushing` until every failure in the job is reviewed; set it to `true` to let Datadog pushes through regardless. Each exporter's toggle is independent — enabling the Report Portal one does not ungate Datadog, and unknown/future exporters have no toggle and stay gated.

9. **Auth and Security category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `SECURE_COOKIES` | `true` | boolean | no | **yes** | yes (restart) |
   | `TRUST_PROXY_HEADERS` | `false` | boolean | no | **yes** | yes (restart) |
   | `REQUIRE_APPROVAL` | `true` | boolean | no | no | yes |
   | `ADMIN_WAIT_APPROVE_MSG` | empty | string | no | no | yes |
   | `ALLOWED_USERS` | empty | string | no | no | yes |
   | `DEFAULT_USER_ROLE` | `reviewer` | string | no | **yes** | yes (restart) |
   | `SESSION_TTL_HOURS` | `720` | integer 1–87600 | no | no | yes |
   | `AUDIT_RETENTION_DAYS` | `90` | integer 0–36500 | no | no | yes |
   | `AUDIT_MAX_ROWS` | `0` | integer ≥ 0 | no | no | yes |
   | `AUDIT_AUTH_FAILURE_MAX_PER_MINUTE` | `20` | integer ≥ 0 | no | no | yes |
   | `LOGIN_LOCKOUT_ENABLED` | `true` | boolean | no | no | yes |
   | `LOGIN_LOCKOUT_THRESHOLD` | `5` | integer ≥ 0 | no | no | yes |

   `ADMIN_KEY` bootstraps the `admin` superuser that lives outside the database; log in as `admin` with that key. It is environment-only: Server Settings shows it masked (read-only) but rejects any attempt to set, persist, or reset it — keep the env var authoritative. `DEFAULT_USER_ROLE` accepts only `viewer`, `reviewer`, or `operator` — any other value fails validation at startup. `ALLOWED_USERS` is the deployment's **write** allow-list — it exists for the case where a wide population can authenticate (self-registration, or a trusted proxy/SSO through `TRUST_PROXY_HEADERS`) while only a known team should be able to create or modify data. It is a comma-separated list of usernames and is empty by default, which means no username restriction: every authenticated user is then limited only by their role. Setting it does not grant anything — a listed reviewer is still a reviewer — it restricts the write routes to the listed accounts. When it is non-empty it is enforced at two points, not one: at registration, where a username that is not on the list is rejected with `403 Registration is restricted. Contact an admin.` before the account is created; and after authentication, on the protected routes that call the allow-list check, where admins always bypass it and any other off-list or unauthenticated caller gets `403 User not allowed. Contact an administrator to be added to the allow list.` A user who was registered before the list tightened can therefore still hold an account but gets 403 on those routes, and an operator who is not listed loses the writes their role would otherwise allow — which is the intended effect of a non-empty list. It is editable in Server Settings like any other server-wide setting (the deployment env supplies the default, a saved value overrides it) — one console for the server, and an admin bypasses the list so tightening it can never lock them out.

   > **Warning:** Set `SECURE_COOKIES=false` only for local HTTP development. Behind a TLS-terminating proxy, leave it `true` or sessions will not be stored by the browser.

   `TRUST_PROXY_HEADERS=true` makes `X-Forwarded-User` a valid identity for every route, which means the proxy becomes the authentication boundary. Only enable it behind a proxy you control.

   `LOGIN_LOCKOUT_ENABLED` is the switch that turns account lockout on or off at runtime (Server Settings → Auth & Security, no restart): while it is off the threshold is ignored and no account is locked, but accounts locked earlier stay locked until an admin unlocks them — a security control never releases people silently. `LOGIN_LOCKOUT_THRESHOLD` is a different control from the audit cap below, and the two must not be confused: it counts **consecutive failed logins for one account** and locks that account after this many, refusing further attempts (with the same `401` as a wrong key) until an administrator unlocks it. `0` disables lockout entirely. The lock does not expire on its own — an attacker who can wait out a timer is not deterred by one. A lock only refuses **new sign-ins**: existing sessions and API keys keep working, so nobody can use the public login endpoint as a kill-switch for another account's CI access (`disabled`, by contrast, refuses every path). Every lock, unlock, disable and enable is audited, and a locked or disabled account still produces `auth.login_blocked` rows with `reason: locked` / `reason: disabled`.

      `AUDIT_AUTH_FAILURE_MAX_PER_MINUTE` bounds the brute-force end of the audit trail: before storing an `auth.login_failed` or `auth.login_blocked` row, the server counts the rows for those two actions in the last 60 seconds — per username at this limit, and server-wide at ten times it — and once either budget is spent the row is skipped. The server-wide budget is what bounds a distributed flood, and it is deliberately larger than the per-username one: with equal budgets, 20 junk usernames would exhaust the shared allowance and the trail would then lose the rows for a targeted account (say `admin`), which is the evidence that matters. The trail also guarantees at least one row per minute for an attempt against an existing account even when both budgets are spent, so a flood of invented usernames can never erase the first sign that someone is attacking a real one. Those rescued rows are themselves capped at this limit per window server-wide, so the hard ceiling on failed-login rows is 11 × the limit per minute no matter how many accounts exist or who creates them — the rescue cannot be farmed by registering accounts, and it cannot be used to flood the table. The check and the insert run in one transaction, so parallel attempts cannot all pass a stale count. The attempt is still logged as its usual `[AUDIT]` line, one summary line is logged per window with the number of drops, and the running total is reported as `suppressed_auth_events` in `checks.audit` on `GET /api/health`. Only those two actions are throttled — every other action, including a successful `auth.login`, is always stored. `0` disables the limit.

   The audit trail also records the events an operator needs to explain a job or an account after the fact, each as its own id: `analysis.rerun` (target `job`, details `provider`, `model`, `trigger`, and `previous_classification` when the job already had a classified failure), `auth.logout`, `comment.delete` (target `comment`, details `job_id`), and `user.tracker_token_set` / `user.tracker_token_delete` (details name the tracker — `jira` or `github` — never the token). Comment edits are not audited because no comment-edit endpoint exists, and there is no session-revocation endpoint to audit: sessions end by logging out, rotating a key, or deleting the account, and each of those is recorded.

10. **Prow category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `PROW_URL` | empty | string | no | no | yes |
   | `GCS_BUCKET` | empty | string | no | no | yes |

   Both are validated on load rather than transformed. `PROW_URL` must start with `https://`, must contain a hostname, and must not embed credentials (`prow_validation.normalize_prow_url`) — surrounding whitespace is trimmed, but a trailing slash is left in place, so `https://prow.example.com/` and `https://prow.example.com` are both accepted and stay as written. `GCS_BUCKET` must match `[a-z0-9][a-z0-9._-]*` after trimming (`normalize_gcs_bucket`), so a `gs://` prefix is **rejected** rather than stripped; pass the bare bucket name. Both are also request-tunable, so a per-build submission can override the server default.

11. **Server category.**

    | Variable | Default | Type | Secret | Restart | After login |
    | --- | --- | --- | --- | --- | --- |
    | `PUBLIC_BASE_URL` | unset | string | no | no | yes |
    | `ADDITIONAL_REPOS` | empty | string | no | no | yes |
    | `MAX_CONCURRENT_REPO_CLONES` | `10` | integer > 0 | no | no | yes |
    | `WAIT_FOR_COMPLETION` | `true` | boolean | no | no | yes |
    | `POLL_INTERVAL_MINUTES` | `2` | integer > 0 | no | no | yes |
    | `MAX_WAIT_MINUTES` | `0` | integer ≥ 0 | no | no | yes |
    | `METADATA_RULES_FILE` | empty | string | no | **yes** | yes (restart) |
    | `LOG_LEVEL` | empty (`INFO`) | string | no | **yes** | yes (restart) |
    | `ENABLE_AUTO_REVIEW` | `true` | boolean | no | no | yes |
    | `AUTO_PUSH_EXPORTERS` | empty | string | no | no | yes |

    `ADDITIONAL_REPOS` uses `name:url,name:url`, with an optional `:ref` and `@token` suffix per entry; names must be unique. `MAX_WAIT_MINUTES=0` means wait indefinitely. `PUBLIC_BASE_URL` is the only origin RootCoz will build absolute links from — request `Host` and `X-Forwarded-*` headers are never trusted, which is what blocks host-header injection in result and tracker links.

    `METADATA_RULES_FILE` points at a YAML or JSON file of name-based metadata rules. Rules load once and cache for the process lifetime, so edits need a restart. A broken rules file logs a warning and yields no rules rather than failing the server.

    `AUTO_PUSH_EXPORTERS` takes a comma-separated exporter list such as `reportportal`, and only applies when `ENABLE_AUTO_REVIEW` is also on.
   
   `ENABLE_FREE_MODELS` lets every user run the admin-declared free provider+model pairs (Users page → *Free AI models*) on server credentials; the free-providers grant is not needed for admins or users who already have `can_use_server_providers`.

   `AMBIENT_AUTH_PROVIDERS` lists provider IDs that authenticate via ambient credentials (ADC, OAuth, CLI login) and never consume a per-session user API key. They are excluded from the user credential flow even when the sidecar reports them as key-capable — without this, users can store a key the provider ignores (and any garbage key passes verification, because the provider authenticates via ADC regardless). Their models remain available with server credentials. Set it to an empty string to trust the sidecar's capability reporting entirely.

12. **Web Push category.**

    | Variable | Default | Type | Secret | Restart | After login |
    | --- | --- | --- | --- | --- | --- |
    | `VAPID_PUBLIC_KEY` | empty | string | no | **yes** | yes (restart) |
    | `VAPID_PRIVATE_KEY` | empty | string | yes | **yes** | yes |
    | `VAPID_CLAIM_EMAIL` | empty | string | no | **yes** | yes (restart) |

    Leave both keys empty to auto-generate a pair on first use. Setting only one of them logs a partial-configuration warning but does **not** disable Web Push: the missing key is auto-generated and persisted, and the pair in effect may therefore not match what you set. Because notification signing resolves keys through `get_vapid_config()`, which reads environment variables or the generated key file and never the database, a `VAPID_PRIVATE_KEY` saved through Server Settings is not used for signing. Set the keys in the environment to take effect. Note also that changing the public key or claim email invalidates subscriptions already registered with browsers — they are restart-required and best managed at deployment time.

13. **Notifications category** (added with #347).

    | Variable | Default | Type | Secret | Restart | After login |
    | --- | --- | --- | --- | --- | --- |
    | `SMTP_HOST` | empty | string | no | no | yes |
    | `SMTP_PORT` | `587` | integer > 0 | no | no | yes |
    | `SMTP_USER` | empty | string | no | no | yes |
    | `SMTP_PASSWORD` | unset | string | yes | no | yes |
    | `SMTP_FROM` | empty | string | no | no | yes |
    | `ALERT_EMAIL_TO` | empty | string | no | no | yes |
    | `SLACK_WEBHOOK_URL` | unset | string | yes | no | yes |

    Email alerts need `SMTP_HOST` + `ALERT_EMAIL_TO` (from address defaults to `SMTP_USER`, then `rootcoz@<host>`); Slack alerts need `SLACK_WEBHOOK_URL`. The env var stays the deployment default — a saved setting overrides it.

14. **Sidecar category** (added with #347 — restart required).

    The pi-sidecar starts **once** at container boot and reads these from its process environment. A value saved here is persisted to `/data/pi-sidecar/env`, which the container entrypoint sources before starting the sidecar — the same bridge `RECORD_TRANSCRIPTS` uses. An empty field means "no override": the deployment environment governs.

    | Variable | Default | Type | Secret | Restart | After login |
    | --- | --- | --- | --- | --- | --- |
    | `SIDECAR_HOST` | empty | string | no | **yes** | yes (restart) |
    | `SIDECAR_PORT` | empty | string | no | **yes** | yes (restart) |
    | `SIDECAR_URL` | empty | string | no | **yes** | yes (restart) |
    | `ACPX_AGENTS` | empty | string | no | **yes** | yes (restart) |
    | `CLI_AGENTS` | empty | string | no | **yes** | yes (restart) |
    | `PI_SIDECAR_LOG_LEVEL` | empty | string | no | **yes** | yes (restart) |
    | `NODE_BINARY` | empty | string | no | **yes** | yes (restart) |
    | `PI_SIDECAR_TRANSCRIPTS_DIR` | empty | string | no | **yes** | yes (restart) |

    `SIDECAR_HOST` accepts only values the container's loopback health probe can still reach: loopback addresses, `0.0.0.0`/`::` (wildcard binds that also answer on `127.0.0.1`), or empty. A specific non-loopback address is rejected with a `400` on save — the entrypoint probes `http://127.0.0.1:$SIDECAR_PORT` before every boot and aborts when it fails, so such a value would brick every restart. `0.0.0.0` is accepted with a trust warning: the sidecar will listen on an external interface, the same trust class as `TRUST_PROXY_HEADERS` — only do it on a network you control. `SIDECAR_PORT` must be an integer between 1 and 65535. When `SIDECAR_PORT` is overridden and `SIDECAR_URL` is not set, rootcoz derives `SIDECAR_URL=http://127.0.0.1:<port>` so the Python client follows the listener; an explicit `SIDECAR_URL` always wins. Because the derived value is exported into the process environment, `SIDECAR_URL` shows as env-sourced in Server Settings, but the persist path rejects it — persisting the derived URL would pin it and silently beat the port derivation after a later `sidecar_port` change. Persist `sidecar_port` instead.

### Environment variable audit (#347)

Every environment variable rootcoz or the sidecar consumes, in exactly one row. "Setting key" is the Server Settings field (empty field name in the UI clears the override and falls back to the env value). "Runtime" means a saved change applies without a restart; "restart" means it applies at the next boot (sidecar rows apply through the `/data/pi-sidecar/env` bridge).

| Env var | Setting key | Category | Runtime / Restart |
| --- | --- | --- | --- |
| `JENKINS_URL` | `jenkins_url` | Jenkins | runtime |
| `JENKINS_USER` | `jenkins_user` | Jenkins | runtime (sensitive) |
| `JENKINS_PASSWORD` | `jenkins_password` | Jenkins | runtime (sensitive) |
| `JENKINS_SSL_VERIFY` | `jenkins_ssl_verify` | Jenkins | runtime |
| `JENKINS_TIMEOUT` | `jenkins_timeout` | Jenkins | runtime |
| `JENKINS_ARTIFACTS_MAX_SIZE_MB` | `jenkins_artifacts_max_size_mb` | Jenkins | runtime |
| `GET_JOB_ARTIFACTS` | `get_job_artifacts` | Jenkins | runtime |
| `AI_PROVIDER` | `ai_provider` | AI | runtime |
| `AI_MODEL` | `ai_model` | AI | runtime |
| `AI_CALL_TIMEOUT` | `ai_call_timeout` | AI | runtime |
| `MAX_CONCURRENT_AI_CALLS` | `max_concurrent_ai_calls` | AI | runtime |
| `PEER_AI_CONFIGS` | `peer_ai_configs` | AI | runtime |
| `PEER_ANALYSIS_MAX_ROUNDS` | `peer_analysis_max_rounds` | AI | runtime |
| `RECORD_TRANSCRIPTS` | `record_transcripts` | AI | runtime (sidecar live toggle) |
| `ENABLE_FREE_MODELS` | `enable_free_models` | AI | runtime |
| `AMBIENT_AUTH_PROVIDERS` | `ambient_auth_providers` | AI | runtime |
| `ACTIVE_SERVER_PROVIDERS` | `active_server_providers` | AI | runtime |
| `JIRA_EMAIL` | `jira_email` | Jira | runtime (sensitive) |
| `JIRA_API_TOKEN` | `jira_api_token` | Jira | runtime (sensitive) |
| `JIRA_CLOUD_URL` | `jira_cloud_url` | Jira | runtime |
| `JIRA_CLOUD_SSL_VERIFY` | `jira_cloud_ssl_verify` | Jira | runtime |
| `JIRA_PAT` | `jira_pat` | Jira | runtime (sensitive) |
| `JIRA_SERVER_URL` | `jira_server_url` | Jira | runtime |
| `JIRA_SERVER_SSL_VERIFY` | `jira_server_ssl_verify` | Jira | runtime |
| `JIRA_OAUTH_CLIENT_SECRET` | `jira_oauth_client_secret` | Jira | runtime (sensitive) |
| `ATLASSIAN_OAUTH_CLOUD_ID` | `atlassian_oauth_cloud_id` | Jira | runtime |
| `ATLASSIAN_OAUTH_CLIENT_ID` | `atlassian_oauth_client_id` | Jira | runtime |
| `JIRA_ATST_TOKEN` | `jira_atst_token` | Jira | runtime (sensitive) |
| `JIRA_OAUTH_SSL_VERIFY` | `jira_oauth_ssl_verify` | Jira | runtime |
| `JIRA_SITE_URL` | `jira_site_url` | Jira | runtime |
| `JIRA_PROJECT_KEY` | `jira_project_key` | Jira | runtime |
| `JIRA_MAX_RESULTS` | `jira_max_results` | Jira | runtime |
| `ENABLE_JIRA` | `enable_jira` | Jira | runtime |
| `ENABLE_JIRA_ISSUES` | `enable_jira_issues` | Jira | runtime |
| `GITHUB_TOKEN` | `github_token` | GitHub | runtime (sensitive) |
| `TESTS_REPO_URL` | `tests_repo_url` | GitHub | runtime |
| `TESTS_REPO_TOKEN` | `tests_repo_token` | GitHub | runtime (sensitive) |
| `ENABLE_GITHUB_ISSUES` | `enable_github_issues` | GitHub | runtime |
| `REPORTPORTAL_URL` | `reportportal_url` | Report Portal | runtime |
| `REPORTPORTAL_API_TOKEN` | `reportportal_api_token` | Report Portal | runtime (sensitive) |
| `REPORTPORTAL_PROJECT` | `reportportal_project` | Report Portal | runtime |
| `REPORTPORTAL_VERIFY_SSL` | `reportportal_verify_ssl` | Report Portal | runtime |
| `ENABLE_REPORTPORTAL` | `enable_reportportal` | Report Portal | runtime |
| `RP_PUSH_CLASSIFICATIONS` | `rp_push_classifications` | Report Portal | runtime |
| `RP_PUSH_ROOTCOZ_URL` | `rp_push_rootcoz_url` | Report Portal | runtime |
| `RP_PUSH_TRACKER_LINKS` | `rp_push_tracker_links` | Report Portal | runtime |
| `RP_ALLOW_UNREVIEWED_PUSH` | `rp_allow_unreviewed_push` | Report Portal | runtime |
| `DATADOG_API_KEY` | `datadog_api_key` | Datadog | runtime (sensitive) |
| `DATADOG_APP_KEY` | `datadog_app_key` | Datadog | runtime (sensitive) |
| `DATADOG_SITE` | `datadog_site` | Datadog | runtime |
| `DATADOG_SERVICE` | `datadog_service` | Datadog | runtime |
| `DATADOG_ENV` | `datadog_env` | Datadog | runtime |
| `ENABLE_DATADOG` | `enable_datadog` | Datadog | runtime |
| `DD_PUSH_CLASSIFICATIONS` | `dd_push_classifications` | Datadog | runtime |
| `DD_PUSH_ROOTCOZ_URL` | `dd_push_rootcoz_url` | Datadog | runtime |
| `DD_PUSH_TRACKER_LINKS` | `dd_push_tracker_links` | Datadog | runtime |
| `DD_ALLOW_UNREVIEWED_PUSH` | `dd_allow_unreviewed_push` | Datadog | runtime |
| `ADMIN_KEY` | `admin_key` | Auth & Security | env-only (read-only display) |
| `SECURE_COOKIES` | `secure_cookies` | Auth & Security | restart |
| `TRUST_PROXY_HEADERS` | `trust_proxy_headers` | Auth & Security | restart |
| `REQUIRE_APPROVAL` | `require_approval` | Auth & Security | runtime |
| `ADMIN_WAIT_APPROVE_MSG` | `admin_wait_approve_msg` | Auth & Security | runtime |
| `ALLOWED_USERS` | `allowed_users` | Auth & Security | runtime |
| `DEFAULT_USER_ROLE` | `default_user_role` | Auth & Security | restart |
| `SESSION_TTL_HOURS` | `session_ttl_hours` | Auth & Security | runtime |
| `AUDIT_RETENTION_DAYS` | `audit_retention_days` | Auth & Security | runtime |
| `AUDIT_MAX_ROWS` | `audit_max_rows` | Auth & Security | runtime |
| `AUDIT_AUTH_FAILURE_MAX_PER_MINUTE` | `audit_auth_failure_max_per_minute` | Auth & Security | runtime |
| `LOGIN_LOCKOUT_ENABLED` | `login_lockout_enabled` | Auth & Security | runtime |
| `LOGIN_LOCKOUT_THRESHOLD` | `login_lockout_threshold` | Auth & Security | runtime |
| `PROW_URL` | `prow_url` | Prow | runtime |
| `GCS_BUCKET` | `gcs_bucket` | Prow | runtime |
| `PROW_ARTIFACTS_MAX_SIZE_MB` | `prow_artifacts_max_size_mb` | Prow | runtime |
| `PROW_ARTIFACT_MAX_FILE_SIZE_MB` | `prow_artifact_max_file_size_mb` | Prow | runtime |
| `PROW_ARTIFACTS_MAX_OBJECTS` | `prow_artifacts_max_objects` | Prow | runtime |
| `PUBLIC_BASE_URL` | `public_base_url` | Server | runtime |
| `ADDITIONAL_REPOS` | `additional_repos` | Server | runtime |
| `MAX_CONCURRENT_REPO_CLONES` | `max_concurrent_repo_clones` | Server | runtime |
| `WAIT_FOR_COMPLETION` | `wait_for_completion` | Server | runtime |
| `POLL_INTERVAL_MINUTES` | `poll_interval_minutes` | Server | runtime |
| `MAX_WAIT_MINUTES` | `max_wait_minutes` | Server | runtime |
| `METADATA_RULES_FILE` | `metadata_rules_file` | Server | restart |
| `LOG_LEVEL` | `log_level` | Server | restart |
| `ENABLE_AUTO_REVIEW` | `enable_auto_review` | Server | runtime |
| `AUTO_PUSH_EXPORTERS` | `auto_push_exporters` | Server | runtime |
| `VAPID_PUBLIC_KEY` | `vapid_public_key` | Web Push | restart |
| `VAPID_PRIVATE_KEY` | `vapid_private_key` | Web Push | restart (sensitive) |
| `VAPID_CLAIM_EMAIL` | `vapid_claim_email` | Web Push | restart |
| `SMTP_HOST` | `smtp_host` | Notifications | runtime |
| `SMTP_PORT` | `smtp_port` | Notifications | runtime |
| `SMTP_USER` | `smtp_user` | Notifications | runtime |
| `SMTP_PASSWORD` | `smtp_password` | Notifications | runtime (sensitive) |
| `SMTP_FROM` | `smtp_from` | Notifications | runtime |
| `ALERT_EMAIL_TO` | `alert_email_to` | Notifications | runtime |
| `SLACK_WEBHOOK_URL` | `slack_webhook_url` | Notifications | runtime (sensitive) |
| `SIDECAR_HOST` | `sidecar_host` | Sidecar | restart (env bridge) |
| `SIDECAR_PORT` | `sidecar_port` | Sidecar | restart (env bridge) |
| `SIDECAR_URL` | `sidecar_url` | Sidecar | restart (env bridge; derived from `sidecar_port` when unset — not persistable) |
| `ACPX_AGENTS` | `acpx_agents` | Sidecar | restart (env bridge) |
| `CLI_AGENTS` | `cli_agents` | Sidecar | restart (env bridge) |
| `PI_SIDECAR_LOG_LEVEL` | `pi_sidecar_log_level` | Sidecar | restart (env bridge) |
| `NODE_BINARY` | `node_binary` | Sidecar | restart (env bridge) |
| `PI_SIDECAR_TRANSCRIPTS_DIR` | `pi_sidecar_transcripts_dir` | Sidecar | restart (env bridge) |
| `DB_PATH` | — (no setting) | excluded | env-only |
| `ROOTCOZ_ENCRYPTION_KEY` | — (no setting) | excluded | env-only |
| `PORT` | — (no setting) | excluded | env-only |
| `XDG_CONFIG_HOME` / `XDG_DATA_HOME` | — (no setting) | excluded | env-only |
| `DEV_MODE` / `DEBUG` | — (no setting) | excluded | env-only |
| `GEMINI_API_KEY` | — (no setting) | excluded | env-only (sidecar credential) |
| `ANTHROPIC_API_KEY` | — (no setting) | excluded | env-only (sidecar credential) |
| `CURSOR_API_KEY` | — (no setting) | excluded | env-only (sidecar credential) |
| `CLAUDE_CODE_USE_VERTEX` | — (no setting) | excluded | env-only (Vertex toggle) |
| `CLOUD_ML_REGION` | — (no setting) | excluded | env-only (Vertex) |
| `ANTHROPIC_VERTEX_PROJECT_ID` | — (no setting) | excluded | env-only (Vertex) |
| `GOOGLE_APPLICATION_CREDENTIALS` | — (no setting) | excluded | env-only (Vertex gcloud credential mount) |
| `CLOUDSDK_CONFIG` | — (no setting) | excluded | env-only (Vertex gcloud config mount) |
| `PI_SIDECAR_AGENT_DIR` | — (no setting) | excluded | env-only (sidecar agent dir) |
| `PI_SIDECAR_TRANSCRIPTS_MAX_AGE_DAYS` | — (no setting) | excluded | env-only (sidecar pruning) |
| `PI_SIDECAR_TRANSCRIPTS_MAX_SIZE_MB` | — (no setting) | excluded | env-only (sidecar pruning) |

`ADMIN_KEY` also bootstraps the `admin` superuser before any settings exist; keep the env var authoritative for bootstrap. Admin-managed server provider API keys (the `GEMINI_API_KEY` replacement for server-credential sessions) live in the encrypted `server_ai_providers` table, managed under Server Settings → AI — they are not env vars.

### Variables that never reach the Server Settings UI

These are read straight from the process environment and have no `Settings` field, so they are bootstrap-only. Set them in `.env`, the Compose file, the Helm chart, or a ConfigMap.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ROOTCOZ_ENCRYPTION_KEY` | auto-generated file key | Fernet key for at-rest encryption and the HMAC secret for API key hashes |
| `XDG_DATA_HOME` | `~/.local/share` | Where the auto-generated `.encryption_key` file lives |
| `XDG_CONFIG_HOME` | user default | Writable config directory the container points at `/tmp/config` |
| `DEV_MODE` / `DEBUG` | unset | Set to `true` to enable uvicorn auto-reload |
| `PORT` | `8000` | Port the ASGI server binds |
| `ANTHROPIC_API_KEY` | unset | Claude credential for the sidecar |
| `GEMINI_API_KEY` | unset | Gemini credential for the sidecar |
| `CURSOR_API_KEY` | unset | Cursor credential for the sidecar |
| `CLAUDE_CODE_USE_VERTEX` | unset | Set to `1` to authenticate Claude through Vertex AI |
| `CLOUD_ML_REGION` | unset | Vertex region, used with `CLAUDE_CODE_USE_VERTEX` |
| `ANTHROPIC_VERTEX_PROJECT_ID` | unset | Vertex project, used with `CLAUDE_CODE_USE_VERTEX` |
| `GOOGLE_APPLICATION_CREDENTIALS` | unset | Path to the Vertex service-account key file the Helm chart mounts (`/gcloud-credentials/application_default_credentials.json`) |
| `CLOUDSDK_CONFIG` | unset | gcloud config directory for the Vertex credential mount (`/gcloud-credentials` in the chart) |
| `PI_SIDECAR_AGENT_DIR` | unset | Sidecar agent dir for custom providers and settings (see below) |

#### Transcript retention

Recording is **off by default** — an administrator must enable it (Server Settings → AI or `RECORD_TRANSCRIPTS`), and rootcoz never deletes transcript files. Retention is the sidecar's pruning job, controlled by two env vars it reads at container start:

| Variable | Default | Meaning |
|---|---|---|
| `PI_SIDECAR_TRANSCRIPTS_MAX_AGE_DAYS` | `0` (disabled) | Delete transcript files older than this many days |
| `PI_SIDECAR_TRANSCRIPTS_MAX_SIZE_MB` | `0` (disabled) | Keep the transcripts directory under this total size |

Both defaults mean **no pruning**: every recorded session stays until an operator sets a budget or cleans the directory manually. The pruning is best-effort and runs from the sidecar's session activity — at most one scan per minute, only when sessions are created or turns are written — and **active sessions are never pruned**, so a long-running session (or an idle sidecar) can exceed the age or size budget temporarily. Plan disk usage accordingly — a transcript stores the full redacted turn history of its session.

`gemini` resolves to the `google` provider, and `GEMINI_API_KEY` is the credential RootCoz documents and charts for it (`ai.provider=gemini` pairs with `ai.geminiApiKey`). `gemini auth login` is a host-side login for the standalone Gemini CLI; the sidecar exposes that agent under its own provider id — `cli-gemini`, the canonical form of the legacy `gemini-cli` name — not under `google`, so it is not a substitute for `GEMINI_API_KEY` on the `gemini` alias. For `claude`, see the resolution rules above: the alias maps to `google-vertex-claude`, so `ANTHROPIC_API_KEY` on its own is not the credential that provider uses — the Vertex path is. RootCoz passes the environment through to the sidecar and cannot say which credentials a provider accepts, so confirm the requirement in that provider's own documentation.

RootCoz does not read the Vertex trio itself — `config.py` documents them as variables the Claude CLI consumes, and the server only passes them through the process environment to the sidecar. What this repo guarantees is limited to that pass-through. The Helm chart is the more useful reference, because it treats Vertex as a unit: setting `ai.vertex.enabled` writes `CLAUDE_CODE_USE_VERTEX=1`, `CLOUD_ML_REGION`, and `ANTHROPIC_VERTEX_PROJECT_ID`, and separately mounts `ai.vertex.serviceAccountKey` and points `GOOGLE_APPLICATION_CREDENTIALS` at the mounted `application_default_credentials.json`. Install fails outright if `ai.vertex.enabled` is set without `ai.vertex.projectId` or `ai.vertex.serviceAccountKey`, so the chart's own definition of "Vertex enabled" includes a credential file, not just the three variables above.

Whether the provider ends up registered and usable, and what credentials it actually requires, is decided inside the Pi-sidecar and the Anthropic Vertex tooling, not here. For the authoritative credential requirements, follow the Anthropic Claude Code on Vertex AI documentation rather than treating the trio as self-sufficient. This matters for `AI_PROVIDER=claude` in particular, because that alias resolves to the `google-vertex-claude` provider — so Vertex is the path on which `claude` is expected to resolve at all.

### Sidecar custom providers (agent dir)

Pi-sidecar >=4.9.0 reads an **agent dir** at process start: `models.json` registers custom pi providers (listed by `GET /api/ai-models`, usable as `AI_PROVIDER`/`AI_MODEL`), `auth.json` supplies provider credentials, and `settings.json` seeds the sidecar's in-memory settings store. Point the sidecar at a persistent dir with `PI_SIDECAR_AGENT_DIR` (unset means an ephemeral scratch dir, i.e. no custom providers). A `models.json` `apiKey` may reference an environment variable — `"$MYGATEWAY_API_KEY"` resolves against the sidecar process environment, so the variable must be exported on the same container.

Two operational rules follow from "read at process start": changing any file requires a sidecar restart (the Helm chart handles this with a checksum-annotated rolling restart; in Docker Compose restart the container), and `rootcoz ai-models` refresh does not re-read the directory. There is no provider allowlist on the RootCoz side — any exact provider/model pair present in the sidecar catalogue is accepted, so a custom provider is selected the same way as a built-in one.

#### Key-scoped model listing (`trustKeyScopedListing`)

By default the sidecar refuses to send a user's request-scoped key to any host that is not a provider-owned builtin endpoint or a loopback address, so custom providers return no per-key model list. A provider-level `trustKeyScopedListing` flag in `models.json` opts one provider into key-scoped model listing:

- Opt-in, per provider, boolean. Requires pi-sidecar >=4.9.0.
- When `true`, the sidecar performs key-scoped model listing against that provider's configured `baseUrl` with a **user's request-scoped key** — the same trust boundary the manifest's server credential (`apiKey`) already crosses.
- Effect for RootCoz users: a user who stored their own key for that provider gets a **live, per-key model list in the model pickers** (like builtin providers such as `openai`) instead of hand-typing model IDs.
- Only useful when the gateway implements an OpenAI-compatible `/models` endpoint on its `baseUrl`.
- Security: the flag is honored **only** from the admin-controlled `models.json` manifest — request- or session-level input can never set it. The admin thereby declares exactly which hosts may receive user keys.

```json
{
  "providers": {
    "mygateway": {
      "baseUrl": "https://mygateway.example.com/v1",
      "api": "openai-completions",
      "apiKey": "$MYGATEWAY_API_KEY",
      "trustKeyScopedListing": true,
      "models": [
        { "id": "mygateway/gpt-4o", "name": "GPT-4o via MyGateway" }
      ]
    }
  }
}
```

For a flagged provider, the listing response is the union of the live gateway listing and the manifest-configured `models[]` — admin-declared models appear even when the gateway's `/models` endpoint does not list them. When the native listing fails, the flag has no effect.

RootCoz reuses the flag for the **server** credential pool. When the manifest `apiKey` is an environment reference (`"$VAR"` / `"${VAR}"`), RootCoz resolves the variable from its own process environment (the sidecar's container) and captures the provider's key-scoped listing at startup, when an admin opens Server Settings → Server providers (at most once every five minutes), and on a forced model refresh. That listing becomes the provider's offered model set — the same effect a stored key has — with **no stored server key needed**: unless an admin promotes it (see below), the credential stays in the deployment env, is never written to the database, and never appears in a log line or API response. A failed or unsupported re-capture leaves the last captured listing in place, so a sidecar hiccup never reverts the offered models to the static manifest.

In `GET /api/admin/server-providers`, `key_scoped_source` says which credential produced the listing: `"deployment"` for an env-referenced manifest key, `"stored"` for a key added in the UI, and `null` when no key-scoped listing was captured. `has_key` stays `false` for the deployment case because nothing is stored. `Refresh models` on such a provider re-runs the listing with the deployment key, exactly as it does with a stored one. `has_deployment_credential` says whether a deployment credential is resolvable for that provider at all (an env-referenced manifest key or an ambient `<PROVIDER_ID>_API_KEY`), independently of whether a key-scoped listing was ever captured: it drives the `Deployment credentials` badge and the `Store API key` action, so a provider whose sidecar cannot list models can still have its key promoted into the DB. `key_scoped_source` keeps describing the *listing*, not the credential. `can_refresh_models` says whether `Refresh models` applies to that provider: `true` for a stored key or a capturable deployment credential (an env-referenced manifest key, or an ambient `<PROVIDER_ID>_API_KEY` — see below), `false` when neither resolves. A literal (non-env) `apiKey`, an unflagged provider with no ambient variable, or an unset variable produces no capture at all, and a provider whose sidecar reports no key-scoped listing (`modelListingSupported: false`) captures nothing and keeps its static catalog. A deployment credential is only captured for a provider the sidecar reports as supporting session API keys (`supportsSessionApiKey`), the same answer that gates user keys: `trustKeyScopedListing` never overrides it, so an unsupported provider is never sent to the key-scoped listing endpoint (which would answer it with a `400`), keeps its static catalog, reports `can_refresh_models: false`, and its `Refresh models` request is refused with a `400` instead of a sidecar call.

The listing carries every provider the sidecar registers as accepting session API keys, not only the ones with a model in the catalog: a registered provider with no catalog model appears with `models: []`, `supports_key: true`, `has_key: false`, and `can_refresh_models: false`, so an admin can still add a key for it (the models then arrive with the key's own listing). A provider the sidecar reports as not key-capable (`supportsSessionApiKey: false`, e.g. `github-copilot`) is never added.

An admin who wants the deployment credential in the encrypted store instead of the environment uses `Store API key` on that row, which calls `POST /api/admin/server-providers/{provider}/store-deployment-key` with only `{ "verification_model": "<model id>" }`: **nothing is pasted** — the server promotes the credential it already resolves for the provider, verifying it with a real sidecar turn before writing it, and the write is the same encrypted, generation-bumping, listing-capturing, selection-seeding store the add and rotate endpoints use. The response never contains the key, a prefix of it, or the variable's value, and the audit record names only the admin and the provider — as its own action id, `provider.key.promote`, so a promoted deployment credential is distinguishable from a key an admin typed (`provider.key.store`) or replaced (`provider.key.rotate`). Afterwards the row is an ordinary stored-key provider: `has_key` becomes `true`, `key_scoped_source` becomes `"stored"`, and `Rotate` / `Remove` replace the `Store API key` action. A provider with no resolvable credential (unset variable, unreadable manifest, no manifest entry) answers `404`, a provider that already has a stored key answers `409` (rotate instead), and a failed verification is a `200` with `ok: false` and nothing stored.

A provider authenticated from the **environment** needs no manifest at all. Builtin and registered providers are resolved the same way: RootCoz derives the variable name from the provider ID — uppercased, every non-alphanumeric character folded to `_`, suffixed with `_API_KEY` — so `openai` reads `OPENAI_API_KEY`, `xai` reads `XAI_API_KEY`, `mistral` reads `MISTRAL_API_KEY`, and a registered provider `my-gateway` reads `MY_GATEWAY_API_KEY`. `google` (and the `gemini` alias) is the documented exception: `GEMINI_API_KEY` is read first and `GOOGLE_API_KEY` second. Only variables that are set and non-empty resolve, and their values are never logged or persisted. Such a provider's live listing is captured and refreshed exactly like an env-referenced manifest key — `has_key` stays `false`, `key_scoped_source` is `"deployment"`, and `Refresh models` is offered; with the variable unset nothing resolves, so the row keeps its static catalog and offers no `Refresh models`. Cursor's `acpx-`/`cli-` agents are not resolved from `CURSOR_API_KEY` (their auth is the CLI), and a provider the manifest declares is keyed by the manifest, not by an ambient variable.

#### Adding a custom provider

An end-to-end walkthrough for registering a custom provider behind an OpenAI-compatible gateway. The `models.json` below registers the provider, wires its credential to an environment variable, and opts it into key-scoped listing in one snippet:

**1. Write `models.json` into a persistent agent dir** (e.g. `/etc/rootcoz/pi-sidecar-agent/models.json`):

```json
{
  "providers": {
    "mygateway": {
      "baseUrl": "https://mygateway.example.com/v1",
      "api": "openai-completions",
      "apiKey": "$MYGATEWAY_API_KEY",
      "trustKeyScopedListing": true,
      "models": [
        {
          "id": "mygateway/gpt-4o",
          "name": "GPT-4o via MyGateway",
          "contextWindow": 128000,
          "maxTokens": 16384
        },
        {
          "id": "mygateway/llama-4",
          "name": "Llama 4 via MyGateway",
          "contextWindow": 128000,
          "maxTokens": 8192
        }
      ]
    }
  }
}
```

`id` must be prefixed with the provider id (`mygateway/...`). `contextWindow` and `maxTokens` are optional and surface as the model's input/output token limits in the pickers.

**2a. Docker Compose wiring.** Mount the agent dir into the container, point the sidecar at it, and export the referenced key in `.env`:

```yaml
services:
  rootcoz:
    volumes:
      - ./pi-sidecar-agent:/etc/rootcoz/pi-sidecar-agent:ro
    environment:
      PI_SIDECAR_AGENT_DIR: /etc/rootcoz/pi-sidecar-agent
      MYGATEWAY_API_KEY: ${MYGATEWAY_API_KEY}
```

```dotenv
# .env
MYGATEWAY_API_KEY=your-gateway-api-key
```

The `"$MYGATEWAY_API_KEY"` reference in `models.json` resolves against the sidecar process environment, so the variable must be exported on the same container.

**2b. Helm wiring.** Pass the same files as chart values — no manual volume management:

```yaml
sidecar:
  agentDir:
    modelsJson:
      providers:
        mygateway:
          baseUrl: https://mygateway.example.com/v1
          api: openai-completions
          apiKey: $MYGATEWAY_API_KEY
          trustKeyScopedListing: true
          models:
            - id: mygateway/gpt-4o
              name: GPT-4o via MyGateway
              contextWindow: 128000
              maxTokens: 16384
    env:
      MYGATEWAY_API_KEY: "your-gateway-api-key"
```

The chart stores the files in a Secret, copies them into a writable agent dir at `/etc/pi-sidecar-agent` via an initContainer, and sets `PI_SIDECAR_AGENT_DIR`. `sidecar.agentDir.env` supplies the Secret-backed env vars for every `"$VAR"` reference in `models.json`.

**3. Restart the sidecar.** The agent dir is read once at process start — restart the container (Compose) or upgrade the release (the chart's checksum annotation rolls the Deployment automatically).

**4. Verify.** Run `rootcoz ai-models` (or `GET /api/ai-models`): the provider's models appear in the catalogue, and `mygateway` is selectable as `AI_PROVIDER`/`AI_MODEL` like any built-in provider.

> **Warning:** `ROOTCOZ_ENCRYPTION_KEY` is the one variable worth getting right before you have real data. Rotating it invalidates every encrypted stored token *and* every stored API key hash, so all keys must be re-issued afterwards. Existing sessions use plain SHA-256 hashing and survive. If you do not set it, RootCoz generates a file under `$XDG_DATA_HOME/rootcoz/.encryption_key` and warns you in the health endpoint.

### Helm chart coverage

The Helm chart exists for the bootstrap-only variables, since those cannot be set after login:

| Chart value | Variable |
| --- | --- |
| `ai.provider`, `ai.model` | `AI_PROVIDER`, `AI_MODEL` |
| `ai.geminiApiKey` | `GEMINI_API_KEY` |
| `ai.anthropicApiKey` | `ANTHROPIC_API_KEY` |
| `ai.vertex.enabled`, `ai.vertex.projectId`, `ai.vertex.region`, `ai.vertex.serviceAccountKey` | `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`, plus `GOOGLE_APPLICATION_CREDENTIALS` pointing at the mounted GCP key |
| `ai.cursor.apiKey`, `ai.cursor.authJson` | `CURSOR_API_KEY` and the mounted Cursor `auth.json` |
| `sidecar.agentDir.modelsJson`, `sidecar.agentDir.authJson`, `sidecar.agentDir.settingsJson` | `PI_SIDECAR_AGENT_DIR` pointing at a writable emptyDir at `/etc/pi-sidecar-agent`, populated by an initContainer from the rendered Secret (pi writes `models-store.json` next to `models.json`, so the dir must be writable); `sidecar.agentDir.env` adds Secret-backed env vars for `"$VAR"` API key references in `models.json` (omitting a key preserves its stored value on upgrade, `""` removes it; chart-managed variable names are rejected) |
| `admin.key` | `ADMIN_KEY` |
| `encryptionKey` | `ROOTCOZ_ENCRYPTION_KEY` |
| `env.xdgDataHome`, `env.xdgConfigHome` | `XDG_DATA_HOME`, `XDG_CONFIG_HOME` |
| `env.secureCookies`, `env.publicBaseUrl` | `SECURE_COOKIES`, `PUBLIC_BASE_URL`; both auto-derived when left empty, as described below |
| `env.metadataRulesFile` | `METADATA_RULES_FILE` |
| `tuning.logLevel` | `LOG_LEVEL` |
| `route.*`, `ingress.*` | Routing for the deployment, including the derived `PUBLIC_BASE_URL` origin |

`admin.key` and `encryptionKey` are auto-generated on first install when empty. Database-configurable fields deliberately have no chart values — the chart comment says so explicitly, and you set those from Server Settings after the first login.

When `env.secureCookies` and `env.publicBaseUrl` are left empty, the chart derives them in `_helpers.tpl`, and the two do not follow the same rule:

| Configuration | `SECURE_COOKIES` | `PUBLIC_BASE_URL` |
| --- | --- | --- |
| `route.enabled` with `route.host` set | `true` | `https://<route.host>` |
| `route.enabled` with `route.host` empty (the chart default on OpenShift) | `true` | unset (empty) |
| Ingress with `ingress.host` set and `ingress.tls.enabled` | `true` | `https://<ingress.host>` |
| Ingress with `ingress.host` set but **no** TLS | `false` | `http://<ingress.host>` |
| Neither Route nor Ingress enabled | `false` | unset (empty) |

The Ingress requires `ingress.host` — `helm install` fails without it — so the "Ingress enabled with no host" case cannot occur.

So a plain-HTTP Ingress still gets a derived base URL — as an `http://` origin — while `SECURE_COOKIES` stays `false`, because that value only flips to `true` for a Route or a TLS Ingress. Session cookies are consequently issued without the `Secure` attribute on a non-TLS Ingress. Set `env.secureCookies` and `env.publicBaseUrl` explicitly if you terminate TLS upstream of the Ingress or need the cookies hardened anyway.

## Advanced Usage

Change settings after login without touching a restart. Open Server Settings, edit the field, and save. The value is validated against the same Pydantic constraints as the environment variable, encrypted if sensitive, and applied to the running process immediately. Setting a field to an empty string deletes the override.

Sensitive values are returned masked as `••••••••`. `GET /api/admin/settings?reveal_key=ADMIN_KEY` unmasks one key, and `reveal_key=__all__` unmasks everything. Responses also report whether the value came from the database, the environment, or the code default, and flag when an environment variable of the same name also exists. See [API Endpoint Reference](api-reference.html).

Encryption uses Fernet (AES-128-CBC plus HMAC-SHA256). Sensitive fields are `JENKINS_USER`, `JENKINS_PASSWORD`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PAT`, `JIRA_OAUTH_CLIENT_SECRET`, `GITHUB_TOKEN`, `TESTS_REPO_TOKEN`, `REPORTPORTAL_API_TOKEN`, `DATADOG_API_KEY`, `DATADOG_APP_KEY`, `ADMIN_KEY`, and `VAPID_PRIVATE_KEY`. They are encrypted before storage and never logged at any level. They are stripped from ordinary API responses, with one exception an administrator should know about: `GET /api/admin/settings` decrypts them and masks them by default, but returns them in full when called with `reveal_key`. Treat that response as secret-bearing.

Blank strings normalise consistently: optional strings become unset, `AI_PROVIDER` is lowercased and normalised, `DEFAULT_USER_ROLE` is validated against the three allowed roles, and secret strings lose surrounding whitespace — but only eight of them: `GITHUB_TOKEN`, `TESTS_REPO_TOKEN`, `JIRA_API_TOKEN`, `JIRA_PAT`, `JIRA_OAUTH_CLIENT_SECRET`, `REPORTPORTAL_API_TOKEN`, `DATADOG_API_KEY`, and `DATADOG_APP_KEY`. `ADMIN_KEY` and `VAPID_PRIVATE_KEY` are **not** trimmed, so a value pasted with surrounding whitespace keeps it, and a trailing newline — easy to pick up from a mounted Kubernetes Secret or `echo` — stays part of the value and will not match on comparison. Pass those two without surrounding whitespace.

## Troubleshooting
- `AI_PROVIDER is required` at startup.  
  The entrypoint hard-fails without a provider and model. Set both in `.env` and restart the stack.

- `docker compose up` fails but the UI shows no error.  
  The entrypoint exits before the server binds. Run `docker compose logs` and read the first lines.

- `400 Server-only settings (env var only)`.  
  You tried to set an environment-only field from Server Settings. Change it in `.env`, the ConfigMap, or the Helm values and restart.

- A Server Settings change has no effect.  
  Check the `restart_required` marker on the field. `SECURE_COOKIES`, `TRUST_PROXY_HEADERS`, `DEFAULT_USER_ROLE`, `METADATA_RULES_FILE`, and all three Web Push fields only apply after a restart.

- A secret looks like `••••••••` in the API response.  
  That is intentional. Use `reveal_key` to unmask a specific key, and never log the full response.

- `ROOTCOZ_ENCRYPTION_KEY is not set` in `/api/health`.  
  Expected outside production. Set the variable for real deployments; otherwise the key lives in a local file and any container rebuild discards it.

- `Peer config` errors mentioning an unmatched bracket or an empty entry.  
  `PEER_AI_CONFIGS` is comma-separated and bracket-aware. Check for a trailing comma or a model name with an unclosed `[`.

- Metadata rules are ignored after editing the file.  
  `METADATA_RULES_FILE` is cached for the process lifetime. Restart the server.

## Related Pages
- [Deploying RootCoz](deploy-rootcoz.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)
- [Configuring Analysis Context](configure-analysis-context.html)
- [Quickstart](quickstart.html)
- [API Endpoint Reference](api-reference.html)
- [CLI Command Reference](cli-reference.html)