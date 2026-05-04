# Submitting Jenkins Builds and JUnit XML

Submit a Jenkins build or a JUnit XML file when you want rootcoz to analyze failures with the right AI, repository context, peer review, and Jira search for that specific run. Setting those options before you submit gives the first report better evidence and usually saves a re-run.

## Prerequisites

- A reachable rootcoz server.
- An AI provider and model. If you do not set them per submission, they must already be configured as defaults.
- For Jenkins submissions: the job name and build number, plus Jenkins connection details if your server does not already have them.
- For repository-aware analysis: the tests repo URL, and access to clone it if it is private.
- For Jira matching: a Jira URL, project key, and either Jira Cloud email + API token or a Jira Server/Data Center PAT available for the run.
- If you still need the initial setup, see [Run Your First Analysis](run-your-first-analysis.html).

## Quick Example

**Jenkins build**

```bash
rootcoz --server http://localhost:8000 analyze \
  --job-name my-job \
  --build-number 42 \
  --provider claude \
  --model opus-4
```

**JUnit XML file**

1. Open `New Analysis`.
2. Choose `Paste XML` or `Upload File`.
3. Add the XML, choose the AI settings for this run, and click `Submit Analysis`.

> **Tip:** To see model IDs your server currently exposes, run `rootcoz --server http://localhost:8000 ai-models --provider claude`.

| If you already have… | Use this path | What happens next |
| --- | --- | --- |
| A Jenkins build number | `Jenkins Job` in the web app or `rootcoz analyze` | rootcoz can wait for the build, pull Jenkins test data, and optionally fetch artifacts |
| A finished `.xml` file | `Paste XML` or `Upload File` in the web app | rootcoz analyzes the XML immediately and opens the result directly |

## Step-by-Step

```bash
rootcoz --server http://localhost:8000 analyze \
  --job-name my-job \
  --build-number 42 \
  --provider claude \
  --model opus-4 \
  --tests-repo-url https://github.com/org/tests:release-4.19 \
  --additional-repos "infra:https://github.com/org/infra,product:https://github.com/org/product" \
  --peers "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro" \
  --peer-analysis-max-rounds 5 \
  --jira \
  --jira-url https://jira.example.com \
  --jira-email user@example.com \
  --jira-api-token "$JIRA_API_TOKEN" \
  --jira-project-key PROJ \
  --wait \
  --poll-interval 2 \
  --max-wait 60
```

Use that as a Jenkins template. The steps below show how to map the same choices into `New Analysis`, or trim the command down when you only need a few overrides.

1. Choose the submission path that matches what you already have. Use `Jenkins Job` or `rootcoz analyze` when the build is in Jenkins. Use `Paste XML` or `Upload File` when you already have a JUnit XML file and do not need Jenkins polling.

> **Note:** In the current codebase, `rootcoz analyze` submits Jenkins jobs. Raw JUnit XML submission is handled in the web app, and the bundled pytest example can send XML automatically after a test run.

2. Identify the Jenkins build or XML file. For Jenkins, enter the job name and build number; folder-style names such as `folder/job-name` are supported. Add `Jenkins URL`, `Jenkins User`, and `Jenkins Password / Token` only when you need to override the server defaults for this run.

For a running Jenkins build, leave `Wait for build completion` turned on. In the CLI, use `--wait`, `--poll-interval`, and `--max-wait`; use `--no-wait` if you want to skip Jenkins polling for that submission. For XML, paste the full JUnit XML or upload a `.xml` file.

3. Set the AI provider and model for this job. Rootcoz accepts `claude`, `gemini`, and `cursor`. In the form, pick `AI Provider` and `AI Model`; in the CLI, use `--provider` and `--model`.

If you need a quick lookup first, run:

```bash
rootcoz --server http://localhost:8000 ai-models --provider claude
```

Use `AI CLI Timeout` or `--ai-cli-timeout` when one provider/model needs more time than your default. Use `Raw Prompt` or `--raw-prompt` only when this run needs extra instructions that do not belong in your normal setup.

4. Add repository context before you submit. `Tests Repo URL` gives rootcoz a repository to clone for code context, and in the web app you can split that into `Tests Repo URL` plus `Ref / Branch`. In the CLI, append the ref to the URL, such as `https://github.com/org/tests:release-4.19`.

Use `Additional Repositories` or `--additional-repos` when one repository is not enough. The CLI format is `name:url,name:url`, and each entry can also be pinned to a ref the same way. If the tests repo is private, use `--tests-repo-token` or store that token in your CLI config; the web form assumes the server already has repository access.

5. Turn on peer analysis when you want a second opinion before you review failures. In the web app, toggle `Enable peer review`, add one or more peers, and set `Max Rounds`. In the CLI, use `--peers "provider:model,provider:model"` and optionally `--peer-analysis-max-rounds`.

Peer analysis is best when you want the main AI answer challenged by other models. Keep `Max Rounds` small unless the disagreement is genuinely valuable; the allowed range is `1` to `10`.

6. Enable Jira search when you want rootcoz to look for related bugs during analysis. In the web app, turn on `Enable Jira search` and set `Jira URL` plus `Project Key` when you need per-job overrides. In the CLI, `--jira` turns it on and you can also pass auth and SSL options per submission.

For Jira Cloud, use `--jira-email` plus `--jira-api-token`. For Jira Server/Data Center, use `--jira-pat`. If you want to skip Jira matching for one run, use `--no-jira` or switch `Enable Jira search` off.

> **Warning:** Jira matching only works when rootcoz has a Jira URL, project key, and credentials for the run. That can come from server defaults, CLI config, or the CLI flags you pass on this submission.

Searching Jira during analysis is separate from creating a ticket afterward. See [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html) when you are ready to file one.

7. Submit the job and open the result. In the web app, click `Submit Analysis`. Jenkins submissions open the status view first; XML submissions open the result directly.

From the CLI, rootcoz prints the queued job ID and poll URL. To check the current state, run:

```bash
rootcoz --server http://localhost:8000 status <job_id>
```

When the analysis is complete, review the grouped failures and classifications. See [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html) for the report workflow.

## Advanced Usage

### Save your usual defaults in CLI config

If you submit similar jobs every day, put the stable settings in `$XDG_CONFIG_HOME/rootcoz/config.toml` or `~/.config/rootcoz/config.toml`. Then your command stays short and you only override what changes for a specific run.

```toml
[default]
server = "dev"

[defaults]
jenkins_url = "https://jenkins.example.com"
tests_repo_url = "https://github.com/your-org/your-tests"
ai_provider = "cursor"
ai_model = "gpt-5.4-xhigh"
wait_for_completion = true
poll_interval_minutes = 2
max_wait_minutes = 0
enable_jira = true
peers = "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro"
peer_analysis_max_rounds = 3

[servers.dev]
url = "http://localhost:8000"
```

With that in place, a typical submission becomes:

```bash
rootcoz --server dev analyze --job-name my-job --build-number 42
```

See [CLI Command Reference](cli-command-reference.html) for the full flag list, or [Automating Common Tasks with the CLI](automating-common-tasks-with-the-cli.html) for reusable patterns.

### Analyze successful builds or control artifact download

Jenkins submissions support two useful per-job overrides. Turn on `Force analysis on successful builds` or use `--force` when you want AI analysis even though Jenkins reported `SUCCESS`. Leave `Fetch build artifacts` on or use `--get-job-artifacts` when logs or screenshots matter, and use `Max Size (MB)` or `--jenkins-artifacts-max-size-mb` to cap how much rootcoz downloads for that run.

Artifact download is enabled by default. If you want a lighter analysis, turn it off with `--no-get-job-artifacts`.

### Submit JUnit XML straight from pytest

After you add the bundled pytest hook to your project, you can send JUnit XML to rootcoz automatically and write the enriched XML back to the same file after failures. Set the server and AI environment variables, then run pytest with both `--junitxml` and `--analyze-with-ai`.

```bash
export ROOTCOZ_SERVER=http://localhost:8000
export ROOTCOZ_AI_PROVIDER=claude
export ROOTCOZ_AI_MODEL=opus-4
pytest --junitxml=report.xml --analyze-with-ai
```

> **Note:** This workflow only runs when the test session fails, and it skips enrichment if `--junitxml` was not supplied.

The returned XML includes rootcoz analysis properties and a `report_url` property that points back to the rootcoz result.

### Override SSL verification only for one run

If Jenkins or Jira uses a self-signed certificate, the CLI lets you relax SSL verification per job instead of changing your global setup. Use `--no-jenkins-ssl-verify` and/or `--no-jira-ssl-verify` only for the affected submission.

## Troubleshooting

- `No AI provider configured` or `No AI model configured`: set the provider/model in the submission itself or add them to your normal defaults before submitting again.
- `Invalid XML`: upload or paste a real JUnit XML document, not a console log or HTML report.
- `No test failures found in the provided XML.`: rootcoz still creates a completed result, but it keeps the original XML because there was nothing to analyze.
- `Jenkins reachability check failed`: verify the Jenkins URL, credentials, and network path from the rootcoz server. If the build is already complete and you do not want polling, try `--no-wait`.
- `Timed out waiting for Jenkins job ...`: increase `Max Wait` / `--max-wait`, or submit later when the build is closer to completion.
- Jira search shows no useful matches: confirm the Jira URL, project key, and credentials available for the run, and turn Jira off for jobs where that search is not helpful.

## Related Pages

- [Run Your First Analysis](run-your-first-analysis.html)
- [Tracking Analysis Progress](tracking-analysis-progress.html)
- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)
- [Automating Common Tasks with the CLI](automating-common-tasks-with-the-cli.html)
- [CLI Command Reference](cli-command-reference.html)