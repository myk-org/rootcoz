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

### Build git SHA

The Process card on Admin → Server Status shows the commit the image was built from, and only the image builder can supply it: pass `--build-arg GIT_SHA=$(git rev-parse HEAD)` (`docker-compose.yaml` forwards a `GIT_SHA` environment variable to the build); this chart does not build images, so an image built without the arg shows `not visible in this container` — honest, not a bug.

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

Default resources (`1 CPU / 4Gi` request, `2 CPU / 8Gi` limit) suit evaluation clusters. For
concurrent AI analysis workloads apply the shipped production profile — an overlay, never a
default:

```bash
helm upgrade --install rootcoz ./chart -n rootcoz \
  -f chart/examples/values-openshift.yaml \
  -f chart/examples/values-production.yaml \
  -f ~/.config/rootcoz/helm/values.secrets.yaml
```

`chart/examples/values-production.yaml` sets `4 CPU / 32Gi / 16Gi ephemeral-storage` on **both**
`requests` and `limits`. Where the numbers come from:

| Resource | Value | Basis |
|---|---|---|
| CPU | `4` | The live deployment's limit is `4000m` and CPU throttling stays under 1% of periods. |
| Memory | `32Gi` | Peak usage (analysis + graph indexing + sidecar) is ~6 GiB. 32Gi is headroom to raise `max_concurrent_ai_calls` above the default 3; the rule for sizing a smaller pod is **~1 GiB baseline + ~2 GiB per concurrent analysis**. |
| ephemeral-storage | `16Gi` | **A judgement, not a measurement**: 1 GiB graph-index budget per analysis workspace × default concurrency 3, plus up to 10 concurrent repo clones of arbitrary size, plus Jenkins/Prow artifacts and the container writable layer. Must stay at or below the node's ephemeral allocatable. |

The PVC size is deliberately *not* in the profile: the example is applied over a live release, where
changing `persistence.size` either no-ops or triggers a PVC resize. Database growth is tracked in #409
(~83 KB per stored result).

**Why `requests == limits`:** the deployment is a single stateful replica, so predictable scheduling
beats burst. Guaranteed QoS is computed from cpu and memory only (ephemeral-storage does not
affect it) **and across init containers too** — the `copy-sidecar-agent` init container matches its own
requests and limits for exactly that reason, so enabling `sidecar.agentDir` cannot quietly downgrade a
Guaranteed pod to Burstable. Those init-container numbers are new (`10m/32Mi` on both sides, previously
`10m/16Mi` request against `100m/128Mi` limit), which is why that pod template rolls once on upgrade —
see [Rollout on upgrade](#rollout-on-upgrade). The third value is matched for a different reason —
the request drives scheduling and the limit drives kubelet eviction, so one number describes both
thresholds. The cost is a scheduling floor of 32Gi memory + 16Gi ephemeral: the node must have that
allocatable. The repo's dev deployment requests `16Gi` instead (`1000m` CPU) — Burstable, the fallback
for smaller nodes.

### `/tmp` and ephemeral storage

Clones and code-graph indexes live under `/tmp`. Setting `resources.limits["ephemeral-storage"]`
makes the chart mount a dedicated `/tmp` `emptyDir` with `sizeLimit` equal to that limit, verbatim,
plus `TMPDIR=/tmp`. There is no second value to keep in sync.

**Ownership vs. #402 C1.** This chart owns the `/tmp` cap: the number is
`resources.limits["ephemeral-storage"]` and there is no separate `workspace.sizeLimit` key. #402 C1
applies the same `/tmp` `emptyDir` + `sizeLimit` to the live deployment, so that deployment must carry
the same number as the profile it runs (the production example uses `16Gi`). Two values for one
threshold is exactly the drift the single-source rule above prevents.

Behaviour and failure mode: exceeding the container's ephemeral-storage limit **or** the `emptyDir`
`sizeLimit` is a **kubelet eviction, not a throttle** — the whole pod goes down mid-analysis. In flight,
analyses in that process are lost (results are written on completion), pi-sidecar transcripts end
mid-file, and the pod returns through the `Recreate` strategy; there is no second replica to absorb
the work. The SQLite database survives: it lives on the PVC at `/data`, and SQLite WAL recovery
handles the interrupted write.

A limit is still the right choice over no limit: with no cap the failure is node `DiskPressure`,
which evicts *every* pod on the node, including unrelated workloads. The `/tmp` cap is a bound on
`/tmp` specifically — a runaway clone or extract dir fails at a known number instead of filling the
node. Which of the two thresholds reports first is not deterministic and does not need to be: the
response to both is to raise the number. An `ephemeral-storage` **request** without a limit still
constrains scheduling but mounts nothing.

Media unchanged by this: `/data` (PVC) and `PI_SIDECAR_TRANSCRIPTS_DIR` stay on the data volume.
`XDG_CONFIG_HOME` already pointed at `/tmp/config`, so its lifetime is unchanged.

**Opt-in only.** The default profile is untouched: defaults set no `ephemeral-storage` and the
evaluation recipes render byte-identically. Setting the ephemeral limit mounts `/tmp` and rolls the
pod once.

### Rollout on upgrade

The byte-identical default render holds for installs that **do not** set `sidecar.agentDir`. Installs
that do get **one pod restart** from this chart version alone: the `copy-sidecar-agent` init
container's request and limit changed (`10m/16Mi` request + `100m/128Mi` limit → `10m/32Mi` on both
sides), and any pod-template change is a rollout. The equality is the point — without it the pod is
Burstable whenever the sidecar agent dir is set, so the restart is what buys Guaranteed QoS. It is
still a restart: one replica, `Recreate`, 30 s grace period, so an analysis running at that moment is
interrupted and has to be re-submitted (results are written on completion; SQLite recovers via WAL).

### Extended resources

`resources.requests` / `resources.limits` accept qualified extended resources (`nvidia.com/gpu`,
`amd.com/gpu`) and `hugepages-*` in addition to `cpu`, `memory` and `ephemeral-storage`. Unknown
*unqualified* keys are rejected by the schema, so `resources.limit.cpu=4` (a typo) fails at render
instead of producing a pod the API server refuses.

**Units:** the schema accepts the Kubernetes [Quantity
grammar](https://kubernetes.io/docs/reference/kubernetes-api/definitions/quantity-resource/) for
`cpu`, `memory` and `ephemeral-storage`: `4Gi`, `512Mi`, `.5Gi`, `500m`, `2.5`, `1e3`, plus a bare
number. A unit-less value — a bare number (`memory: 4`) or a numeric string (`"4"`) — is **bytes**
for `memory` and `ephemeral-storage`, so `memory: 4` is 4 bytes and an OOM-kill on start: always give
memory and storage a unit. Anything the grammar does not accept, such as
`--set resources.limits.cpu=not-a-quantity` or `4GiB`, is schema-rejected at render instead of
reaching the API server. Setting a key to `""` omits **that key** from the render — an empty quantity
is invalid, so the chart drops it instead of emitting one.

**A cleared request is not "no floor".** Dropping the request key does not stop the **limit** from
becoming the effective request: Kubernetes defaults a missing request to the matching limit, so
`--set resources.requests.memory=""` on the default profile (`limits.memory: 8Gi`) still schedules for
`8Gi`. Only a namespace `LimitRange` defaulting the request supplies a different number, and the chart
renders nothing that would. A genuine no-floor pod needs **both** sides cleared
(`resources.requests.memory=""` and `resources.limits.memory=""`); clearing the request alone just
removes the explicit value.

Full sizing guidance for operators: [Deploying rootcoz → Sizing](../docs/deploy-rootcoz.md#sizing).

**Out of scope, recorded so nobody "fixes" one side blindly:** `chart/README.md` (this section) and
`.dev/openshift/deployment.yaml` disagree on the *request* — Guaranteed `32Gi` here versus a Burstable
`16Gi` request on the live deployment. The live manifest is not chart-managed and changing a live
deployment's requests is a rollout decision.

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

### Health alert CronJob

An optional CronJob scrapes `/metrics` on a schedule and posts one Slack message listing every failing check: unhealthy (`rootcoz_health_up`), error rate above the threshold, and `/data` usage above the warning or critical threshold. The script lives in `chart/files/health-alert.sh` and is mounted from a ConfigMap, so the tested file is the deployed file.

```yaml
alerts:
  enabled: true
  name: rootcoz-prod-health-alerts  # reuse the existing name you adopt (see Naming)
  envLabel: prod
  schedule: "*/5 * * * *"
  errorRatePercent: 5
  diskWarnPercent: 75
  diskCritPercent: 90
```

| Value | Default | Meaning |
| --- | --- | --- |
| `alerts.enabled` | `false` | Render the CronJob and its script ConfigMap |
| `alerts.name` | `""` | CronJob name, truncated to 52 chars; empty means `<fullname>-<hash>-health-alerts` |
| `alerts.schedule` | `*/5 * * * *` | Cron expression |
| `alerts.envLabel` | `""` | Message prefix; empty means `<fullname>` |
| `alerts.metricsUrl` | `""` | Metrics URL; empty means `http://<fullname>:8000/metrics` |
| `alerts.image` | `curlimages/curl:8.12.1` | Must provide `sh`, `awk` and `curl`; pin by digest for production |
| `alerts.slackSecretName` | `""` | Secret holding the webhook; empty means the release credentials Secret |
| `alerts.slackSecretKey` | `SLACK_WEBHOOK_URL` | Key inside that Secret |
| `alerts.errorRatePercent` | `5` | Alert above this rolling-window error rate |
| `alerts.diskWarnPercent` | `75` | `/data` warning threshold |
| `alerts.diskCritPercent` | `90` | `/data` critical threshold |

Things to know before turning it on:

- **The webhook comes from the Secret, not from Server Settings.** `SLACK_WEBHOOK_URL` saved in the UI drives only the in-process alerts; the CronJob cannot read the database. Without the Secret key the job still schedules and prints the message into the job log — it never crash-loops, and it never posts.
- **Naming.** The live `rootcoz-prod-health-alerts` / `rootcoz-dev-health-alerts` CronJobs predate this chart. Leaving `alerts.name` empty creates `<fullname>-<hash>-health-alerts` as a second CronJob. Setting `alerts.name` to an existing name is **not** adoption: Helm only takes over an object that carries its ownership metadata, so `helm upgrade` fails with `invalid ownership metadata` if the CronJob exists without it. Adopt it by adding that metadata first (or delete the CronJob and let the chart recreate it):

  ```bash
  kubectl -n rootcoz label cronjob rootcoz-prod-health-alerts app.kubernetes.io/managed-by=Helm
  kubectl -n rootcoz annotate cronjob rootcoz-prod-health-alerts \
    meta.helm.sh/release-name=rootcoz meta.helm.sh/release-namespace=rootcoz
  ```

  Both names are bounded — CronJob 52 characters (the Job controller appends an 11-character suffix), script ConfigMap 63 — and carry an 8-character hash of the fullname, so a long release name cannot produce an object the API server rejects and two releases sharing a long prefix cannot collide.
- **The image is pulled with `imagePullSecrets`.** The CronJob pod uses the same `imagePullSecrets` as the application Deployment, so a mirrored alert image in a private registry pulls with the credentials already configured for the app.
- **A wrong URL fails loudly.** A non-empty response that is not rootcoz metrics (a login page, a proxy error page) is reported as `metrics endpoint returned no rootcoz metrics` instead of silencing every check.
- **The disk check is the newest part.** When `/metrics` has no `rootcoz_data_used_ratio` (an image older than the gauges, or a failing filesystem probe) the disk check is skipped silently and the health and error-rate checks still run. The gauges measure the filesystem holding the data directory, so they work unchanged for a PVC, a Docker volume, a bind mount or a plain directory.
- The job needs no cluster permissions: the pod sets `automountServiceAccountToken: false` and only curls the application Service.

## Validation

```bash
helm lint chart/
uvx --with tox-uv tox -e chart
helm test rootcoz -n rootcoz   # requires running release
```

The `chart` tox env lints the chart, renders every recipe and fixture, renders
`chart/examples/values-production.yaml` and asserts its resources plus the quoted `/tmp` `sizeLimit`
(and that the `/tmp` mount never reaches the init container). The default render — `chart/` with only
`ai.provider`, `ai.model` and the required secrets, no overlay — has a **baseline assertion**: its
container `resources` block must equal the chart defaults (`requests` `1`/`4Gi`, `limits` `2`/`8Gi`),
and it must carry no `ephemeral-storage`, no `sizeLimit`, no `/tmp` mount and no `TMPDIR`, so a
values or template change that alters the default pod template fails CI instead of silently rolling
every install. The env also asserts the schema negatives (`resources.limit.cpu`,
`resources.requests.cpuu`, a bad `memory` unit `4GiB`), the quantity positives (numeric `cpu`/`memory`
and `ephemeral-storage`, `.5Gi`, `1e3`, `4m`, an explicit string quantity, extended resources —
`nvidia.com/gpu`, `hugepages-2Mi`, a mixed-case name `example.com/GPU_2`, and the rejection of a
malformed one), and that a bad cpu quantity (`not-a-quantity` on both `requests` and `limits`) is
rejected. Two negatives cover wrong **types** rather than wrong keys or strings: a list where a
quantity goes (`resources.limits.memory=[1]`), and a non-string value in `podAnnotations` /
`podLabels` / `nodeSelector`, which are string→string maps. Further checks cover the pod-integrity
rules: `--set resources.requests.memory=""` renders no `memory`
request (empty means omit), and with the sidecar agent dir configured the init container's `requests`
equal its `limits`, so a Guaranteed app container keeps Guaranteed QoS for the whole pod.

## Limitations

- `helm template` offline renders empty auto-generated secrets (`lookup` requires a live cluster)
- Provider-conditional required fields are validated at runtime, not by JSON Schema alone
