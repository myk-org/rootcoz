# Analyzing Test Failures

Submit your test failures to rootcoz for AI-powered root cause analysis. Whether your tests run in Jenkins, produce JUnit XML reports, or you have a list of failures on hand, rootcoz classifies each failure as a **Code Issue**, **Product Bug**, or **Infrastructure** problem and explains why.

## Prerequisites

- A running rootcoz server (see [Quickstart](quickstart.html))
- An account with **operator** or **admin** role (see [Managing Users and Roles](managing-users.html))
- For CLI usage: the `rootcoz` CLI configured with a server connection (see [Setting Up the CLI](cli-setup.html))

## Quick Example

**CLI — analyze a Jenkins job:**

```bash
rootcoz analyze --source jenkins --job-name my-project/regression-tests --build-number 142
```

**CLI — analyze a JUnit XML file:**

```bash
rootcoz analyze --source file --file test-results.xml
```

Both commands return a job ID you can use to check progress:

```
Job queued: a1b2c3d4-5678-90ab-cdef-1234567890ab
Status: pending
Poll: https://rootcoz.example.com/results/a1b2c3d4-5678-90ab-cdef-1234567890ab
```

## Submitting from the Web UI

1. Click **New Analysis** in the navigation bar.
2. Choose your input mode: **Jenkins Job**, **Paste XML**, or **Upload File**.
3. Fill in the required fields (job name + build number for Jenkins, or XML content for file mode).
4. Click **Submit Analysis**.

The UI redirects to a live status page where you can watch the analysis progress. When it completes, you're taken to the results page automatically.

### Jenkins Job Mode

Enter the Jenkins job name (including any folder path, e.g. `team/nightly-regression`) and the build number. Optionally provide Jenkins connection details if different from the server defaults.

The **Wait for build completion** toggle (on by default) tells rootcoz to poll Jenkins until the build finishes before starting analysis. Configure the poll interval and maximum wait time as needed.

### Paste XML / Upload File Mode

Paste JUnit XML content directly or drag-and-drop an `.xml` file. rootcoz parses the XML, extracts failed test cases, and runs AI analysis on each failure.

> **Tip:** This mode works with any CI system that produces JUnit-compatible XML — GitHub Actions, GitLab CI, CircleCI, and others.

## Submitting from the CLI

### Jenkins Analysis

```bash
rootcoz analyze \
  --source jenkins \
  --job-name team/nightly-regression \
  --build-number 142
```

The `--source` flag defaults to `jenkins`, so you can omit it:

```bash
rootcoz analyze --job-name team/nightly-regression --build-number 142
```

### JUnit XML File Analysis

```bash
rootcoz analyze --source file --file path/to/junit-results.xml
```

### Common Options

| Flag | Description |
|------|-------------|
| `--name "My Analysis"` | Custom display name on the dashboard |
| `--provider claude` | AI provider: `claude`, `gemini`, or `cursor` |
| `--model <model-id>` | Specific AI model to use |
| `--tag regression` | Tag for categorization (repeatable) |
| `--jira / --no-jira` | Enable or disable Jira bug search |
| `--json` | Output as JSON instead of human-readable text |

### Checking Status

After submitting, check the analysis status:

```bash
rootcoz status <job-id>
```

View the full results:

```bash
rootcoz results show <job-id>
```

Or get the complete JSON output:

```bash
rootcoz results show <job-id> --full --json
```

List all recent analyses on the dashboard:

```bash
rootcoz results dashboard
```

### Aborting an Analysis

Cancel a running or waiting analysis:

```bash
rootcoz abort <job-id>
```

## What Happens During Analysis

When you submit a job, rootcoz runs these steps in the background:

1. **Fetch data** — pulls test results (and optionally console logs and build artifacts) from Jenkins, or parses the provided XML
2. **Group failures** — deduplicates failures by error signature so identical errors are analyzed only once
3. **AI analysis** — sends each unique failure group to the AI with full context (error messages, stack traces, console output, artifacts, and source code from configured repositories)
4. **Classification** — the AI classifies each failure and assigns a pattern:
   - **Classification** (root cause): `CODE ISSUE`, `PRODUCT BUG`, or `INFRASTRUCTURE`
   - **Pattern** (behavior): `NEW`, `REGRESSION`, `FLAKY`, `INTERMITTENT`, `KNOWN_BUG`, or `PERSISTENT`
5. **Post-processing** — if Jira is enabled, searches for existing bugs matching any `PRODUCT BUG` findings; if a tests repo is configured, searches for related issues on `CODE ISSUE` findings
6. **Pipeline expansion** — for Jenkins pipeline jobs, recursively analyzes failed child jobs

> **Note:** Analysis runs asynchronously. The submission endpoint returns immediately with a job ID. Use the status page or `rootcoz status <job-id>` to monitor progress.

## Re-Analyzing Jobs

You can re-run analysis on a previously analyzed job — useful when you want to try a different AI provider, model, or prompt.

**Web UI:** Open the results page for any completed job and click **Re-Analyze**.

**CLI:**

```bash
rootcoz re-analyze <job-id>
```

Re-analysis creates a new job with a fresh job ID while preserving a link back to the original.

### Re-Analyzing a Single Failure

To re-analyze just one failure instead of the entire job:

```bash
rootcoz failure re-analyze <failure-uuid>
```

Find failure UUIDs with:

```bash
rootcoz results show <job-id> --json
```

## Advanced Usage

### Per-Request Jenkins Overrides

Override server-level Jenkins settings for a single analysis:

```bash
rootcoz analyze \
  --job-name my-job \
  --build-number 42 \
  --jenkins-url https://other-jenkins.example.com \
  --jenkins-user myuser \
  --jenkins-password mytoken
```

### Build Artifacts

By default, rootcoz downloads build artifacts from Jenkins to give the AI richer context. Control this behavior:

```bash
# Disable artifact download
rootcoz analyze --job-name my-job --build-number 42 --no-get-job-artifacts

# Limit artifact download size
rootcoz analyze --job-name my-job --build-number 42 --jenkins-artifacts-max-size-mb 100
```

### Waiting for In-Progress Builds

Submit analysis for a build that hasn't finished yet:

```bash
rootcoz analyze \
  --job-name my-job \
  --build-number 42 \
  --wait \
  --poll-interval 5 \
  --max-wait 120
```

This polls Jenkins every 5 minutes, waiting up to 120 minutes for the build to complete before analyzing.

To skip waiting and analyze only if the build is already done:

```bash
rootcoz analyze --job-name my-job --build-number 42 --no-wait
```

### Forcing Analysis on Successful Builds

By default, rootcoz skips analysis when a build reports SUCCESS. To analyze anyway:

```bash
rootcoz analyze --job-name my-job --build-number 42 --force
```

### Custom AI Instructions

Append custom instructions to the AI prompt:

```bash
rootcoz analyze \
  --job-name my-job \
  --build-number 42 \
  --raw-prompt "Focus on database connection timeouts. This is a known flaky area."
```

In the web UI, expand the **AI Configuration** section and fill in the **Raw Prompt** field.

### Additional Source Repositories

Give the AI access to additional code repositories for better context:

```bash
rootcoz analyze \
  --job-name my-job \
  --build-number 42 \
  --additional-repos "infra:https://github.com/org/infra,product:https://github.com/org/product:main"
```

The format is `name:url` or `name:url:ref` (comma-separated). In the web UI, use the **Source Repositories** section.

### Multi-AI Peer Analysis

Have multiple AI providers review and debate each other's classifications for higher accuracy:

```bash
rootcoz analyze \
  --job-name my-job \
  --build-number 42 \
  --peers "gemini:gemini-2.5-pro,cursor:gpt-5.4-xhigh" \
  --peer-analysis-max-rounds 3
```

See [Using Multi-AI Peer Analysis](peer-analysis.html) for details on configuring peer review.

### Saving Defaults in Config

Instead of passing flags every time, save defaults in your config file (`~/.config/rootcoz/config.toml`):

```toml
[servers.myserver]
url = "https://rootcoz.example.com"
username = "alice"
api_key = "rk_..."
ai_provider = "claude"
jenkins_url = "https://jenkins.example.com"
jenkins_user = "ci-user"
jenkins_password = "api-token"
enable_jira = true
peers = "gemini:gemini-2.5-pro"
```

Then simply run:

```bash
rootcoz --server myserver analyze --job-name my-job --build-number 42
```

See [Setting Up the CLI](cli-setup.html) for the full config file reference.

### Using Tags for Organization

Tags help you filter and group analyses on the dashboard:

```bash
rootcoz analyze \
  --job-name my-job \
  --build-number 42 \
  --tag nightly \
  --tag regression
```

Tags are normalized to lowercase and deduplicated automatically.

### pytest Integration

If you use pytest, you can enrich JUnit XML reports with AI analysis directly from your test runs without manually submitting files. See [Integrating with pytest](pytest-integration.html).

## Troubleshooting

**"This action requires a higher role"** — You need the `operator` or `admin` role to submit analyses. Ask an administrator to upgrade your role. See [Managing Users and Roles](managing-users.html).

**"No server specified"** — The CLI doesn't know where to connect. Set `--server`, the `ROOTCOZ_SERVER` environment variable, or configure a default server in `~/.config/rootcoz/config.toml`. See [Setting Up the CLI](cli-setup.html).

**Analysis stuck in "waiting" status** — The job is waiting for the Jenkins build to complete. Check that the build is still running in Jenkins. Use `rootcoz abort <job-id>` to cancel waiting, or set `--max-wait` to limit how long rootcoz waits.

**Analysis completed with "build passed"** — The Jenkins build reported SUCCESS and rootcoz skipped analysis. Use `--force` if you want to analyze a successful build.

**"file not found" when using --file** — Verify the path to your XML file. The CLI expects a local file path, not a URL.

**XML parsing errors** — Ensure your file is valid JUnit-compatible XML. rootcoz supports standard JUnit XML with `<testsuite>` and `<testcase>` elements containing `<failure>` or `<error>` children.

## Related Pages

- [Reviewing Analysis Results](reviewing-results.html)
- [Setting Up the CLI](cli-setup.html)
- [Using Multi-AI Peer Analysis](peer-analysis.html)
- [Configuring Integrations](configuring-integrations.html)
- [Integrating with pytest](pytest-integration.html)