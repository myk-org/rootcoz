# Analyze Your First Jenkins Job

You want JJI running, pointed at one real Jenkins failure, and showing a report you can use instead of digging through raw Jenkins output. The fastest first run uses Docker Compose for the server, then `jji` from the checkout to queue one build and open the stored result.

## Prerequisites
- Docker with Docker Compose, or Python 3.12 plus Node.js, npm, and `uv` if you want the local-install path
- A Jenkins URL, username, password or API token, and a build number you want to inspect
- One AI provider credential. The smallest first setup is `claude` with `ANTHROPIC_API_KEY`

## Quick Example

```bash
cp .env.example .env
```

```dotenv
JENKINS_URL=https://jenkins.example.com
JENKINS_USER=your-username
JENKINS_PASSWORD=your-api-token
AI_PROVIDER=claude
AI_MODEL=your-model-name
ANTHROPIC_API_KEY=your-anthropic-api-key
```

```bash
docker compose up -d
uv run jji --server http://localhost:8000 --user jdoe health
uv run jji --server http://localhost:8000 --user jdoe analyze --job-name my-job --build-number 42
```

```text
Job queued: <job_id>
Status: queued
Poll: /results/<job_id>
```

Open `http://localhost:8000`, save the same username (`jdoe`), then paste the printed `Poll:` path after that host. If the build is still finishing or the analysis is still running, JJI shows the live status page first and switches to the report when it completes.

## Step-by-Step

1. Create the server environment file.

```bash
cp .env.example .env
```

```dotenv
JENKINS_URL=https://jenkins.example.com
JENKINS_USER=your-username
JENKINS_PASSWORD=your-api-token
AI_PROVIDER=claude
AI_MODEL=your-model-name
ANTHROPIC_API_KEY=your-anthropic-api-key
```

Replace the placeholders with real values. For the first run, keep everything else at the defaults from `.env.example`.

> **Tip:** Use a failed build that has already finished. You will get to the report faster than waiting for Jenkins to complete a running job.

2. Start JJI and confirm the server is reachable.

```bash
docker compose up -d
uv run jji --server http://localhost:8000 --user jdoe health
```

When the container is ready, the health command should report `healthy`. The same server at `http://localhost:8000` serves the web UI and the API.

3. Save a browser username.

Open `http://localhost:8000/`. On the first visit, JJI sends you to `/register`.

Enter the same username you used with `--user` and click **Save**. Leave `API Key`, `GitHub Token`, `Jira Email`, and `Jira Token` empty for this first run.

> **Note:** A username is enough to analyze a job and open the report. Admin access and personal tracker tokens are only needed for later features.

4. Queue the Jenkins build.

```bash
uv run jji --server http://localhost:8000 --user jdoe analyze --job-name my-job --build-number 42
```

Replace `my-job` and `42` with a real Jenkins job and build number. If the job lives inside Jenkins folders, keep the full Jenkins path style, such as `folder/job-name`.

The command prints a new `job_id` plus a `Poll:` path. JJI stores the run immediately, then moves it through `waiting`, `pending`, or `running` until the report is ready.

5. Open the first report.

```bash
uv run jji --server http://localhost:8000 --user jdoe status <job_id>
uv run jji --server http://localhost:8000 --user jdoe results show <job_id>
```

Use `status` while the run is in progress. Then either open `http://localhost:8000` plus the `Poll:` path, or click the new row on the dashboard.

The dashboard opens the live status page for in-progress runs and the final report for completed runs. The status page refreshes every 10 seconds and automatically switches to the report page, where you will see the Jenkins link, failure count, AI provider/model, a key takeaway, and the grouped failures from the build.

## Advanced Usage

### Run from a local install instead of Docker

```bash
uv sync

cd frontend
npm ci --no-audit --no-fund
npx vite build
cd ..

export AI_PROVIDER=claude
export AI_MODEL=your-model-name
export ANTHROPIC_API_KEY=your-anthropic-api-key

uv run jenkins-job-insight
```

Reuse the same `.env` file from the Docker steps for `JENKINS_*` and other server settings. For a local process, export `AI_PROVIDER`, `AI_MODEL`, and the matching provider credential before you start the server.

> **Warning:** If a local run says no AI provider or model is configured even though those values are in `.env`, export them in your shell before starting `uv run jenkins-job-insight`.

### Choose a different AI provider

| Provider | Minimum settings |
| --- | --- |
| `claude` | `AI_PROVIDER=claude`, `AI_MODEL=opus-4`, `ANTHROPIC_API_KEY=...` |
| `gemini` | `AI_PROVIDER=gemini`, `AI_MODEL=gemini-2.5-pro`, `GEMINI_API_KEY=...` |
| `cursor` | `AI_PROVIDER=cursor`, `AI_MODEL=gpt-5.4-xhigh`, `CURSOR_API_KEY=...` |

The Docker image already includes the supported provider CLIs. You only need to supply the matching credentials.

> **Tip:** `gemini` can also use `gemini auth login` instead of `GEMINI_API_KEY`.

### Save CLI defaults so you can skip repeated flags

```toml
[default]
server = "local"

[servers.local]
url = "http://localhost:8000"
username = "jdoe"
```

Save that as `~/.config/jji/config.toml`. After that, `uv run jji health` and `uv run jji analyze --job-name my-job --build-number 42` can use the saved server and username automatically.

### Make the `Poll:` line use a full URL

```dotenv
PUBLIC_BASE_URL=https://jji.example.com
```

Without `PUBLIC_BASE_URL`, JJI returns relative links such as `/results/<job_id>`. Set it when you want the CLI output and generated report links to point at the public hostname directly.

- See [Configuration and Environment Reference](configuration-and-environment-reference.html) for every server setting.
- See [CLI Command Reference](cli-command-reference.html) for all `jji` commands.
- See [Copy Common Analysis Recipes](copy-common-analysis-recipes.html) for ready-made commands such as `--no-wait`, self-signed Jenkins, forced analysis, and extra repository context.
- See [Copy Common Deployment Recipes](copy-common-deployment-recipes.html) for container and reverse-proxy patterns.

## Troubleshooting

- `Error: No server specified`: pass `--server http://localhost:8000`, or save a CLI profile in `~/.config/jji/config.toml`.
- The browser keeps sending you to `/register`: save a username first. A normal first run does not need an API key.
- The local install shows `Frontend not built`: rebuild the frontend with `npm ci --no-audit --no-fund` and `npx vite build` in `frontend/`, then restart the server.
- Jenkins TLS errors block the first run: set `JENKINS_SSL_VERIFY=false` in `.env`, or use the self-signed Jenkins recipe in [Copy Common Analysis Recipes](copy-common-analysis-recipes.html).
- The run stays in `waiting`: JJI is still polling Jenkins for build completion. Use a finished build for the fastest first run, or see [Copy Common Analysis Recipes](copy-common-analysis-recipes.html) for a no-wait example.
- The `Poll:` line is only `/results/<job_id>`: that is expected until `PUBLIC_BASE_URL` is set.

## Related Pages

- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Review and Classify Failures](review-and-classify-failures.html)
- [Copy Common Analysis Recipes](copy-common-analysis-recipes.html)
- [Copy Common Deployment Recipes](copy-common-deployment-recipes.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)