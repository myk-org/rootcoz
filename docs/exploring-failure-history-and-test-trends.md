# Exploring Failure History and Test Trends

When a failure comes back again and again, you want to know whether it is a repeat problem, a flaky test, or just noise in one job. This guide shows how to search stored failures, inspect one test across runs, and compare classifications so you can decide faster and avoid repeating old investigation work.

## Prerequisites
- Your RootCoz server already has completed analyses to inspect.
- For the web app, you are signed in with a RootCoz username.
- For CLI examples, `rootcoz` is configured for your server with `--server` or a saved profile. See [Configuration Reference](configuration-reference.html).

## Quick Example
```bash
rootcoz history test tests.TestA.test_one
```

Use this first when you want the recorded failure history for one test, including recent failing runs, related comments, and the classification mix.

```bash
rootcoz history test tests.network.TestDNS.test_lookup --job-name ocp-4.16-e2e
```

Use the `--job-name` form when you want RootCoz to estimate pass/fail rate for one specific job instead of showing failure-only history.

> **Note:** The web app is the fastest way to browse and open related reports. The CLI is the fastest way to get job-scoped pass/fail math for one test.

## Step-by-Step
1. Open `History` in the web app. Start with a test name in the search box, and add a classification filter when you want to narrow the list to patterns like `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, or `KNOWN_BUG`. Click a row to open that report, or click the test name to open the per-test history view.

2. Read the test history page from top to bottom. Check the note under the test name before you interpret the summary cards, then use the classification chips and their counts to see how that test has been labeled over time.

3. Compare several entries in `Recent Runs`, not just the newest one. A steady label across many runs usually means a repeat problem, while labels that bounce across nearby runs are a strong signal to investigate flakiness or environment issues.

4. Check the comments before you start a fresh investigation. This is often the quickest place to find earlier bug links, ownership notes, or "already fixed" breadcrumbs that save you from reopening the same work.

5. When you need a cleaner trend view for one job, switch to the CLI.

   ```bash
   rootcoz history test tests.network.TestDNS.test_lookup --job-name ocp-4.16-e2e
   ```

   Use this when the web page gives you enough browsing context but you also want a job-scoped failure rate and a tighter comparison set. Add `--limit` if you want more recent failing runs in the output.

> **Tip:** If you need to locate the right report before switching to history, see [Finding Runs on the Dashboard](finding-runs-on-the-dashboard.html).

## Advanced Usage

| When you want to... | Use this command |
| --- | --- |
| Browse the full stored failure stream | `rootcoz history failures` |
| Compare one test against older runs only | `rootcoz history test tests.TestA.test_one --exclude-job-id job-99` |
| Check job-wide trend data for automation or scripts | `rootcoz --json history stats ocp-e2e` |
| Review saved classification decisions for one report | `rootcoz classifications list --job-id job-123` |
| Find every test that shares one known error signature | `rootcoz history search --signature sig-abc` |

Use `rootcoz history failures` when you want breadth. It is the best CLI view for scanning many stored failures, and you can narrow it further with `--search`, `--classification`, `--limit`, and `--offset`.

Use `rootcoz history test` when you want depth. Add `--job-name` for a meaningful pass/fail rate inside one job, and add `--exclude-job-id` when you are reviewing the current report and want to compare it only against older history.

Use `rootcoz --json history stats ocp-e2e` when you want machine-readable trend data. The JSON output includes the overall failure rate, common failures, and the `recent_trend` field.

Use `rootcoz classifications list --job-id ...` when you want to compare saved report decisions such as `PRODUCT BUG` or `INFRASTRUCTURE`. For the broader cross-build failure stream, stay with `history failures` or `history test`.

Use `rootcoz history search --signature ...` only when you already have the exact signature value from automation or JSON output. Signature matching is exact, so partial values will not help.

See [CLI Command Reference](cli-command-reference.html) for the full flag list.

## Troubleshooting
- The web test history page shows `Failure Rate` as `N/A`: this is expected when you are browsing failure-only history without a job filter. Run `rootcoz history test tests.network.TestDNS.test_lookup --job-name ocp-4.16-e2e` for job-scoped pass/fail math.
- A test history page is empty: RootCoz can only show stored analysis history. If that test has never failed in analyzed runs, there is nothing to compare yet.
- A run you just submitted is not showing up in history yet: wait for the analysis to finish, then refresh and try again. See [Tracking Analysis Progress](tracking-analysis-progress.html).
- The CLI says no server is configured or cannot connect: pass `--server` explicitly or fix your saved server profile. See [Configuration Reference](configuration-reference.html).

## Related Pages

- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)
- [Commenting and Mentioning Teammates](commenting-and-mentioning-teammates.html)
- [Finding Runs on the Dashboard](finding-runs-on-the-dashboard.html)
- [Automating Common Tasks with the CLI](automating-common-tasks-with-the-cli.html)
- [CLI Command Reference](cli-command-reference.html)