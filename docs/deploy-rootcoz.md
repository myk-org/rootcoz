# Deploying RootCoz

> **Note:** The Helm chart bootstraps RootCoz itself. Configure Jenkins, Jira, GitHub, Report Portal, and other runtime settings in the UI after first login. See [Configuration Reference](configuration-reference.html) and [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.


> **Warning:** The Helm chart is single-replica and expects one persistent volume. Enable either `route` or `ingress`, not both.

## Prerequisites

- Docker with Docker Compose for the local recipe, or Helm 3 plus `kubectl` or `oc` for the cluster recipes
- A cluster with a `ReadWriteOnce` PersistentVolume provisioner for the SQLite data volume
- One AI provider credential: `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` (or a Vertex service account key). `CURSOR_API_KEY` alone is not enough — the bundled Compose file leaves `CURSOR_API_KEY`, `ACPX_AGENTS`, and `CLI_AGENTS` commented out, so a Cursor deployment needs those uncommented and `ACPX_AGENTS` (or `CLI_AGENTS`) set before Cursor models are discovered.
- An admin API key of at least 16 characters if you want to set `admin.key` yourself; otherwise the interactive setup script requires one and plain `helm install` auto-generates it
- `openssl`, only for the self-signed certificate in the Ingress recipe

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
