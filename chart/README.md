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
| `ai.provider` | `AI_PROVIDER` | `gemini`, `claude`, or `cursor` |
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

## Bootstrap Secrets

The interactive setup script **requires** an `admin.key` (bootstrap admin API key / first-login password). Manual installs may omit `admin.key` and `encryptionKey` — both are auto-generated on first install and preserved across upgrades (Helm `lookup`).

Retrieve an auto-generated admin API key after install (only needed when you did not set `admin.key`):

```bash
kubectl get secret rootcoz-secret -n rootcoz \
  -o jsonpath='{.data.ADMIN_KEY}' | base64 -d; echo
```

Login: username `admin`, password = API key above.

**Warning:** Do not delete the encryption secret (`rootcoz-encryption-key`) independently — the database becomes unreadable.

AI API keys, Vertex SA JSON, and Cursor `auth.json` supplied via values are stored in Kubernetes Secrets **and** in the Helm release Secret (`sh.helm.release.v1.*`). For stricter environments, inject credentials with External Secrets / Sealed Secrets and omit plaintext keys from Helm values where possible. On upgrade, omitting a previously set AI key / mount credential preserves the existing Secret via Helm `lookup`.

## Greenwave / ResultsDB / WaiverDB

Greenwave is a release-gating integration and remains disabled until `greenwave.enabled: true` sets the environment-only `ENABLE_GREENWAVE=true` safety gate. Configuring a URL or credential in Server Settings does **not** enable it. `greenwave.pushWaivers` and `greenwave.allowAiWaivers` are also environment-only safety controls; the remaining fields can be bootstrapped with Helm and later managed in **Server Settings**.

To use `AUTO_PUSH_EXPORTERS=greenwave`, set `subjectTemplate` so the exporter can construct the build NVR from push context. Without it, auto-push is rejected at config load. Manual pushes via the API/CLI/report-page can always supply an explicit `--subject-identifier` instead.

Minimal token-authenticated ResultsDB setup:

```yaml
greenwave:
  enabled: true
  url: https://resultsdb.example.com/api/v2.0
  resultsdbAuthMethod: token
  apiToken: "..." # keep in a mode-0600 secrets values file
```

WaiverDB must be configured explicitly. A missing URL or product version causes `greenwave_enabled` to return `False` (HTTP 400 from the push endpoint) rather than silently skipping waivers. A missing OIDC/bearer token follows the same path. A missing Kerberos keytab or SSL cert/key pair is caught earlier — at Settings validation (config load) — before `greenwave_enabled` is evaluated:

```yaml
greenwave:
  enabled: true
  pushWaivers: true
  waiverUrl: https://waiverdb.example.com/api/v1.0
  waiverAuthMethod: oidc
  waiverToken: "..."
  productVersion: myproduct-1
  waivableClassifications: INFRASTRUCTURE
```

Supported ResultsDB auth methods are `none`, `token`, `kerberos`, and `ssl`; WaiverDB supports `oidc`, `kerberos`, and `ssl`. Authenticated URLs require HTTPS by default. Plain HTTP is permitted only for unauthenticated (`none`) auth in isolated local development when the effective verify value is exactly false (`verifySsl: false` with no `caBundle`); all authenticated methods (`token`, `oidc`, `kerberos`, `ssl`) always require HTTPS. Never use the HTTP escape hatch in production. Prefer `caBundle` for private CAs. `caBundle`, `sslCert`, and `sslKey` are paths inside the container: the referenced files must already be supplied by the image, a cluster-side injector, or a separately managed mount because this chart does not create those mounts.

For Kerberos, provide the base64-encoded keytab payload and principal:

```yaml
greenwave:
  resultsdbAuthMethod: kerberos
  waiverAuthMethod: kerberos
  kerberosPrincipal: svc-rootcoz@EXAMPLE.COM
  kerberosKeytab: "<base64-without-line-breaks>"
  kerberosKeytabPath: /etc/rootcoz/greenwave/krb5.keytab
```

The chart stores the payload unchanged in a Kubernetes Secret and mounts it read-only. The container image includes the native Kerberos libraries and the optional pyspnego Kerberos backend. Do not place raw keytab bytes in YAML. Greenwave token and resolved keytab checksums are part of the pod template, so changing Helm values rolls the Deployment. Values omitted during upgrade are preserved with Helm `lookup`; an out-of-band Secret edit alone still requires `kubectl rollout restart`, or a subsequent `helm upgrade` so the resolved checksum is rendered again.

The rootcoz service identity also needs ResultsDB write access and the required organization identity-management/group permissions in WaiverDB's `permissions.yml` for the configured testcase prefix. Authentication without those service-side permissions returns `403`. Supply the organization internal CA bundle when the services use a private CA. Operators are trusted to choose the subject identifier passed to ResultsDB/WaiverDB; keep WaiverDB testcase globs narrow and grant the rootcoz operator role only to users authorized to affect release gating.

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

AI and Greenwave credential keys in values apply on upgrade. Auto-generated `ADMIN_KEY` and `ROOTCOZ_ENCRYPTION_KEY` are never rotated by Helm upgrade. Vertex SA JSON, Cursor `auth.json`, Greenwave bearer tokens, and the Greenwave keytab are preserved via `lookup` when omitted from values. Deployment pod-template checksums track rendered configuration and mounted credential changes. Out-of-band Secret edits (e.g. via External Secrets) require `kubectl rollout restart` unless a subsequent Helm upgrade renders the changed resolved checksum.

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
