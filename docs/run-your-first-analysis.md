# Run Your First Analysis

You want one real Jenkins build analyzed end to end so you can stop reading raw CI output and start from a report. The fastest path is to start the bundled Docker setup, save a username, submit a build, and let RootCoz open the report for you.

## Prerequisites
- Docker with Docker Compose
- A Jenkins URL, username, password or API token, and one build number you want to inspect
- One AI provider credential
- A web browser

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
curl http://localhost:8000/health
```

1. Open `http://localhost:8000`.
2. Enter a username and click `Save`.
3. Click `New Analysis`.
4. Enter `Job Name` and `Build Number`.
5. Click `Submit Analysis`.

RootCoz opens a live status page first, then switches to the report when the analysis completes.

> **Note:** The Docker image already includes the supported AI CLIs. For a first run, you only need one working provider credential plus `AI_PROVIDER` and `AI_MODEL`.

## Step-by-Step

1. Create your `.env` file.

   Start with the quick example above, then swap the provider credential if you are not using Claude.

   | AI provider | Also set in `.env` |
   | --- | --- |
   | Claude | `ANTHROPIC_API_KEY=...` |
   | Gemini | `GEMINI_API_KEY=...` |
   | Cursor | `CURSOR_API_KEY=...` |

   > **Warning:** Replace the example Jenkins values before you start the stack. Docker Compose will start even if those values still point at placeholders.

2. Start RootCoz.

   Run:

   ```bash
   docker compose up -d
   ```

   Then confirm the server is responding:

   ```bash
   curl http://localhost:8000/health
   ```

   The web app and API are both served from `http://localhost:8000`.

3. Create your profile.

   On a first visit, RootCoz sends you to a profile screen. A username is enough for regular use.

   The `API Key`, `GitHub Token`, `Jira Email`, and `Jira Token` fields are optional for this first report.

   > **Tip:** Leave the token fields blank on day one. Add them later when you are ready to create issues directly from a report.

4. Submit your first Jenkins build.

   Click `New Analysis`, keep `Jenkins Job` selected, then fill in:
   - `Job Name`
   - `Build Number`

   If you already set `JENKINS_URL`, `JENKINS_USER`, and `JENKINS_PASSWORD` in `.env`, you can leave the matching form fields blank. If you want different values for just this run, enter them on the form to override the server defaults.

   Review `AI Provider` and `AI Model` before you submit. Leave `Wait for build completion` on unless you do not want RootCoz to wait for an active Jenkins build.

5. Open the report.

   After you click `Submit Analysis`, RootCoz shows a status page while the job is `waiting`, `pending`, or `running`. When analysis finishes, it redirects to the stored report automatically.

   If you come back later, the dashboard keeps the run so you can reopen it without submitting again.

## Advanced Usage

- You can override the server defaults per run from `New Analysis`, including Jenkins connection fields, `AI Provider`, `AI Model`, `AI CLI Timeout`, Jira search settings, and source repository settings.
- `New Analysis` also supports `Paste XML` and `Upload File` when you want to analyze JUnit XML directly instead of pulling from Jenkins.
- If Jenkins uses a self-signed certificate, set `JENKINS_SSL_VERIFY=false` before starting RootCoz.
- Personal GitHub and Jira tokens in your profile are mainly for actions you take later from the report, such as issue creation. See [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html).
- If you want to connect external systems at the server level after your first run, see [Connecting GitHub, Jira, and Report Portal](connecting-github-jira-and-report-portal.html).
- If you prefer to script this flow once the server is running, see [CLI Command Reference](cli-command-reference.html).

## Troubleshooting

- If `docker compose up -d` says `AI_PROVIDER is required` or `AI_MODEL is required`, add both values to `.env` and start again.
- If `Submit Analysis` fails immediately, re-check your Jenkins URL, username, and token. You can also enter those values directly on `New Analysis` for a one-off override.
- If the status page stays in `waiting`, Jenkins is still running and RootCoz is monitoring it. Leave it running, or disable `Wait for build completion` on a later submission if you do not want that behavior.
- If the profile page says `The username 'admin' is reserved`, choose a different username unless you were given an admin API key.
- If the browser shows connection errors, verify `curl http://localhost:8000/health` works, then inspect the server logs with `docker compose logs rootcoz`.
- If you get a `403` allow-list error while submitting, ask an administrator to add your username or grant you admin access.

## Related Pages

- [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html)
- [Tracking Analysis Progress](tracking-analysis-progress.html)
- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)
- [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html)
- [Connecting GitHub, Jira, and Report Portal](connecting-github-jira-and-report-portal.html)