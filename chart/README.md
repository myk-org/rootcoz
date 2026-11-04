# rootcoz Helm Chart

Bootstrap Helm chart for deploying [rootcoz](https://github.com/myk-org/rootcoz) on OpenShift or Kubernetes.

This chart installs rootcoz with routing, AI provider credentials, and admin bootstrap. Configure Jenkins, Jira, GitHub, Report Portal, and other settings via **Server Settings** in the UI after first login.

## Prerequisites

- Kubernetes 1.25+ or OpenShift 4.x
- Helm 3.x
- PersistentVolume provisioner (ReadWriteOnce)
- `kubectl` or `oc`
- [uv](https://docs.astral.sh/uv/) (for the interactive setup script)

## Quick Start (interactive)

```bash
uv run python scripts/helm-setup.py
```

The setup script prompts for an **output directory** (default ``~/.config/rootcoz/helm``, outside the git repo), cluster type, hostname, AI provider, credentials, and the bootstrap **admin API key** (first-login password for username `admin`). It writes:

- `values.generated.yaml` — non-sensitive settings
- `values.secrets.yaml` — API keys (mode 0600)

On re-run, existing files in that directory are loaded and offered as defaults (secrets: leave blank to keep). Writing under the git checkout is refused so secrets are not accidentally committed.

Then runs `helm upgrade --install`.

### Setup Script CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--release` | prompt (`rootcoz`) | Helm release name |
| `--namespace` | prompt (`rootcoz`) | Kubernetes namespace |
| `--output-dir` | prompt (`~/.config/rootcoz/helm`) | Directory for values files (must be outside the git repo) |
| `--dry-run` | — | Pass `--dry-run` to Helm (no cluster changes) |
| `--skip-helm` | — | Only write values files, do not run Helm |

### Troubleshooting

**`Failed to parse ... values.secrets.yaml` / special characters (`#x001b`)**

Older runs could capture Insert-key escapes into secrets. New prompts sanitize input; on load, setup also strips escapes from on-disk YAML, rewrites the file when possible (secrets stay mode `0600`), warns on stderr, and re-prompts for blanked secrets.

- If the warning says **in memory only**, the file was not writable — fix permissions or delete the file, then re-run.
- If setup still cannot parse the file, delete the affected values file and re-run.

## Manual Install

### OpenShift (Route, default)

```bash
helm upgrade --install rootcoz ./chart \
  --namespace rootcoz --create-namespace \
  -f chart/examples/values-openshift.yaml \
  -f ~/.config/rootcoz/helm/values.secrets.yaml
```

Set AI credentials in your values files. Optionally set `route.host` for a custom hostname (if omitted or empty, OpenShift auto-generates one).

### Vanilla Kubernetes (Ingress + TLS)

```bash
helm upgrade --install rootcoz ./chart \
  --namespace rootcoz --create-namespace \
  -f chart/examples/values-kubernetes.yaml \
  -f ~/.config/rootcoz/helm/values.secrets.yaml
```

Disable Route and enable Ingress in values (`route.enabled=false`, `ingress.enabled=true`).

### ClusterIP only (port-forward)

```bash
helm upgrade --install rootcoz ./chart \
  --namespace rootcoz --create-namespace \
  -f chart/examples/values-clusterip.yaml \
  -f ~/.config/rootcoz/helm/values.secrets.yaml

kubectl port-forward svc/rootcoz 8000:8000 -n rootcoz
```

`SECURE_COOKIES` defaults to `false` when no Route or TLS Ingress is configured.

### Private image registry

If the cluster cannot pull from GHCR anonymously, create a pull secret and set:

```yaml
imagePullSecrets:
  - name: ghcr-pull-secret
```

## Required Values

| Value | Env var | Notes |
|-------|---------|-------|
| `ai.provider` | `AI_PROVIDER` | Any sidecar provider ID: `gemini`, `claude`, `cursor`, or a custom provider registered via the agent dir |
| `ai.model` | `AI_MODEL` | Model name for the provider |
| Provider credential | see below | Required for AI analysis |

> **Note:** `ai.provider` and `ai.model` are enforced on initial install only. On upgrade, existing values are preserved if not overridden.

Provider credentials (typically in `~/.config/rootcoz/helm/values.secrets.yaml`):

| Provider | Values key | Env var |
|----------|------------|---------|
| Gemini | `ai.geminiApiKey` | `GEMINI_API_KEY` |
| Claude (API) | `ai.anthropicApiKey` | `ANTHROPIC_API_KEY` |
| Claude (Vertex) | `ai.vertex.serviceAccountKey` + `ai.vertex.projectId` | mounted SA JSON |
| Cursor | `ai.cursor.apiKey` and/or `ai.cursor.authJson` | `CURSOR_API_KEY` + auth mount |
| Datadog (optional) | `datadog.apiKey` + `datadog.appKey` | `DATADOG_API_KEY` + `DATADOG_APP_KEY` |
| Custom (sidecar agent dir) | `sidecar.agentDir.modelsJson` etc. | `PI_SIDECAR_AGENT_DIR` + agent-dir mount |

### Sidecar custom providers (agent dir)

pi-sidecar >=4.9.0 can register custom pi providers from a persistent agent dir.
Set any of `sidecar.agentDir.modelsJson` / `authJson` / `settingsJson` (string or
YAML object) — the chart stores them in a Secret and copies them via an
initContainer into a writable emptyDir mounted at `/etc/pi-sidecar-agent` (pi
persists `models-store.json` next to `models.json`, so the agent dir must be
writable — a direct read-only Secret mount breaks provider availability with
EROFS), and sets `PI_SIDECAR_AGENT_DIR` accordingly. API keys
referenced as `"$VAR"` inside `models.json` come from `sidecar.agentDir.env`
(e.g. `env: { MYGATEWAY_API_KEY: "..." }`). Env keys use tombstone semantics:
omitting a key preserves its stored Secret value on upgrade (Helm `lookup`),
while setting it to `""` removes it. Keys matching chart-managed variable
names (`AI_PROVIDER`, `SECURE_COOKIES`, `ADMIN_KEY`, …) are rejected at render
time. Files are read once at sidecar start;
changes roll the Deployment via checksum annotations. Example:

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
    env:
      MYGATEWAY_API_KEY: "..."
```

Custom providers appear in the model catalog (`GET /api/ai-models`, or `rootcoz ai-models`)
and are selectable as `ai.provider` / `ai.model` like any built-in provider.

#### Waiver writes need a WaiverDB permission

Mounting a credential is necessary but not sufficient for waivers: the rootcoz service account must also hold **write permission for `rootcoz.*` testcases in WaiverDB**. Without it, waiver submission fails with HTTP 403 while the ResultsDB result pushes in the same run still succeed, so the symptom is a *partially* successful push. Result-only deployments do not need it.

## Greenwave exporter auth files (Kerberos / mTLS)

The Greenwave exporter's auth mode (`greenwave_auth_mode`, Server Settings →
Greenwave, DB-only like `jira_auth_mode`) selects `bearer | oidc | kerberos |
mtls | basic | none`. Two of those modes need **files** that Server Settings
cannot hold — it takes a path. The chart mounts them:

```yaml
greenwave:
  keytabBase64: "" # base64 of a Kerberos keytab — RECOMMENDED for keytabs (see below)
  keytab: ""        # raw keytab content — text-safe content only
  clientCert: ""    # mTLS client certificate (PEM)
  clientKey: ""     # mTLS client private key (PEM)
  caBundle: ""      # custom CA bundle (PEM) for the Greenwave endpoint
  mountPath: /var/run/secrets/rootcoz/greenwave
```

All values are optional; unset values render nothing (no Secret, no volume).
Files land in one Secret (`<release>-greenwave-auth`) mounted read-only at
`greenwave.mountPath` with file names `keytab`, `client.crt`, `client.key`,
`ca.crt`, mode `0440` (group-readable, never world-readable). The container
runs in GID 0 (OpenShift arbitrary-UID convention), so no `fsGroup` or
`runAsUser` override is needed — the same ownership model as the Cursor
auth secret. If you run with a custom `podSecurityContext` that changes the
pod's group, ensure the container user can still read the mounted files.

After install, set the **paths** in Server Settings → Greenwave:

| Server Setting | Value |
|----------------|-------|
| `greenwave_keytab_path` | `/var/run/secrets/rootcoz/greenwave/keytab` |
| `greenwave_client_cert` | `/var/run/secrets/rootcoz/greenwave/client.crt` |
| `greenwave_client_key` | `/var/run/secrets/rootcoz/greenwave/client.key` |
| `greenwave_ca_bundle` | `/var/run/secrets/rootcoz/greenwave/ca.crt` |

The auth mode itself is **not** a chart value — set `greenwave_auth_mode` in
Server Settings. Omitting a previously set file on upgrade preserves the
existing Secret value (Helm `lookup`), same as Vertex SA JSON and Cursor
`auth.json`.

### Keytabs are binary — use `keytabBase64`

A Kerberos keytab is binary and almost never valid UTF-8, so do **not** pass
it with `--set-file greenwave.keytab=./my.keytab` or as a raw YAML string —
helm stores/re-renders the mangled value, and a later
`helm upgrade --reuse-values` that does not re-pass the file writes the
mangled value over the good Secret, breaking every Kerberos push. Instead,
base64-encode it yourself; the chart writes it to the Secret `data:` key
as-is (it is already base64, so it is never re-encoded):

```bash
helm install rootcoz chart/ ... --set greenwave.keytabBase64=$(base64 -w0 my.keytab)
```

`greenwave.keytab` remains for text-safe content only, and the two are
mutually exclusive — setting both fails the render with a clear message.
The mTLS entries (`clientCert` / `clientKey` / `caBundle`) are PEM text, so
raw values or `--set-file` are fine there.

## Bootstrap Secrets

The interactive setup script **requires** an `admin.key` (bootstrap admin API key / first-login password). Manual installs may omit `admin.key` and `encryptionKey` — both are auto-generated on first install and preserved across upgrades (Helm `lookup`).

Retrieve an auto-generated admin API key after install (only needed when you did not set `admin.key`):

```bash
kubectl get secret rootcoz-secret -n rootcoz \
  -o jsonpath='{.data.ADMIN_KEY}' | base64 -d; echo
```

Login: username `admin`, password = API key above.

**Warning:** Do not delete the encryption secret (`rootcoz-encryption-key`) independently — the database becomes unreadable.

AI API keys, Vertex SA JSON, Cursor `auth.json`, and sidecar agent-dir files supplied via values are stored in Kubernetes Secrets **and** in the Helm release Secret (`sh.helm.release.v1.*`). For stricter environments, inject credentials with External Secrets / Sealed Secrets and omit plaintext keys from Helm values where possible. On upgrade, omitting a previously set AI key / mount credential preserves the existing Secret via Helm `lookup`.

## Production Sizing

Default resources (`1 CPU / 4Gi` request, `2 CPU / 8Gi` limit) suit evaluation clusters. For concurrent AI analysis workloads, use:

```yaml
resources:
  requests:
    cpu: "4"
    memory: 32Gi
  limits:
    cpu: "4"
    memory: 32Gi
```

## Constraints

- **Single replica only** — SQLite on ReadWriteOnce PVC; not configurable
- **Recreate strategy** — prevents two pods mounting the same PVC
- **Route XOR Ingress** — cannot enable both; both may be disabled

## Upgrade

```bash
helm upgrade rootcoz ./chart -n rootcoz \
  -f ~/.config/rootcoz/helm/values.generated.yaml \
  -f ~/.config/rootcoz/helm/values.secrets.yaml
```

AI credential keys in values apply on upgrade. Auto-generated `ADMIN_KEY` and `ROOTCOZ_ENCRYPTION_KEY` are never rotated by Helm upgrade. Vertex SA JSON and Cursor `auth.json` are also preserved via `lookup` when omitted from values. Deployment pod-template checksums track values changes (salted with the release revision). Out-of-band Secret edits (e.g. via External Secrets) require `kubectl rollout restart`.

To rotate encryption key: delete the secret manually, then reinstall (destroys encrypted DB credentials — re-issue API keys).

## Uninstall

```bash
helm uninstall <release> -n <namespace>
```

The PVC is retained (`helm.sh/resource-policy: keep`) to prevent accidental data loss.

**Full cleanup** (removes PVC data and namespace):

```bash
helm uninstall <release> -n <namespace>
kubectl delete pvc <release>-data -n <namespace>
kubectl delete namespace <namespace>
```

## Optional Features

### OpenShift ImageStream

```yaml
imageStream:
  enabled: true
  name: rootcoz
```

When enabled, the deployment pulls from the internal registry ImageStream instead of GHCR directly.

## Validation

```bash
helm lint chart/
uvx --with tox-uv tox -e chart
helm test rootcoz -n rootcoz   # requires running release
```

## Limitations

- `helm template` offline renders empty auto-generated secrets (`lookup` requires a live cluster)
- Provider-conditional required fields are validated at runtime, not by JSON Schema alone
