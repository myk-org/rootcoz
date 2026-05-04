# Automating Common Tasks with the CLI

Copy these patterns as-is, then swap the concrete server, user, job, and test values for your environment. See [CLI Command Reference](cli-command-reference.html) for every flag, [API Endpoint Reference](api-endpoint-reference.html) for raw `--json` payloads, and [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html) for tracker-specific automation.

> **Note:** If you already use named server profiles in `~/.config/rootcoz/config.toml`, skip the `ROOTCOZ_SERVER` export and add `--server dev` after `rootcoz` instead.

## Fail Fast on Server Health
Use this before cron jobs or CI wrappers so you do not enqueue work against an unhealthy RootCoz server.

```bash
set -euo pipefail

export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"

python3 - <<'PY'
import json
import subprocess
import sys

health = json.loads(subprocess.check_output(["rootcoz", "--json", "health"], text=True))
print(f"rootcoz status: {health['status']}")

for name, check in sorted(health.get("checks", {}).items()):
    detail = check.get("detail", "")
    suffix = f" ({detail})" if detail else ""
    print(f"{name}: {check.get('status', 'unknown')}{suffix}")

sys.exit(1 if health["status"] == "unhealthy" else 0)
PY
```

`rootcoz health` uses the detailed `/api/health` endpoint when available and falls back to the lightweight legacy check when it is not. In automation, inspect the JSON `status` field yourself because RootCoz can still return structured health output when the server reports an unhealthy 503.

- `rootcoz --json capabilities` reports optional automation flags such as GitHub, Jira, and Report Portal support.
- Add `export ROOTCOZ_NO_VERIFY_SSL=true` when you are testing against a server with a self-signed certificate.

## Queue an Analysis and Wait for the Result
Use this when you want a single shell block that submits a Jenkins build, waits for RootCoz to finish, and leaves you with the final JSON payload.

```bash
set -euo pipefail

export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"
export ROOTCOZ_USERNAME="alice"

job_id="$(
  rootcoz --json analyze \
    --job-name "ocp-e2e-aws-ovn-upgrade" \
    --build-number 41892 \
    --provider "cursor" \
    --model "gpt-5.4-xhigh" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["job_id"])'
)"

while :; do
  status="$(
    rootcoz --json status "$job_id" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
  )"
  echo "$(date -u +%H:%M:%S) $job_id -> $status"

  case "$status" in
    completed|failed) break ;;
    pending|waiting|running) sleep 15 ;;
    *) echo "Unexpected status: $status" >&2; exit 1 ;;
  esac
done

rootcoz --json status "$job_id"
```

The first command queues analysis and captures the returned `job_id`, and the loop keeps polling stored job status until the run becomes `completed` or `failed`. The final `status --json` call gives the next step a full machine-readable result document instead of just a short table.

- Add `--wait --poll-interval 2 --max-wait 30` if the Jenkins build might still be running when you submit the analysis.
- `rootcoz ai-models --provider cursor` lists valid model IDs, and `rootcoz ai-configs` shows provider/model pairs that already completed successfully on this server.
- Use `--peers "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro"` or `--additional-repos "infra:https://github.com/acme/infra"` when the AI needs extra debate or repository context.

## Re-run a Previous Analysis
Use this when you want a fresh run from the same saved settings instead of rebuilding the original request by hand.

```bash
set -euo pipefail

export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"
export ROOTCOZ_USERNAME="alice"

source_job_id="$(
  rootcoz --json results list --limit 1 \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["job_id"])'
)"
new_job_id="$(
  rootcoz --json re-analyze "$source_job_id" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["job_id"])'
)"

echo "Re-analysis queued: $source_job_id -> $new_job_id"
```

The first command grabs the newest stored job ID, and the second queues a re-analysis that reuses the original request parameters. Replace the `results list` step with a specific job ID when you want to replay a particular historical run instead of the latest one.

## Review a Failing Test from the Shell
Use this when you already know the run and test you want to comment on and mark reviewed.

```bash
set -euo pipefail

export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"
export ROOTCOZ_USERNAME="alice"

job_id="9a5ac7e2-1b61-4e67-a2a8-0dbb3c7ef5b1"
test_name="tests.network.TestIngress.test_route_flaps"

rootcoz results review-status "$job_id"

rootcoz comments add "$job_id" \
  --test "$test_name" \
  --message "@bob reproduced this during the 2026-04-18 router rollout; tracking in NET-4281"

rootcoz results set-reviewed "$job_id" \
  --test "$test_name" \
  --reviewed
```

This checks the run's review counts, adds a comment to one failing test, and then marks that failure as reviewed under your username. It is the fastest CLI workflow when you already know the `job_id` and fully qualified test name you want to close out.

- `rootcoz mentionable-users` lists usernames you can safely `@mention`.
- `rootcoz classify "$test_name" --type INFRASTRUCTURE --job-id "$job_id" --reason "router rollout"` stores a classification alongside the review.
- Add `--child-job` and `--child-build` to both commands when the failure came from a pipeline child job.

## Check One Test's Failure History
Use this when you want the recent context for a single failing test before you classify or review it.

```bash
export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"
export ROOTCOZ_USERNAME="alice"

rootcoz history test \
  "tests.network.TestIngress.test_route_flaps" \
  --job-name "ocp-e2e-aws-ovn-upgrade" \
  --limit 10
```

This prints the failure count, failure rate, last classification, recent runs, and related comments for one fully qualified test name. Use it when you need quick context on whether a failure is new, noisy, or already well understood.

- Add `--exclude-job-id "9a5ac7e2-1b61-4e67-a2a8-0dbb3c7ef5b1"` to compare against earlier history without the run you are currently triaging.

## Build a Filtered Failure Queue
Use this when you need a small, scripted batch of historical failures filtered by job, classification, and search text.

```bash
set -euo pipefail

export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"
export ROOTCOZ_USERNAME="alice"

rootcoz --json history failures \
  --job-name "ocp-e2e-aws-ovn-upgrade" \
  --classification "PRODUCT BUG" \
  --search "route" \
  --limit 20 \
  --offset 0 \
  > product-bug-queue.json

echo "Saved product-bug-queue.json"
```

This builds a paginated slice of historical failures filtered by job, stored classification, and search text. It works well for ad hoc review queues and for exporting small batches into other tools.

- Drop `--json` if you want the built-in table instead of a file.
- If you already have a specific error signature hash, use `rootcoz history search --signature "4e6f3e8c1e6c4540833d6f2d3f89ce9a83c5a7d54b4f6e5b19f9d4f642b9a4c1"`.

## Work Through Unread Mentions
Use this when you want an inbox-like CLI loop for comments that mentioned your username.

```bash
export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"
export ROOTCOZ_USERNAME="alice"

rootcoz mentions --unread
rootcoz mentions-mark-read --ids "812,815"
```

`mentions --unread` limits the list to new notifications for the current user, and `mentions-mark-read` accepts a comma-separated list of comment IDs. This gives you a quick follow-up flow without opening the web UI.

- Use `rootcoz mentions-mark-all-read` to clear everything in one step.
- Add `--offset 50` to page through older mentions.

## Confirm Admin Access Before Changes
Use this before creating users, changing roles, or rotating keys so you know exactly which admin identity the CLI is using.

```bash
export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"
read -rsp "Admin API key: " ROOTCOZ_API_KEY
export ROOTCOZ_API_KEY
echo

rootcoz auth whoami
rootcoz admin users list
```

Admin commands authenticate with `--api-key` or `ROOTCOZ_API_KEY`, so this pattern keeps the key out of shell history while still making the rest of the shell session usable. `auth whoami` confirms the effective identity before you create, delete, or change users.

> **Warning:** `admin users create`, `admin users change-role ... admin`, and `admin users rotate-key` show the new API key once and it cannot be retrieved later.

- `rootcoz admin users create "release-bot"` creates a delegated admin and prints its first API key.
- `rootcoz admin users rotate-key "release-bot"` rotates an existing key.
- `rootcoz auth login --username "platform-admin" --api-key "$ROOTCOZ_API_KEY"` validates credentials without changing config, but it does not persist a session.

## Export Weekly Token Usage
Use this when you need a quick weekly usage file for chargeback, provider comparisons, or admin reporting.

```bash
export ROOTCOZ_SERVER="https://rootcoz.ci.example.com"
read -rsp "Admin API key: " ROOTCOZ_API_KEY
export ROOTCOZ_API_KEY
echo

rootcoz admin token-usage \
  --period week \
  --group-by provider \
  --format csv \
  > rootcoz-token-usage-weekly.csv

echo "Saved rootcoz-token-usage-weekly.csv"
```

With no filters, `admin token-usage` prints a summary dashboard; adding `--period` or `--group-by` switches it into filtered reporting mode. CSV output is convenient when you want a file you can hand off to finance, ops, or another script.

- `rootcoz admin token-usage --job-id "9a5ac7e2-1b61-4e67-a2a8-0dbb3c7ef5b1"` shows token and cost details for one analysis.
- `rootcoz --json admin token-usage --provider cursor --group-by model` gives machine-readable per-model usage for one provider.

## Related Pages

- [CLI Command Reference](cli-command-reference.html)
- [API Endpoint Reference](api-endpoint-reference.html)
- [Configuration Reference](configuration-reference.html)
- [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html)
- [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html)