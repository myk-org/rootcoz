# Copy Common Deployment Recipes

> **Note:** These recipes assume you are running from a checkout of the repository. For the full server env list and endpoint shapes, see [Configuration and Environment Reference](configuration-and-environment-reference.html) and [REST API Reference](rest-api-reference.html).

## Start a Simple Docker Compose Instance

Run the repository's shipped Compose stack with a persistent SQLite database and the combined UI/API on port `8000`.

```bash
cat > .env <<'EOF'
JENKINS_URL=https://ci.example.com
JENKINS_USER=svc-jji
JENKINS_PASSWORD=jenkins-api-token-123
AI_PROVIDER=claude
AI_MODEL=claude-opus-4-1
ANTHROPIC_API_KEY=anthropic-demo-key-123
JJI_ENCRYPTION_KEY=change-this-before-prod-32chars
PUBLIC_BASE_URL=http://localhost:8000
SECURE_COOKIES=false
LOG_LEVEL=INFO
EOF

mkdir -p data
docker compose up -d --build
docker compose ps
curl -fsS http://localhost:8000/health
```

This uses the repository's `docker-compose.yaml` as-is: it builds `Dockerfile`, mounts `./data` to `/data`, and keeps the container healthy with a `/health` probe. It is the fastest way to get a persistent JJI instance running from the repo checkout. After it is up, see [Analyze Your First Jenkins Job](analyze-your-first-jenkins-job.html) for the first-run flow.

- For public HTTPS, change `PUBLIC_BASE_URL` to your external `https://...` URL and set `SECURE_COOKIES=true`.
- If Jenkins uses a self-signed certificate, add `JENKINS_SSL_VERIFY=false` to `.env`.
- If you change `PORT`, update the Compose port mapping and healthcheck to match.

## Run Container Dev Mode with Hot Reload

Start backend reload and frontend HMR inside the container without switching away from Docker.

```bash
cat > .env <<'EOF'
JENKINS_URL=https://ci.example.com
JENKINS_USER=svc-jji
JENKINS_PASSWORD=jenkins-api-token-123
AI_PROVIDER=claude
AI_MODEL=claude-opus-4-1
ANTHROPIC_API_KEY=anthropic-demo-key-123
JJI_ENCRYPTION_KEY=dev-only-encryption-key
SECURE_COOKIES=false
LOG_LEVEL=DEBUG
EOF

cat > docker-compose.override.yaml <<'EOF'
services:
  jenkins-job-insight:
    environment:
      - DEV_MODE=true
    ports:
      - "5173:5173"
    volumes:
      - ./src:/app/src
      - ./frontend:/app/frontend
EOF

mkdir -p data
docker compose up --build
```

With `DEV_MODE=true`, `entrypoint.sh` starts Vite on `5173` and appends `uvicorn --reload --reload-dir /app/src` for backend code changes. Open `http://localhost:5173` for the UI; Vite proxies API traffic to the backend on `http://localhost:8000`.

> **Warning:** Keep `SECURE_COOKIES=false` only for local HTTP development.

- The first startup may run `npm install` inside the container if `frontend/node_modules` is missing.

## Put JJI Behind a Reverse Proxy

Terminate traffic at a proxy and keep the JJI app itself off the public port.

```bash
cat > .env.proxy <<'EOF'
JENKINS_URL=https://ci.example.com
JENKINS_USER=svc-jji
JENKINS_PASSWORD=jenkins-api-token-123
AI_PROVIDER=claude
AI_MODEL=claude-opus-4-1
ANTHROPIC_API_KEY=anthropic-demo-key-123
JJI_ENCRYPTION_KEY=change-this-before-prod-32chars
PUBLIC_BASE_URL=http://localhost:8080
SECURE_COOKIES=false
EOF

cat > nginx.conf <<'EOF'
events {}
http {
  server {
    listen 80;
    server_name _;
    location / {
      proxy_pass http://jji:8000;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
    }
  }
}
EOF

cat > compose.proxy.yaml <<'EOF'
services:
  jji:
    build: .
    env_file:
      - .env.proxy
    volumes:
      - ./data:/data
  nginx:
    image: nginx:alpine
    depends_on:
      - jji
    ports:
      - "8080:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
EOF

mkdir -p data
docker compose -f compose.proxy.yaml up -d --build
```

This makes the proxy the only entry point and keeps JJI itself on the internal Compose network. `PUBLIC_BASE_URL` matters because JJI does not derive external links from request headers, so set it to the exact URL users will open.

- For public HTTPS, terminate TLS at the proxy, change `PUBLIC_BASE_URL` to `https://...`, and set `SECURE_COOKIES=true`.
- If your auth layer injects `X-Forwarded-User`, add `TRUST_PROXY_HEADERS=true` to JJI and `proxy_set_header X-Forwarded-User $http_x_forwarded_user;` to the proxy.
- JJI ignores `X-Forwarded-User: admin`; `admin` stays reserved for real admin auth.

## Smoke-Test the Health Endpoints

Check that the service is up and inspect the detailed dependency view before you route traffic to it.

```bash
set -euo pipefail

BASE_URL=http://localhost:8000

echo "Lightweight health:"
curl -fsS "$BASE_URL/health"
echo
echo
echo "Detailed health:"
curl -fsS "$BASE_URL/api/health"
echo
```

`/health` is the lightweight endpoint used by the container healthcheck. `/api/health` adds database, Jenkins, AI provider, and Report Portal checks, and returns `503` only when JJI is actually `unhealthy`.

- `/health`, `/api/health`, and `/metrics` are excluded from JJI's rolling error counters, so normal probes do not skew `jji_error_rate`.

## Scrape Built-In Prometheus Metrics

Collect JJI's rolling-window metrics without adding an exporter sidecar.

```bash
cat > prometheus.yml <<'EOF'
global:
  scrape_interval: 30s

scrape_configs:
  - job_name: jenkins-job-insight
    metrics_path: /metrics
    static_configs:
      - targets:
          - jenkins-job-insight:8000
EOF

cat > compose.metrics.yaml <<'EOF'
services:
  prometheus:
    image: prom/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
EOF

docker compose -f docker-compose.yaml -f compose.metrics.yaml up -d --build
```

This starts Prometheus alongside the repository's Compose service and scrapes JJI directly on the Compose network. JJI publishes rolling-window gauges for `jji_requests_total`, `jji_errors_total`, `jji_error_rate`, `jji_errors_by_class`, plus `jji_health_up` and `jji_active_analyses`.

- Open `http://localhost:9090/targets` to confirm the scrape is `UP`.
- If Prometheus runs elsewhere, replace `jenkins-job-insight:8000` with your service DNS name or external host:port.
- `jji_health_up` is `1` for both `healthy` and `degraded`, and `0` only when `/api/health` is `unhealthy`.

## Send Lightweight Slack and Email Alerts

Let a single JJI instance notify you when 5xx responses spike, without waiting on external monitoring.

```bash
cat > .env <<'EOF'
JENKINS_URL=https://ci.example.com
JENKINS_USER=svc-jji
JENKINS_PASSWORD=jenkins-api-token-123
AI_PROVIDER=claude
AI_MODEL=claude-opus-4-1
ANTHROPIC_API_KEY=anthropic-demo-key-123
JJI_ENCRYPTION_KEY=change-this-before-prod-32chars
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/EXAMPLE/WEBHOOK/URL
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=jji-alerts@example.com
SMTP_PASSWORD=smtp-app-password-123
SMTP_FROM=jji-alerts@example.com
ALERT_EMAIL_TO=sre@example.com
EOF

mkdir -p data
docker compose up -d --build
```

JJI dispatches best-effort Slack and email alerts when its 5-minute rolling 5xx error rate goes above 50% and at least 10 requests have been seen. Duplicate alert events are throttled for 5 minutes, so this works well as a lightweight single-instance safety net.

- Slack webhook URLs should use `https://`.
- If you set `SMTP_HOST` without `ALERT_EMAIL_TO`, JJI starts but warns that email alerts will not be sent.

## Apply the OpenShift PrometheusRule

Use the repository's current OpenShift alert thresholds for warning, critical, and health-down conditions.

```bash
oc apply -f - <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: jenkins-job-insight-alerts
  labels:
    app: jenkins-job-insight
spec:
  groups:
    - name: jenkins-job-insight.rules
      rules:
        - alert: JJIHighErrorRate
          expr: jji_error_rate > 0.1
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "JJI high error rate"
            description: >
              JJI error rate is {{ $value | humanizePercentage }} over the
              rolling window (threshold 10%).
        - alert: JJIVeryHighErrorRate
          expr: jji_error_rate > 0.5
          for: 2m
          labels:
            severity: critical
          annotations:
            summary: "JJI critical error rate"
            description: >
              JJI error rate is {{ $value | humanizePercentage }} over the
              rolling window (threshold 50%).
        - alert: JJIHealthUnhealthy
          expr: jji_health_up == 0
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "JJI health check failing"
            description: "JJI /api/health is returning 503 (unhealthy) for over 5 minutes."
EOF
```

This matches the rule shipped in `docs/openshift-prometheus-rule.yaml`. Use it after OpenShift monitoring is scraping JJI's `/metrics` endpoint so you get two error-rate thresholds and one hard health-down alert.

- Add any labels your Alertmanager routing expects before applying the rule.
- The container image is already prepared for OpenShift-style arbitrary UID runtimes; the main deployment requirement is a writable `/data` volume.

## Related Pages

- [Analyze Your First Jenkins Job](analyze-your-first-jenkins-job.html)
- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Manage Users, Access, and Token Usage](manage-users-access-and-token-usage.html)
- [REST API Reference](rest-api-reference.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)