# Building a Datadog Dashboard with RootCoz Data

Once RootCoz pushes results to Datadog, you can build one dashboard that shows your test runs next to the RootCoz failure analysis. This page gives you the checklist and the ready-made prompt for Datadog **Bits AI**, which builds the dashboard for you.

## Prerequisites
- RootCoz is configured to push results to Datadog. See [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html) for the exporter settings (`ENABLE_DATADOG`, `DATADOG_SITE`, and the two Datadog keys).
- You can open **Bits AI** in Datadog and create a dashboard in the same Datadog organization your test runs report to.

## 1. Check Your Setup First
- Your test results are in Datadog Test Optimization.
- Each test has `job_name` and `build_number` attributes. For example, add them when uploading: `datadog-ci junit upload --tags job_name:<job> --tags build_number:<n>`.
- RootCoz is set up to push results to Datadog.
- RootCoz uses the **same** job name and build number values as your tests.

## 2. Create the Dashboard
1. In Datadog, open **Bits AI**.
2. Paste the RootCoz dashboard prompt from [The RootCoz Dashboard Prompt](#the-rootcoz-dashboard-prompt) below.
3. Replace the placeholders:
   - `<PROJECT>`: your project name
   - `<TEST_SERVICE>`: your test service name in Test Optimization
   - `<CI_BUILD_URL>`: your CI build link pattern, for example `https://jenkins.example.com/job/{job}/{build}/`
4. Send it. Bits checks your data, builds the dashboard and gives you the link.
5. If Bits says something is missing, fix it and run the prompt again.

## 3. Use the Dashboard
1. In the **Builds** table, click a build and choose **Drill down to this build**.
2. Review the failed tests and the RootCoz classification and failure pattern charts.
3. Click a row or a chart slice to open the CI build, Test Optimization or the RootCoz events.

## Tips
- Always select one build. When `build_number` is `*`, the RootCoz numbers aren't reliable.
- Right after a RootCoz push, set the time range to **Past 1 Hour**. Longer ranges can show older numbers for a few minutes.
- Wait at least 1 minute between RootCoz pushes for the same build.
- A finished dashboard looks like [this RootCoz dashboard in Datadog](https://app.datadoghq.eu/dashboard/9si-chz-sbj).

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

## Related Pages

- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [Exploring History and Reports](explore-history-and-reports.html)
- [Configuration Reference](configuration-reference.html)
