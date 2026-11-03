# Create a RootCoz Dashboard in Datadog

RootCoz pushes every failed test to Datadog as an event and the failure counts as two gauge metrics. This page shows how to turn that data into one dashboard that sits next to your test runs, using the Datadog **Bits AI** prompt below.

## Prerequisites
- RootCoz's Datadog exporter is configured: `ENABLE_DATADOG`, `DATADOG_SITE`, the API key and application key, and `DATADOG_SERVICE=rootcoz`. The last one matters here: the dashboard filters RootCoz events on `service:rootcoz`, and the exporter only emits a `service:` tag when `DATADOG_SERVICE` is set. See [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html) for the exporter settings and [Configuration Reference](configuration-reference.html) for the `DATADOG_SERVICE` and `DATADOG_ENV` values.
- You can open **Bits AI** in Datadog, in the same Datadog organization your test runs report to.

## Step-by-Step

### 1. Check Your Setup First
- Your test results are in Datadog Test Optimization.
- Each test has `job_name` and `build_number` attributes. For example, add them when uploading: `datadog-ci junit upload --tags job_name:<job> --tags build_number:<n>`.
- RootCoz is set up to push results to Datadog.
- RootCoz uses the **same** job name and build number values as your tests.

> **Note:** RootCoz sanitizes a value before it becomes a Datadog tag — anything outside letters, digits, `.`, `_`, `:`, and `-` becomes `_`. A job named `CNV tier1 / nightly` therefore appears as `CNV_tier1___nightly`, and the dashboard's RootCoz filters only match that rewritten form. Job names made of letters, digits, dots, underscores, colons, and hyphens are kept as they are.

### 2. Create the Dashboard
1. In Datadog, open **Bits AI**.
2. Paste the RootCoz dashboard prompt from [The RootCoz Dashboard Prompt](#the-rootcoz-dashboard-prompt) below.
3. Replace the placeholders:
   - `<PROJECT>`: your project name
   - `<TEST_SERVICE>`: your test service name in Test Optimization
   - `<CI_BUILD_URL>`: your CI build link pattern, for example `https://jenkins.example.com/job/{job}/{build}/`
4. Send it. Bits checks your data, builds the dashboard and gives you the link.
5. If Bits says something is missing, fix it and run the prompt again.

> **Tip:** Keep the `{job}` and `{build}` tokens inside `<CI_BUILD_URL>` exactly as written — Bits substitutes them. Only the `<...>` placeholders are yours to replace.

### 3. Use the Dashboard
1. In the **Builds** table, click a build and choose **Drill down to this build**.
2. Review the failed tests and the RootCoz classification and failure pattern charts.
3. Click a row or a chart slice to open the CI build, Test Optimization or the RootCoz events.

## The RootCoz Dashboard Prompt

Copy this whole block into Bits AI and replace the three placeholders before you send it.

```text
Build a Datadog dashboard: "<PROJECT> Test Runs - RootCoz Failure Analysis".

My data:
- Test Optimization service: <TEST_SERVICE>
- Test attributes: @job_name, @build_number
- CI build link: <CI_BUILD_URL>   (e.g. https://jenkins.example.com/job/{job}/{build}/)
- RootCoz events: service:rootcoz, tags job_name, build_number, tfa.classification, tfa.failure_pattern
- RootCoz metrics (gauges): rootcoz.failures.classification by tfa.classification,
  rootcoz.failures.pattern by tfa.failure_pattern

First check that this data exists and the names are correct. Tell me what's missing. Don't guess.

Template variables: job_name, build_number (default *).
- Test queries:    @job_name:$job_name.value @build_number:$build_number.value
- RootCoz metrics: {$job_name,$build_number}
- RootCoz events:  job_name:$job_name.value build_number:$build_number.value

Widgets:
1. Note: "Pick a build in Builds > Drill down. Right after a RootCoz push, use Past 1 Hour."
2. Query value "Tests run": count of tests.
3. Query value "Failed tests": tests with @test.status:fail (red if > 0).
4. Query value "RootCoz-analyzed failures": sum:rootcoz.failures.classification, aggregator last.
5. Table "Builds": group by job + build; columns Failed, Total, Failure %.
   Row links: drill down (set template vars), open CI build, open failed tests
   in Test Optimization, open RootCoz events in Event Explorer.
6. Table "Failed tests": failed tests grouped by @test.name.
7. Pie "RootCoz classification": rootcoz.failures.classification by tfa.classification, aggregator last.
8. Pie "RootCoz failure pattern": rootcoz.failures.pattern by tfa.failure_pattern, aggregator last.
9. Event list "RootCoz events": service:rootcoz for the selected build.

Rules:
- Every test query includes: test_level:test @test.service:<TEST_SERVICE>
- Test widgets (2, 3, 5, 6): fixed Past 1 Month.
- RootCoz widgets (4, 7, 8): follow dashboard time (no fixed time range).
- Use RootCoz metrics, not event counts. Events can't be updated, so every re-push adds duplicates.
- Check that every widget returns data, then give me the dashboard link.
```

## Advanced Usage
- Always select one build. When `build_number` is `*`, the RootCoz numbers aren't reliable.
- Right after a RootCoz push, set the time range to **Past 1 Hour**. Longer ranges can show older numbers for a few minutes.
- Wait at least 1 minute between RootCoz pushes for the same build.
- A finished dashboard looks like [this RootCoz dashboard in Datadog](https://app.datadoghq.eu/dashboard/9si-chz-sbj). You need access to that Datadog organization to open it.
- A child-scoped push (`rootcoz push JOB_ID --plugin datadog --child-job-name ... --child-build-number ...`) writes its own metric series, tagged with `child_job_name`, `child_build_number`, and `rootcoz.scope_id`, on top of the parent job and build. When you push both a parent and its children, one build holds one series per pushed scope — filter on the scope tags, or the RootCoz totals add the parent and every child together.

## Troubleshooting
- Bits says the RootCoz data is missing.  
  Check that `DATADOG_SERVICE=rootcoz` is set (the `service:` tag is omitted otherwise), and that at least one push has run — `rootcoz push JOB_ID --plugin datadog`.

- The RootCoz widgets are empty for the build I picked.  
  Confirm the push used the same job name and build number as the test run, remembering that spaces and most punctuation in a job name become `_` in Datadog tags. Then set the time range to **Past 1 Hour**.

- The tests show up but the RootCoz charts stay at zero.  
  The classification gauges are still published when `DD_PUSH_CLASSIFICATIONS` is off, but every count is zero. Turn that setting on and push again.

- The push is refused with `409 job has N unreviewed failure(s)`.  
  Review the failures first, or set `DD_ALLOW_UNREVIEWED_PUSH=true` to push anyway. See [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html).

## Related Pages

- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [Exploring History and Reports](explore-history-and-reports.html)
- [Configuration Reference](configuration-reference.html)
