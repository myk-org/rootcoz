# Configuration Reference

RootCoz reads its configuration from environment variables at startup and lets admins override most of it later from the Server Settings page. This page lists every variable, its default, whether it is a secret, and whether a restart is needed — organised into the same categories the Server Settings UI uses.

## Prerequisites
- A running or planned RootCoz deployment. See [Deploying RootCoz](deploy-rootcoz.html) for the Kubernetes and OpenShift paths.
- Admin access if you plan to change settings after login. See [Managing Users and Server Settings](manage-users-and-server-settings.html).
- One AI credential for `claude`, `gemini`, or `cursor` if you want analysis to run at all.

## Quick Example

```dotenv
AI_PROVIDER=claude
AI_MODEL=claude-sonnet-4
ANTHROPIC_API_KEY=your-anthropic-api-key
DEFAULT_USER_ROLE=operator
REQUIRE_APPROVAL=false
SECURE_COOKIES=false
```

Those six lines are enough to run the local Docker stack at `http://localhost:8000` and submit your first analysis.

Only `AI_PROVIDER` and `AI_MODEL` are hard-required. `docker-compose.yaml` fails fast without them, and the Helm chart requires `ai.provider` and `ai.model`. `ADMIN_KEY` is optional: the chart auto-generates it on first install. Everything else has a working default.

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

   Fields marked `after login: no` are server-only. `PUT /api/admin/settings` rejects them with `400 Server-only settings (env var only)`, so they must come from the environment.

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
   | `FORCE_SERVER_CREDENTIALS` | `false` | boolean | no | no | yes |
   | `AI_CALL_TIMEOUT` | `10` | integer > 0 (minutes) | no | no | yes |
   | `MAX_CONCURRENT_AI_CALLS` | `3` | integer > 0 | no | no | yes |
   | `PEER_AI_CONFIGS` | empty | string | no | no | yes |
   | `PEER_ANALYSIS_MAX_ROUNDS` | `3` | integer 1–10 | no | no | yes |

   `AI_PROVIDER` accepts `claude`, `gemini`, or `cursor`. Values are lowercased on load, and legacy `*-cli` names are normalised to their canonical form. `PEER_AI_CONFIGS` uses `provider:model,provider:model`; model names may contain commas inside square brackets, for example `cursor:gpt-5.4[context=272k,reasoning=medium]`.

   > **Note:** An unsupported `AI_PROVIDER` value fails at submit time, immediately, even when the model is not yet resolved. Blank provider strings mean "not configured" and defer the error to a clearer message later.

5. **Jira category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `JIRA_URL` | unset | string | no | no | yes |
   | `JIRA_EMAIL` | unset | string | yes | no | yes |
   | `JIRA_API_TOKEN` | unset | string | yes | no | yes |
   | `JIRA_PAT` | unset | string | yes | no | yes |
   | `JIRA_PROJECT_KEY` | unset | string | no | no | yes |
   | `JIRA_SSL_VERIFY` | `true` | boolean | no | no | yes |
   | `JIRA_MAX_RESULTS` | `5` | integer > 0 | no | no | yes |
   | `ENABLE_JIRA` | unset | boolean | no | no | yes |
   | `ENABLE_JIRA_ISSUES` | unset | boolean | no | no | **no** |

   `JIRA_EMAIL` decides the auth mode. With an email, RootCoz uses Jira Cloud Basic auth and prefers `JIRA_API_TOKEN`, falling back to `JIRA_PAT`. Without an email it treats the server as Server/DC, prefers `JIRA_PAT`, and falls back to `JIRA_API_TOKEN`. Setting `ENABLE_JIRA=false` forces Jira off; setting it to `true` without a URL, credentials, or project key logs a warning and stays off.

6. **GitHub category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `GITHUB_TOKEN` | unset | string | yes | no | yes |
   | `TESTS_REPO_URL` | unset | string | no | no | yes |
   | `TESTS_REPO_TOKEN` | unset | string | yes | no | yes |
   | `ENABLE_GITHUB_ISSUES` | unset | boolean | no | no | **no** |

   With `ENABLE_GITHUB_ISSUES` unset, GitHub issue creation auto-enables when both `TESTS_REPO_URL` and `GITHUB_TOKEN` are configured. The user-facing feedback dialog stays available whenever the toggle is not explicitly `false`.

7. **Report Portal category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `REPORTPORTAL_URL` | unset | string | no | no | yes |
   | `REPORTPORTAL_API_TOKEN` | unset | string | yes | no | yes |
   | `REPORTPORTAL_PROJECT` | unset | string | no | no | yes |
   | `REPORTPORTAL_VERIFY_SSL` | `true` | boolean | no | no | yes |
   | `ENABLE_REPORTPORTAL` | unset | boolean | no | no | **no** |
   | `RP_PUSH_CLASSIFICATIONS` | `true` | boolean | no | no | **no** |
   | `RP_PUSH_ROOTCOZ_URL` | `true` | boolean | no | no | **no** |
   | `RP_PUSH_TRACKER_LINKS` | `true` | boolean | no | no | **no** |

   Auto-detection mirrors GitHub: with the toggle unset, Report Portal is on when URL, token, and project are all set. `RP_PUSH_CLASSIFICATIONS` maps RootCoz classifications to Report Portal defect types (`PRODUCT_BUG`, `AUTOMATION_BUG`, `SYSTEM_ISSUE`).

8. **Auth and Security category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `ADMIN_KEY` | empty | string | yes | no | yes |
   | `SECURE_COOKIES` | `true` | boolean | no | **yes** | **no** |
   | `TRUST_PROXY_HEADERS` | `false` | boolean | no | **yes** | **no** |
   | `REQUIRE_APPROVAL` | `true` | boolean | no | no | **no** |
   | `ADMIN_WAIT_APPROVE_MSG` | empty | string | no | no | **no** |
   | `ALLOWED_USERS` | empty | string | no | no | **no** |
   | `DEFAULT_USER_ROLE` | `reviewer` | string | no | **yes** | **no** |

   `ADMIN_KEY` bootstraps the `admin` superuser that lives outside the database; log in as `admin` with that key. `DEFAULT_USER_ROLE` accepts only `viewer`, `reviewer`, or `operator` — any other value fails validation at startup. `ALLOWED_USERS` is a comma-separated allow list evaluated only after authentication, and it is empty (open) by default.

   > **Warning:** Set `SECURE_COOKIES=false` only for local HTTP development. Behind a TLS-terminating proxy, leave it `true` or sessions will not be stored by the browser.

   `TRUST_PROXY_HEADERS=true` makes `X-Forwarded-User` a valid identity for every route, which means the proxy becomes the authentication boundary. Only enable it behind a proxy you control.

9. **Prow category.**

   | Variable | Default | Type | Secret | Restart | After login |
   | --- | --- | --- | --- | --- | --- |
   | `PROW_URL` | empty | string | no | no | yes |
   | `GCS_BUCKET` | empty | string | no | no | yes |

   Both are normalised on load, so trailing slashes and bucket prefixes are stripped rather than rejected. Both are also request-tunable, so a per-build submission can override the server default.

10. **Server category.**

    | Variable | Default | Type | Secret | Restart | After login |
    | --- | --- | --- | --- | --- | --- |
    | `PUBLIC_BASE_URL` | unset | string | no | no | **no** |
    | `ADDITIONAL_REPOS` | empty | string | no | no | yes |
    | `MAX_CONCURRENT_REPO_CLONES` | `10` | integer > 0 | no | no | yes |
    | `WAIT_FOR_COMPLETION` | `true` | boolean | no | no | yes |
    | `POLL_INTERVAL_MINUTES` | `2` | integer > 0 | no | no | yes |
    | `MAX_WAIT_MINUTES` | `0` | integer ≥ 0 | no | no | yes |
    | `METADATA_RULES_FILE` | empty | string | no | **yes** | **no** |
    | `ENABLE_AUTO_REVIEW` | `true` | boolean | no | no | **no** |
    | `AUTO_PUSH_EXPORTERS` | empty | string | no | no | **no** |

    `ADDITIONAL_REPOS` uses `name:url,name:url`, with an optional `:ref` and `@token` suffix per entry; names must be unique. `MAX_WAIT_MINUTES=0` means wait indefinitely. `PUBLIC_BASE_URL` is the only origin RootCoz will build absolute links from — request `Host` and `X-Forwarded-*` headers are never trusted, which is what blocks host-header injection in result and tracker links.

    `METADATA_RULES_FILE` points at a YAML or JSON file of name-based metadata rules. Rules load once and cache for the process lifetime, so edits need a restart. A broken rules file logs a warning and yields no rules rather than failing the server.

    `AUTO_PUSH_EXPORTERS` takes a comma-separated exporter list such as `reportportal`, and only applies when `ENABLE_AUTO_REVIEW` is also on.

11. **Web Push category.**

    | Variable | Default | Type | Secret | Restart | After login |
    | --- | --- | --- | --- | --- | --- |
    | `VAPID_PUBLIC_KEY` | empty | string | no | **yes** | **no** |
    | `VAPID_PRIVATE_KEY` | empty | string | yes | **yes** | yes |
    | `VAPID_CLAIM_EMAIL` | empty | string | no | **yes** | **no** |

    Leave both keys empty to auto-generate a pair on first use. Setting only one of them logs a partial-configuration warning and disables Web Push. Note the asymmetry: the private key is changeable from the UI, but the public key and claim email are environment-only, because changing them would invalidate subscriptions already registered with browsers.

### Variables that never reach the Server Settings UI

These are read straight from the process environment and have no `Settings` field, so they are bootstrap-only. Set them in `.env`, the Compose file, the Helm chart, or a ConfigMap.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ROOTCOZ_ENCRYPTION_KEY` | auto-generated file key | Fernet key for at-rest encryption and the HMAC secret for API key hashes |
| `XDG_DATA_HOME` | `~/.local/share` | Where the auto-generated `.encryption_key` file lives |
| `XDG_CONFIG_HOME` | user default | Writable config directory the container points at `/tmp/config` |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DEV_MODE` / `DEBUG` | unset | Set to `true` to enable uvicorn auto-reload |
| `PORT` | `8000` | Port the ASGI server binds |
| `ANTHROPIC_API_KEY` | unset | Claude credential for the sidecar |
| `GEMINI_API_KEY` | unset | Gemini credential for the sidecar |
| `CURSOR_API_KEY` | unset | Cursor credential for the sidecar |
| `CLAUDE_CODE_USE_VERTEX` | unset | Set to `1` to authenticate Claude through Vertex AI |
| `CLOUD_ML_REGION` | unset | Vertex region, used with `CLAUDE_CODE_USE_VERTEX` |
| `ANTHROPIC_VERTEX_PROJECT_ID` | unset | Vertex project, used with `CLAUDE_CODE_USE_VERTEX` |
| `ACPX_AGENTS` | unset | Comma-separated providers exposed through the Cursor ACPX bridge |
| `CLI_AGENTS` | unset | Comma-separated providers exposed through the local CLI agents |

Only one of `ANTHROPIC_API_KEY` or the Vertex trio is needed for `claude`. Gemini can also authenticate via `gemini auth login`, which needs no variable at all.

> **Warning:** `ROOTCOZ_ENCRYPTION_KEY` is the one variable worth getting right before you have real data. Rotating it invalidates every encrypted stored token *and* every stored API key hash, so all keys must be re-issued afterwards. Existing sessions use plain SHA-256 hashing and survive. If you do not set it, RootCoz generates a file under `$XDG_DATA_HOME/rootcoz/.encryption_key` and warns you in the health endpoint.

### Helm chart coverage

The Helm chart exists for the bootstrap-only variables, since those cannot be set after login:

| Chart value | Variable |
| --- | --- |
| `ai.provider`, `ai.model` | `AI_PROVIDER`, `AI_MODEL` |
| `ai.geminiApiKey` | `GEMINI_API_KEY` |
| `ai.anthropicApiKey` | `ANTHROPIC_API_KEY` |
| `ai.vertex.enabled`, `ai.vertex.projectId`, `ai.vertex.region`, `ai.vertex.serviceAccountKey` | `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`, and a mounted GCP key |
| `ai.cursor.apiKey`, `ai.cursor.authJson` | `CURSOR_API_KEY` and the mounted Cursor `auth.json` |
| `admin.key` | `ADMIN_KEY` |
| `encryptionKey` | `ROOTCOZ_ENCRYPTION_KEY` |
| `env.xdgDataHome`, `env.xdgConfigHome` | `XDG_DATA_HOME`, `XDG_CONFIG_HOME` |
| `env.secureCookies`, `env.publicBaseUrl` | `SECURE_COOKIES`, `PUBLIC_BASE_URL`; both auto-derived from Route or TLS Ingress when left empty |
| `env.metadataRulesFile` | `METADATA_RULES_FILE` |
| `tuning.logLevel` | `LOG_LEVEL` |
| `route.*`, `ingress.*` | Routing for the deployment, including the derived `PUBLIC_BASE_URL` origin |

`admin.key` and `encryptionKey` are auto-generated on first install when empty. Database-configurable fields deliberately have no chart values — the chart comment says so explicitly, and you set those from Server Settings after the first login.

## Advanced Usage

Change settings after login without touching a restart. Open Server Settings, edit the field, and save. The value is validated against the same Pydantic constraints as the environment variable, encrypted if sensitive, and applied to the running process immediately. Setting a field to an empty string deletes the override.

Sensitive values are returned masked as `••••••••`. `GET /api/admin/settings?reveal_key=ADMIN_KEY` unmasks one key, and `reveal_key=__all__` unmasks everything. Responses also report whether the value came from the database, the environment, or the code default, and flag when an environment variable of the same name also exists. See [API Endpoint Reference](api-reference.html).

Encryption uses Fernet (AES-128-CBC plus HMAC-SHA256). Sensitive fields are `JENKINS_USER`, `JENKINS_PASSWORD`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PAT`, `GITHUB_TOKEN`, `TESTS_REPO_TOKEN`, `REPORTPORTAL_API_TOKEN`, `ADMIN_KEY`, and `VAPID_PRIVATE_KEY`. They are encrypted before storage, stripped from API responses, and never logged at any level.

Blank strings normalise consistently: optional strings become unset, `AI_PROVIDER` is lowercased and normalised, `DEFAULT_USER_ROLE` is validated against the three allowed roles, and secret strings lose surrounding whitespace.

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