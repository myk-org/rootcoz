# Deploying RootCoz

> **Note:** The Helm chart bootstraps RootCoz itself. Configure Jenkins, Jira, GitHub, Report Portal, and other runtime settings in the UI after first login. See [Configuration Reference](configuration-reference.html) and [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.


> **Warning:** The Helm chart is single-replica and expects one persistent volume. Enable either `route` or `ingress`, not both.

## Prerequisites

- Docker with Docker Compose for the local recipe, or Helm 3 plus `kubectl` or `oc` for the cluster recipes
- A cluster with a `ReadWriteOnce` PersistentVolume provisioner for the SQLite data volume
- One AI provider credential: `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` (or a Vertex service account key). `CURSOR_API_KEY` alone is not enough — the bundled Compose file leaves `CURSOR_API_KEY`, `ACPX_AGENTS`, and `CLI_AGENTS` commented out, so a Cursor deployment needs those uncommented and `ACPX_AGENTS` (or `CLI_AGENTS`) set before Cursor models are discovered.
- An admin API key of at least 16 characters if you want to set `admin.key` yourself; otherwise the interactive setup script requires one and plain `helm install` auto-generates it
- `openssl`, only for the self-signed certificate in the Ingress recipe

**Running outside the container.** The image ships everything the analysis needs, including the code-graph indexer (`@trailhq/trail-code`, installed as `trail` under `sidecar-helper/node_modules/.bin/trail`) and Node.js for the Pi SDK sidecar. These are **requirements, not optional extras**: a bare host install must provide them itself, or the graph tools and AI sessions do not work at all. RootCoz does not fall back to a degraded mode when a bundled dependency is missing — that is by design, so a misconfigured deployment fails visibly instead of quietly running with fewer capabilities.

## Start locally with Docker Compose

Bring up a local RootCoz on `http://localhost:8000` with persistent data in `./data`.

```bash
cat > .env <<'EOF'
JENKINS_URL=https://jenkins.example.com
JENKINS_USER=ci-reader
JENKINS_PASSWORD=jenkins-api-token
JENKINS_SSL_VERIFY=true
AI_PROVIDER=gemini
AI_MODEL=gemini-2.5-pro
GEMINI_API_KEY=replace-with-real-gemini-key
LOG_LEVEL=INFO
DEBUG=false
EOF

docker compose up -d
curl http://localhost:8000/health
```

This uses the repo’s `docker-compose.yaml`, builds the local image, and keeps the SQLite database in `./data`. Use it for laptops, demos, and single-user environments where `localhost` access is enough.

- After changing `.env`, reload with `docker compose up -d --force-recreate rootcoz`.
- Continue with [Quickstart](quickstart.html) once the health check returns `{"status":"healthy"}`.

> **Note:** This `.env` leaves the shipped defaults for access control in place: `REQUIRE_APPROVAL` is `true` and `DEFAULT_USER_ROLE` is `reviewer`. Add `REQUIRE_APPROVAL=false` and `DEFAULT_USER_ROLE=operator` if you want to register and submit without an admin in the loop.

> **Note:** To register custom pi providers for the sidecar (pi-sidecar >=4.8.5), mount an agent dir with `models.json` / `auth.json` / `settings.json` into the container and add `PI_SIDECAR_AGENT_DIR=/path/to/dir` plus any `"$VAR"` API key variables to `.env` — the sidecar reads the dir once at start, so recreate the container after changing it. On Helm, use `sidecar.agentDir.*` values instead; see the [Configuration Reference](configuration-reference.html#sidecar-custom-providers-agent-dir).

## Bootstrap a shared cluster interactively

Use the setup script to generate safe Helm values files outside the repo and install the chart in one pass.

```bash
mkdir -p "$HOME/.config/rootcoz/helm"

uv run python scripts/helm-setup.py \
  --release rootcoz \
  --namespace rootcoz \
  --output-dir "$HOME/.config/rootcoz/helm"
```

The script prompts for cluster type, hostname, AI provider, credentials, and the bootstrap admin key, then writes `values.generated.yaml` and `values.secrets.yaml` before running `helm upgrade --install`. Use this when you want the fastest first-time shared deployment without hand-editing values files.

- Add `--skip-helm` to write files only.
- Add `--dry-run` to pass `--dry-run` through to Helm.

## Install on OpenShift with a Route

Publish RootCoz on OpenShift with a stable route and keep sensitive values outside the git checkout.

```bash
NAMESPACE=rootcoz
VALUES_DIR="$HOME/.config/rootcoz/helm"
mkdir -p "$VALUES_DIR"

cat > "$VALUES_DIR/values.generated.yaml" <<'EOF'
route:
  enabled: true
  host: rootcoz.apps.example.com
ingress:
  enabled: false
ai:
  provider: gemini
  model: gemini-2.5-pro
EOF

cat > "$VALUES_DIR/values.secrets.yaml" <<'EOF'
ai:
  geminiApiKey: "replace-with-real-gemini-key"
admin:
  key: "rootcoz-admin-2026-demo-key-please-change"
encryptionKey: "7d7f4cef5a224778a3a1a5d8af4c12b62"
EOF

helm upgrade --install rootcoz ./chart \
  --namespace "$NAMESPACE" --create-namespace \
  -f "$VALUES_DIR/values.generated.yaml" \
  -f "$VALUES_DIR/values.secrets.yaml"

oc get route -n "$NAMESPACE"
```

This uses the chart’s default OpenShift-friendly path: a Route on top of the `rootcoz` service, persistent storage, and a bootstrap admin key you control from day one. Use it when you want a shared internal deployment with the smallest amount of cluster-specific tuning.

- Omit `route.host` or set it to `""` if you want OpenShift to generate the hostname.
- After the route exists, sign in as `admin` and continue with [Quickstart](quickstart.html).

## Install on Kubernetes with TLS Ingress

Run RootCoz behind a standard Kubernetes Ingress and a TLS secret so browser sessions stay on HTTPS.

```bash
NAMESPACE=rootcoz
VALUES_DIR="$HOME/.config/rootcoz/helm"
mkdir -p "$VALUES_DIR" /tmp/rootcoz-tls

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout /tmp/rootcoz-tls/tls.key \
  -out /tmp/rootcoz-tls/tls.crt \
  -days 365 \
  -subj "/CN=rootcoz.example.com"

kubectl create secret tls rootcoz-tls \
  --cert=/tmp/rootcoz-tls/tls.crt \
  --key=/tmp/rootcoz-tls/tls.key \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

cat > "$VALUES_DIR/values.generated.yaml" <<'EOF'
route:
  enabled: false
ingress:
  enabled: true
  host: rootcoz.example.com
  className: nginx
  tls:
    enabled: true
    secretName: rootcoz-tls
ai:
  provider: gemini
  model: gemini-2.5-pro
EOF

cat > "$VALUES_DIR/values.secrets.yaml" <<'EOF'
ai:
  geminiApiKey: "replace-with-real-gemini-key"
admin:
  key: "rootcoz-admin-2026-demo-key-please-change"
encryptionKey: "7d7f4cef5a224778a3a1a5d8af4c12b62"
EOF

helm upgrade --install rootcoz ./chart \
  --namespace "$NAMESPACE" --create-namespace \
  -f "$VALUES_DIR/values.generated.yaml" \
  -f "$VALUES_DIR/values.secrets.yaml"
```

This recipe is for vanilla Kubernetes clusters where you want shared browser access and secure cookies from the start. The self-signed certificate keeps the recipe copy-pasteable; swap it for your normal cluster TLS secret or cert-manager output before exposing the service broadly.

- Replace `className: nginx` with your actual ingress class if needed.
- For production certificates, keep the same `secretName` and remove the `openssl` step.

## Run a private ClusterIP-only release and port-forward it

Use this when you want a shared in-cluster deployment without exposing RootCoz through a Route or Ingress yet.

```bash
NAMESPACE=rootcoz
VALUES_DIR="$HOME/.config/rootcoz/helm"
mkdir -p "$VALUES_DIR"

cat > "$VALUES_DIR/values.generated.yaml" <<'EOF'
route:
  enabled: false
ingress:
  enabled: false
ai:
  provider: gemini
  model: gemini-2.5-pro
EOF

cat > "$VALUES_DIR/values.secrets.yaml" <<'EOF'
ai:
  geminiApiKey: "replace-with-real-gemini-key"
admin:
  key: "rootcoz-admin-2026-demo-key-please-change"
encryptionKey: "7d7f4cef5a224778a3a1a5d8af4c12b62"
EOF

helm upgrade --install rootcoz ./chart \
  --namespace "$NAMESPACE" --create-namespace \
  -f "$VALUES_DIR/values.generated.yaml" \
  -f "$VALUES_DIR/values.secrets.yaml"

kubectl port-forward svc/rootcoz 8000:8000 -n "$NAMESPACE"
```

This keeps the service internal to the cluster and gives you temporary browser and API access on `http://localhost:8000` through `kubectl port-forward`. Use it for admin-only testing, locked-down evaluation clusters, or the period before your ingress or route is approved.

- With no Route or TLS Ingress, the chart automatically falls back to non-secure cookies for this HTTP-only access pattern.
- When you are ready to publish it, switch to the Route or Ingress recipe instead of editing the Service directly.

## Sizing

Default resources (`1 CPU / 4Gi / 12Gi ephemeral-storage` request, `2 CPU / 8Gi / 12Gi` limit) suit
evaluation clusters. For concurrent AI analysis work apply the shipped production profile, which is
reproducible from `chart/examples/values-production.yaml` and validated by the chart tests:

```bash
helm upgrade rootcoz ./chart -n rootcoz \
  -f "$VALUES_DIR/values.generated.yaml" \
  -f chart/examples/values-production.yaml \
  -f "$VALUES_DIR/values.secrets.yaml"
```

It sets `4 CPU / 32Gi / 16Gi ephemeral-storage` on both `requests` and `limits`, replacing the
chart's default `12Gi` rather than adding to it.

| Resource | Value | Basis |
|---|---|---|
| CPU | `4` | The live deployment's limit is `4000m`; CPU throttling stays under 1% of periods. |
| Memory | `32Gi` | Peak usage is ~6 GiB. Size a smaller pod with **~1 GiB baseline + ~2 GiB per concurrent analysis** (default `max_concurrent_ai_calls` is 3). |
| ephemeral-storage | `16Gi` | A judgement from the configured ceilings, not a measurement: 1 GiB graph index per analysis workspace × concurrency 3, up to 10 concurrent repo clones, plus CI artifacts and the writable layer. Keep it at or below the node's ephemeral allocatable. |

**Guaranteed versus Burstable.** `requests == limits` gives Guaranteed QoS (computed from cpu and
memory only) and raises the scheduling floor to 32Gi memory + 16Gi ephemeral, so the node must have
that allocatable. On smaller nodes request `16Gi` memory instead — Burstable, which is what the dev
deployment uses. The live `.dev/openshift/deployment.yaml` request is therefore deliberately
different from this profile.

**Upgrading to this chart version.** Every install rolls once: the default resources block now sets
`ephemeral-storage: 12Gi`, which mounts the `/tmp` `emptyDir` and raises the scheduling floor by 12 Gi,
and `terminationGracePeriodSeconds` went from `30` to `60` — any pod-template change is a rollout. The
60 is the shutdown budget: 15 s for uvicorn to close connections, 40 s for cancelled tasks to unwind,
5 s to exit. With one replica on `Recreate`, an analysis in flight when the pod goes is marked failed
for a server restart before the process exits (`GET /results/{job_id}` returns
`failure_reason: "server_restart"`); it still has to be re-submitted, because the analysis itself is
gone. Installs that set `sidecar.agentDir` had a second reason to roll earlier — the
`copy-sidecar-agent` init container's request and limit are now equal (`10m/32Mi`) where they were
`10m/16Mi` and `100m/128Mi`, and that equality is what keeps a Guaranteed pod Guaranteed.

**Clearing a request does not remove the floor.** Setting `resources.requests.memory=""` drops only
the explicit request; Kubernetes copies the matching limit into the request when no admission policy
supplies one, so the default profile still schedules for `8Gi`. Clear the limit too for a genuine
no-floor pod, or let a namespace `LimitRange` default the request.

**Persistent storage.** Database growth is ~83 KB per stored result with no retention yet (#409); the
PVC size is not part of the production profile, because applying it over a live release either no-ops
or triggers a PVC resize.

**Ephemeral storage and `/tmp`.** Repo clones and code-graph indexes live under `/tmp`, and the chart
caps it **by default**: defaults set `ephemeral-storage: 12Gi` on both `requests` and `limits`, and the
**limit** is what mounts a `/tmp` `emptyDir` with `sizeLimit` equal to that value (plus `TMPDIR=/tmp`),
so a runaway clone fails at a known number instead of filling the node. The production profile replaces
that number with `16Gi`; the derivation of both is in
[the chart README](https://github.com/myk-org/rootcoz/blob/main/chart/README.md#tmp-and-ephemeral-storage).
The cap is removed only by clearing **both** keys — clearing the limit alone keeps the `12Gi` request
and drops the volume. Exceeding either the container limit or the `sizeLimit` is a **kubelet eviction,
not a throttle**: the whole pod goes down mid-analysis, in-flight analyses are lost, and the pod returns
through the `Recreate` strategy. The SQLite database survives on the PVC and recovers through WAL
replay. Prefer a limit over no limit — an uncapped pod causes node `DiskPressure`, which evicts every
pod on the node.

**`XDG_CONFIG_HOME` inside the capped `/tmp`.** The chart sets `env.xdgConfigHome` (`/tmp/config`) and
`entrypoint.sh` creates that directory at start; if it cannot — a restricted SCC that owns the
`emptyDir` root — the container exits 1 naming the path, the uid and the gid, so the pod crash-loops
visibly instead of failing on the first config write. If that happens, set
`env.xdgConfigHome: /data/config` (the data volume) rather than adding permissions;
`tox -e chart` asserts both outcomes against the shipped entrypoint.

**Single replica consequence.** One replica on a ReadWriteOnce PVC, `Recreate` strategy, no HA: any
restart — eviction, upgrade, node drain — interrupts running analyses. Track them on the dashboard
and re-submit after the rollout.

Extended resources (`nvidia.com/gpu`, `hugepages-*`) are supported in `resources.requests` / `limits`.
`memory` and `ephemeral-storage` need a unit (`4Gi`); a bare number is read as bytes. `cpu` takes a
bare number or a quantity string (`4`, `500m`, `2.5`) — a typo such as `not-a-quantity` fails schema
validation at render, not after install at the API server. See
[the chart README](https://github.com/myk-org/rootcoz/blob/main/chart/README.md) for the full
rationale.

## Upgrade and smoke-test a Helm release

Apply new values, wait for the rollout, and run the chart’s built-in health test after any deployment change.

```bash
VALUES_DIR="$HOME/.config/rootcoz/helm"

helm upgrade rootcoz ./chart -n rootcoz \
  -f "$VALUES_DIR/values.generated.yaml" \
  -f "$VALUES_DIR/values.secrets.yaml"

kubectl rollout status deployment/rootcoz -n rootcoz
helm test rootcoz -n rootcoz
```

This is the shortest safe path for normal Helm updates once your release is already running. The `helm test` pod curls `/health`, so you get a quick verification that the app is listening after the rollout finishes.

- If you changed external secrets out of band, follow with `kubectl rollout restart deployment/rootcoz -n rootcoz`.
- For first-login and first-analysis steps after the rollout, see [Quickstart](quickstart.html).

### Roll back to an older build

A rollback to a build older than the one that wrote the data cannot read every row. If `result_json` was stored compressed, the older build skips those rows at startup instead of crashing on them, logs one line per skipped job naming its id, and logs one summary naming the count and the cause, for example:

```
Backfill skipped 13 result(s) whose result_json was written by a build with result_json compression, which this build cannot decode. Those rows are intact and untouched. Run a rootcoz build that has the result_json codec to read it, or restore a pre-compression copy of the database.
```

This skip guard is part of rootcoz itself, so it protects you only when the build you roll back to contains it. A build predating the guard still fails at startup on such a row, so roll back to a build that has it (releases from this version on).

The server keeps serving everything else. `/api/health` reports `{"status": "degraded", "undecodable_rows": <count>}` under `checks.result_json` (it stays HTTP 200 — degraded data is not a liveness failure), and the count reads `{"status": "ok", "undecodable_rows": 0}` once nothing is skipped. Affected jobs show an empty analysis, and a job that was `waiting` stays `waiting`, until you roll forward again (or restore a database copy taken before the compression build). Nothing is written over those rows, so rolling forward brings them back. While that classification restore has not run to completion (a row this build cannot decode keeps it pending), the readable jobs it repairs are restored once, and later boots leave their history rows alone as long as those rows still match the stored result; an override you make after the rollback keeps them matching too, because the override is saved into the stored result. If saving it there fails, the override response still succeeds and the server logs a warning, but the row no longer matches, so the next boot restores it from the stored result.

## Watch disk usage and alert on a filling volume

RootCoz ships no alerting stack — the rule below is yours to install. What it does ship is the metric: `/metrics` exposes three unlabelled gauges for the filesystem that holds the data directory. The probe is a `statvfs` on that directory, so it is storage-agnostic — the same gauges work for an OpenShift PVC, a Docker Compose volume, a bind mount or a plain directory, because they measure whichever filesystem backs the data dir. On a shared node filesystem the numbers can include other tenants' usage: read them as free/used space on that filesystem, not as a per-volume quota.

| Metric | Meaning |
| --- | --- |
| `rootcoz_data_used_bytes` | Bytes used on that filesystem |
| `rootcoz_data_capacity_bytes` | Capacity of that filesystem in bytes |
| `rootcoz_data_used_ratio` | Used / capacity (0-1), omitted when capacity is `0` |

The probe's cost does not grow with the number of files on the filesystem. When the probe fails the gauges are **omitted for that scrape** rather than reported as `0`, which is why an alert must use `absent()`/`>=` on the ratio and never treat a missing series as an empty disk. The gauges carry no labels, so Prometheus adds `instance` and `job` for you.

A complete `PrometheusRule` for a Prometheus that scrapes this server:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: rootcoz-disk
spec:
  groups:
    - name: rootcoz.disk
      rules:
        - alert: RootcozDataVolumeFilling
          expr: rootcoz_data_used_ratio > 0.75
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "RootCoz data directory is {{ $value | humanizePercentage }} full"
            description: "Clean up transcripts or grow the storage; the SQLite database and pi-sidecar transcripts share this filesystem."
        - alert: RootcozDataVolumeFull
          expr: rootcoz_data_used_ratio > 0.90
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "RootCoz data directory is {{ $value | humanizePercentage }} full"
            description: "Writes will start failing. Grow the storage or delete transcripts now."
```

If you also want to be told when the probe itself breaks, add one line: `absent_over_time(rootcoz_data_used_ratio[15m])` — a scrape target that has been up but has no ratio for 15 minutes has a probe that is failing every time.

### The chart health-alert CronJob (the alternative for deployments without Prometheus)

For clusters that scrape nothing, the chart can run its own check: a CronJob that curls `/metrics` on a schedule and posts one Slack message listing every failing check (unhealthy, error rate above the threshold, the data directory's filesystem above the warning or critical threshold with absolute sizes such as `5.4G / 6.0G (90%)`).

```yaml
alerts:
  enabled: true
  # Reuse the existing name you are adopting (see the naming note below)
  name: rootcoz-prod-health-alerts
  envLabel: prod
  schedule: "*/5 * * * *"
  errorRatePercent: 5
  diskWarnPercent: 75
  diskCritPercent: 90
```

The webhook is read from the release's credentials Secret under the key named by `alerts.slackSecretKey` (`SLACK_WEBHOOK_URL` by default). That key is optional: with it unset the job still schedules and prints the message into its own log instead of posting, so an alerting path that was never wired up stays quiet instead of crash-looping. **A webhook saved in Server Settings does not reach this CronJob** — the job runs outside the application and can only see the Secret, so the two must be configured separately.

- `alerts.enabled` defaults to `false`; nothing renders unless you turn it on.
- Set `alerts.name` to the name of an existing CronJob to reuse that name. A name override is **not** adoption: Helm only manages an object that carries its ownership metadata, so an existing `rootcoz-prod-health-alerts` without it makes `helm upgrade` fail with `invalid ownership metadata`.

Delete the existing CronJob and let the chart recreate it, or adopt it by adding the Helm ownership metadata first:

```bash
kubectl -n rootcoz label cronjob rootcoz-prod-health-alerts app.kubernetes.io/managed-by=Helm
kubectl -n rootcoz annotate cronjob rootcoz-prod-health-alerts \
  meta.helm.sh/release-name=rootcoz meta.helm.sh/release-namespace=rootcoz
```

- Left empty, the chart creates `<fullname>-<hash>-health-alerts`, which sits next to a cluster-only CronJob rather than replacing it. Both names are bounded (CronJob 52 characters, script ConfigMap 63) and carry an 8-character hash of the fullname, so a long release name cannot produce an object the API server rejects and two releases sharing a long prefix cannot collide.
- Pin `alerts.image` by digest for production. The default `curlimages/curl:8.12.1` is a tag, and the job runs with `automountServiceAccountToken: false` because it only needs to reach the application Service. The pod pulls the image with the release's `imagePullSecrets`, the same ones the application Deployment uses.
- Deploy the alert before or after the image that carries the data-directory gauges: when the ratio line is absent the disk check is skipped silently, and the health and error-rate checks keep working.

## Database size and compaction

Analysis results are stored compressed: every new `result_json` payload of 1 KB
or more is a zstd frame inside the same column, and a payload a frame would
grow stays plain text. SQLite reports which shape a row has (`typeof(result_json)`
is `blob` or `text`), and the application decodes both transparently — no
migration step, no setting to turn it on.

- Rows written before the upgrade are derived and compacted in the background
  after the next start, so startup and `/health` are never delayed by them. The
  sweep commits one batch at a time, is safe to interrupt, and resumes where it
  stopped on the following boot; the log reports the column bytes and the
  database file size before and after. Each pass opens its database once and
  runs every batch on that one connection, so a file swapped in at the path
  mid-pass is never read or written by it. A sweep that is given no database
  file, a replaced one, or a file without a `results` table stops before its
  first batch and logs that once; a file that goes away during the pass leaves
  the batches already committed in place and the log reports no database file
  size after, because the file it started on is no longer there.
- The database **file** only shrinks after a rewrite plus `VACUUM`, which takes
  the write lock and needs free space roughly equal to the file size. That is an
  operator step, deliberately not automated on a live single-replica deployment.
  The image has no `sqlite3` CLI, but it does ship Python:

```bash
kubectl exec -n rootcoz deploy/rootcoz -- python -c \
  "import sqlite3; sqlite3.connect('/data/results.db').execute('VACUUM')"
```

  Retry it while the application reports `database is locked`. If the volume has
  no headroom for a second copy of the file, scale the deployment to zero and run
  the same statement from a pod that mounts the volume instead.
- Rows written by a pre-compression build are still readable by that build as
  long as they are plain text. A build from before this change cannot read a
  compressed row — parsing the frame raises a decode error, so those results
  surface as errors until you upgrade again — but it never destroys one. Do not
  roll back once the background sweep has run: restore the database from a
  backup taken before the upgrade instead. Upgrade all replicas together.

## Troubleshooting

- `route.enabled and ingress.enabled are mutually exclusive`
  The chart refuses to render with both on. Set the one you are not using to `false`.

- `ingress.host is required when ingress.enabled is true`
  Kubernetes Ingress has no auto-generated hostname. Add `ingress.host` or switch to the OpenShift Route recipe.

- `ai.provider and ai.model are required for install`
  These two keys are enforced on first install only. Add them to `values.generated.yaml`.

- `ai.geminiApiKey is required when ai.provider is gemini`
  Add the matching credential key for your provider: `ai.geminiApiKey`, `ai.anthropicApiKey`, or `ai.cursor.apiKey`.

- `ADMIN_KEY not found in existing secret and admin.key not set in values`
  The credentials Secret was edited or deleted outside Helm. Restore the key by setting `admin.key` in values to match your original key, or reinstall from scratch.

- `helm template` renders empty secrets
  Expected. Auto-generated secrets use Helm `lookup`, which needs a live cluster. Run `helm install` or `helm upgrade` instead.

- `kubectl port-forward` reports the pod is not ready
  Wait for the rollout with `kubectl rollout status deployment/rootcoz -n rootcoz`, then run `helm test rootcoz -n rootcoz` to confirm `/health` responds.

- I cannot log in as `admin`.  
  Retrieve the bootstrap key with `kubectl get secret rootcoz-secret -n rootcoz -o jsonpath='{.data.ADMIN_KEY}' | base64 -d; echo` and use it as the password for username `admin`.

## Related Pages

- [Quickstart](quickstart.html)
- [Configuration Reference](configuration-reference.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)
- [API Endpoint Reference](api-reference.html)
- [Submitting Analyses](submit-analyses.html)
