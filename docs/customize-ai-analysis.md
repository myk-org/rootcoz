# Customize AI Analysis

You want the AI to explain a failure with the right model, the right code context, and the right amount of debate. JJI lets you keep sensible defaults and then override them for one run or one rerun when a job needs better guidance.

## Prerequisites
- A JJI server or CLI profile you can submit jobs to.
- Jenkins access for the job you want to inspect.
- An authenticated AI CLI for `claude`, `gemini`, or `cursor`.

## Quick Example

```bash
jji analyze \
  --job-name my-job \
  --build-number 27 \
  --provider claude \
  --model opus-4
```

Use this when you want a single analysis with an explicit provider and model, without changing any saved defaults.

## Step-by-Step

1. Save the defaults you use most.

```toml
[default]
server = "prod"

[defaults]
ai_provider = "claude"
ai_model = "opus-4"
ai_cli_timeout = 10
tests_repo_url = "https://github.com/your-org/your-tests"
wait_for_completion = true
poll_interval_minutes = 2
max_wait_minutes = 0

[servers.prod]
url = "https://jji.example.com"
username = "jdoe"
```

Place this in `~/.config/jji/config.toml` when you want your everyday provider, model, repo, and wait settings to follow you. CLI flags still override these values for one-off runs.

2. Switch provider or model for one job.

```bash
jji analyze \
  --job-name my-job \
  --build-number 27 \
  --provider cursor \
  --model gpt-5.4-xhigh
```

This is the fastest way to compare models on the same kind of failure. It changes only this run.

3. Add peer reviewers when one model is not enough.

```bash
jji analyze \
  --job-name my-job \
  --build-number 27 \
  --provider claude \
  --model opus-4 \
  --peers "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro" \
  --peer-analysis-max-rounds 5
```

Each peer reviews the main model’s answer, and JJI can run multiple debate rounds before it returns the final analysis. Keep rounds low unless the failure is genuinely ambiguous.

4. Point JJI at the right repositories.

```bash
jji analyze \
  --job-name my-job \
  --build-number 27 \
  --provider claude \
  --model opus-4 \
  --tests-repo-url https://github.com/org/tests:feature/bar \
  --additional-repos "infra:https://github.com/org/infra:develop,product:https://github.com/org/product"
```

Use `--tests-repo-url` for the main test repo, including a branch or tag with `:ref`. Use `--additional-repos` when the explanation depends on code outside that repo, such as product or infra changes.

> **Tip:** Start with only the repositories that matter to the failure. Adding every nearby repo usually makes the run slower without improving the answer.

5. Add run-specific instructions.

```bash
jji analyze \
  --job-name my-job \
  --build-number 27 \
  --provider claude \
  --model opus-4 \
  --raw-prompt "Focus on network issues and cluster events first."
```

Keep the prompt short and goal-oriented. This works best as a temporary nudge for the current run, not as a permanent place to store team guidance.

6. Control timeouts, artifacts, and wait behavior.

```bash
jji analyze \
  --job-name my-job \
  --build-number 27 \
  --provider claude \
  --model opus-4 \
  --ai-cli-timeout 20 \
  --jenkins-artifacts-max-size-mb 50 \
  --wait \
  --poll-interval 5 \
  --max-wait 30
```

- `--ai-cli-timeout` controls how long each AI CLI call can run.
- `--no-get-job-artifacts` skips artifact downloads entirely when they are slow or noisy.
- `--jenkins-artifacts-max-size-mb` keeps artifact context from getting too large.
- `--wait`, `--poll-interval`, and `--max-wait` control how long JJI waits for Jenkins to finish before analysis starts.

> **Note:** `--max-wait 0` means no limit.

7. Rerun an existing result when you want a quick comparison.

```bash
jji re-analyze old-job-1
```

This reuses the saved settings from the original run. If you want to change provider, peers, repositories, prompt, or artifact settings during a rerun, use the report page’s `Re-Analyze Job` dialog instead. See [Review and Classify Failures](review-and-classify-failures.html) for the report workflow.

## Advanced Usage

Use the smallest scope that solves the problem. Server-wide defaults are best for shared behavior, CLI profile defaults are best for your personal baseline, and per-run overrides are best when only one job needs different treatment.

| What you want to change | One run | CLI profile | Server default |
| --- | --- | --- | --- |
| Main provider and model | `--provider`, `--model` | `ai_provider`, `ai_model` | `AI_PROVIDER`, `AI_MODEL` |
| AI timeout | `--ai-cli-timeout` | `ai_cli_timeout` | `AI_CLI_TIMEOUT` |
| Peer reviewers | `--peers`, `--peer-analysis-max-rounds` | `peers`, `peer_analysis_max_rounds` | `PEER_AI_CONFIGS`, `PEER_ANALYSIS_MAX_ROUNDS` |
| Tests repo | `--tests-repo-url` | `tests_repo_url` | `TESTS_REPO_URL` |
| Extra repos | `--additional-repos` | `additional_repos` | `ADDITIONAL_REPOS` |
| Prompt tweaks | `--raw-prompt`, or the report page’s `Re-Analyze Job` dialog | Not available | Not available |
| Artifact downloads | `--get-job-artifacts`, `--no-get-job-artifacts`, `--jenkins-artifacts-max-size-mb`, or the report page’s `Re-Analyze Job` dialog | Not available | `GET_JOB_ARTIFACTS`, `JENKINS_ARTIFACTS_MAX_SIZE_MB` |
| Jenkins wait behavior | `--wait`, `--no-wait`, `--poll-interval`, `--max-wait` | `wait_for_completion`, `poll_interval_minutes`, `max_wait_minutes` | `WAIT_FOR_COMPLETION`, `POLL_INTERVAL_MINUTES`, `MAX_WAIT_MINUTES` |

> **Note:** CLI flags override `~/.config/jji/config.toml`. See [CLI Command Reference](cli-command-reference.html) for the full flag list and [Configuration and Environment Reference](configuration-and-environment-reference.html) for the full env var list.

### Private Repos and Branch Pins

```bash
jji analyze \
  --job-name my-job \
  --build-number 27 \
  --additional-repos "infra:https://github.com/org/infra:develop@ghp_secret123"
```

Use `name:url:ref@token` when you need both a specific branch and a token for a private repo. Use `https://` for private repos, keep repo names unique, and do not use `build-artifacts` as a repo name.

### Stable Prompts vs One-Off Prompts

If a cloned repo contains `JOB_INSIGHT_PROMPT.md`, JJI can use it as project-specific analysis guidance. Put durable, team-wide instructions there and keep `--raw-prompt` for temporary nudges that only matter to the current run.

### Reruns With Overrides

The browser’s `Re-Analyze Job` dialog can override provider, model, prompt, peers, repositories, and artifact settings before it queues a new analysis. That is also the easiest way to turn peer review off for a single rerun when your saved config already enables peers.

If you need scripted reruns with overrides, use the REST re-analyze endpoint. See [REST API Reference](rest-api-reference.html) for details.

## Troubleshooting

- If JJI says no AI provider or model is configured, either pass `--provider` and `--model` on the command line or set defaults in `config.toml` or the server environment.
- If `--peers` fails, each entry must be `provider:model`, separated by commas. Supported providers are `claude`, `gemini`, and `cursor`.
- If `--additional-repos` fails to parse, use `name:url`, then add optional `:ref` and optional `@token` at the end. Repo names must be unique.
- If a rerun keeps using old peer settings, remember that `jji re-analyze` reuses the saved request. Use the report page’s `Re-Analyze Job` dialog or the REST re-analyze endpoint when you need overrides.
- If runs are slow or noisy, lower the artifact size cap, use `--no-get-job-artifacts`, point `--tests-repo-url` at the correct branch, or tighten `--raw-prompt` so the AI focuses on the right evidence.

## Related Pages

- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Copy Common Analysis Recipes](copy-common-analysis-recipes.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)
- [CLI Command Reference](cli-command-reference.html)
- [REST API Reference](rest-api-reference.html)