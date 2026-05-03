# Investigate Failure History

Use failure history when you want to answer "have we seen this before?" before you reclassify a failure or file a bug. Looking across earlier runs helps you separate recurring noise from a real new breakage.

## Prerequisites

- At least one completed analysis in JJI. See [Analyze a Jenkins Job](analyze-a-jenkins-job.html) for details.
- Access to the web app or a configured `jji` CLI. See [CLI Command Reference](cli-command-reference.html) for details.
- A browser username if you want to use the web UI. See [Configure Your Profile and Notifications](configure-your-profile-and-notifications.html) for details.

## Quick Example

```bash
jji history failures
```

Start here when you want the newest recorded failures across all analyzed jobs, then narrow the list by test, classification, job, or signature.

## Step-by-step

> **Tip:** Use the browser to browse and open results quickly. Switch to `jji` when you need an exact job filter, signature search, or JSON output.

| You want to know... | Fastest path |
| --- | --- |
| What has been failing lately? | `History` in the web app or `jji history failures` |
| How often does one test fail in one job? | `jji history test TEST_NAME --job-name JOB_NAME` |
| Do several tests share one failure pattern? | `jji history search --signature SIGNATURE` |
| Is one Jenkins job getting noisier overall? | `jji history stats JOB_NAME` |

1. Start with the broad list.

   ```bash
   jji history failures --limit 50
   ```

   In the browser, open `History` to scan the latest failures. Use the search box to narrow the list, use the classification picker to focus on one category, click a row to open the full result, or click the test name to open that test's history page.

2. Drill into one test.

   ```bash
   jji history test tests.network.TestDNS.test_lookup --job-name ocp-4.16-e2e
   ```

   Use this when one failure keeps reappearing and you want to know whether it is specific to one job. The test view shows recent runs, the latest classification, related comments, and timestamps such as first seen and last seen.

> **Note:** If you want pass counts or a failure rate, include `--job-name`. Without a job filter, the single-test view can still show failure history, but it may not have enough context to calculate a full pass/fail rate.

3. Check whether the same root cause hits multiple tests.

   ```bash
   jji history search --signature sig-abc
   ```

   Run this when several failures look related and you already have a signature value. The default output shows how many times that signature appeared and which tests share it, so you can tell whether you are looking at one repeated failure mode or several unrelated problems.

4. Measure job-wide noise before you decide it is a regression.

   ```bash
   jji history stats ocp-e2e
   ```

   This gives you the number of analyzed builds, how many of them had failures, and the most common failing tests for that job. Use it when the question is "is this job unhealthy lately?" rather than "what happened to this one test?"

5. Narrow the list to what you actually need to triage.

   ```bash
   jji history failures --job-name ocp-e2e --classification "REGRESSION"
   ```

   This is a good last step before taking action. If the pattern looks stable and already explained, continue with [Review and Classify Failures](review-and-classify-failures.html); if it points to a new defect, continue with [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html).

## Advanced Usage

```bash
jji history failures --search "DNS resolution failed"
```

Use `--search` when you only have a fragment of the failure in front of you. It is the quickest way to get back to the right test or result before you decide whether you need a deeper job or signature lookup.

```bash
jji history failures --job-name ocp-e2e --classification "FLAKY" --limit 50 --offset 50
```

Combine exact job filtering, classification filtering, and pagination when the browser list is too broad. The classification filter accepts `CODE ISSUE`, `PRODUCT BUG`, `FLAKY`, `REGRESSION`, `INFRASTRUCTURE`, `KNOWN_BUG`, and `INTERMITTENT`.

```bash
jji history test tests.TestA.test_one --job-name ocp-e2e --exclude-job-id job-99
jji history search --signature sig-abc --exclude-job-id job-99
jji history stats ocp-e2e --exclude-job-id job-99
```

Use `--exclude-job-id` when you are triaging one active result and want to compare it against older history without counting the current run. This makes it easier to decide whether the current failure is genuinely new.

```bash
jji --json history stats ocp-e2e
jji --json history search --signature sig-abc
```

Use JSON output for scripting, dashboards, or when you want fields that are not shown in the default table view. For example, `jji --json history stats` includes the recent trend value for the job. See [REST API Reference](rest-api-reference.html) if you want to automate the same workflows over HTTP.

> **Tip:** In the browser, you can sort the current page by test, job, classification, or date. Use pagination to move through older results.

## Troubleshooting

- **`Failure rate` shows `N/A`, or the per-test totals do not look useful.** Run `jji history test TEST_NAME --job-name JOB_NAME` so JJI can estimate pass/fail math for one job.
- **Signature search returns nothing.** Make sure you are using the signature value, not the human-readable error message. If all you have is the error text, start with `jji history failures --search "..."`.
- **The CLI says no server was specified.** Add `--server`, set `JJI_SERVER`, or configure a default CLI profile. See [CLI Command Reference](cli-command-reference.html) for details.
- **History looks empty right after you start a new analysis.** Wait for the analysis to complete, then try again. If you do not have any completed analyses yet, start with [Analyze a Jenkins Job](analyze-a-jenkins-job.html).

## Related Pages

- [Review and Classify Failures](review-and-classify-failures.html)
- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html)
- [CLI Command Reference](cli-command-reference.html)
- [REST API Reference](rest-api-reference.html)