# Deploying RootCoz

> **Note:** The Helm chart bootstraps RootCoz itself. Configure Jenkins, Jira, GitHub, Report Portal, and other runtime settings in the UI after first login. See [Configuration Reference](configuration-reference.html) and [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.


> **Warning:** The Helm chart is single-replica and expects one persistent volume. Enable either `route` or `ingress`, not both.

## Start locally with Docker Compose

Bring up a local RootCoz on `http://localhost:800` with persistent data in `./data`.

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
curl http://localhost:800/health
```

This uses the repo’s `docker-compose.yaml`, builds the local image, and keeps the SQLite database in `./data`. Use it for laptops, demos, and single-user environments where `localhost` access is enough.

- After changing `.env`, reload with `docker compose up -d --force-recreate rootcoz`.
- Continue with [Quickstart](quickstart.html) once the health check returns `{"status":"ok"}`.

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

kubectl port-forward svc/rootcoz 800:800 -n "$NAMESPACE"
```

This keeps the service internal to the cluster and gives you temporary browser and API access on `http://localhost:800` through `kubectl port-forward`. Use it for admin-only testing, locked-down evaluation clusters, or the period before your ingress or route is approved.

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
- For first-login and first-analysis steps after the rollout, see [Quickstart](quickstart.html).# Deploying RootCoz

> **Note:** The Helm chart bootstraps RootCoz itself. Configure Jenkins, Jira, GitHub, Report Portal, and other runtime settings in the UI after first login. See [Configuration Reference](configuration-reference.html) and [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.


> **Warning:** The Helm chart is single-replica and expects one persistent volume. Enable either `route` or `ingress`, not both.

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
- Continue with [Quickstart](quickstart.html) once the health check returns `{"status":"ok"}`.

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

## Related Pages

- [Quickstart](quickstart.html)
- [Configuration Reference](configuration-reference.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)
- [API Endpoint Reference](api-reference.html)
- [Submitting Analyses](submit-analyses.html)