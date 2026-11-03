# Create a RootCoz Dashboard in Datadog

RootCoz pushes every failed test to Datadog as an event and the failure counts as two gauge metrics. This page shows how to turn that data into one dashboard that sits next to your test runs, using the Datadog **Bits AI** prompt below.

## Prerequisites
- RootCoz's Datadog exporter is configured: `ENABLE_DATADOG`, `DATADOG_SITE`, the API key and application key, and `DATADOG_SERVICE=<ROOTCOZ_SERVICE>`. The last one matters here: the dashboard filters RootCoz events on a `service:` tag, and the exporter only emits that tag when `DATADOG_SERVICE` is set. See [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html) for the exporter settings and [Configuration Reference](configuration-reference.html) for the `DATADOG_SERVICE` and `DATADOG_ENV` values.
- You can open **Bits AI** in Datadog, in the same Datadog organization your test runs report to.

## Step-by-Step

### 1. Check Your Setup First
- Your test results are in Datadog Test Optimization.
- Each test has `job_name` and `build_number` attributes. For example, add them when uploading: `datadog-ci junit upload --tags job_name:<job> --tags build_number:<n>`.
- RootCoz is set up to push results to Datadog.
- RootCoz uses the **same** job name and build number values as your tests.

> **Note:** RootCoz sanitizes a value before it becomes a Datadog tag — anything outside letters, digits, `.`, `_`, `:`, and `-` becomes `_`. A job named `nightly tier1 / x86` therefore appears as `nightly_tier1___x86`, and the dashboard's RootCoz filters only match that rewritten form. Job names made of letters, digits, dots, underscores, colons, and hyphens are kept as they are.

### 2. Create the Dashboard
1. In Datadog, open **Bits AI**.
2. Paste the RootCoz dashboard prompt from [The RootCoz Dashboard Prompt](#the-rootcoz-dashboard-prompt) below.
3. Replace the placeholders:
   - `<PROJECT>`: your project name
   - `<TEST_SERVICE>`: your test service name in Test Optimization
   - `<ROOTCOZ_SERVICE>`: the `DATADOG_SERVICE` value you set in RootCoz, for example `rootcoz`
   - `<CI_BUILD_URL>`: your CI build link pattern, for example `https://jenkins.example.com/job/{job}/{build}/`
4. Send it. Bits checks your data, builds the dashboard and gives you the link.
5. If Bits says something is missing, fix it and run the prompt again.

> **Tip:** Keep the `{job}` and `{build}` tokens inside `<CI_BUILD_URL>` exactly as written — Bits substitutes them. Only the `<...>` placeholders are yours to replace.

### 3. Use the Dashboard
1. In the **Builds** table, click a build and choose **Drill down to this build**.
2. Review the failed tests and the RootCoz classification and failure pattern charts.
3. Click a row or a chart slice to open the CI build, Test Optimization or the RootCoz events.
4. In the **RootCoz pushes** table, find the push you want and filter the dashboard to that single `rootcoz.push_id`. Without that step, a failure that was re-pushed more than once is counted more than once.

## The RootCoz Dashboard Prompt

Copy this whole block into Bits AI and replace the four placeholders before you send it.

```text
Build a Datadog dashboard: "<PROJECT> Test Runs - RootCoz Failure Analysis".

My data:
- Test Optimization service: <TEST_SERVICE>
- Test attributes: @job_name, @build_number
- CI build link: <CI_BUILD_URL>   (e.g. https://jenkins.example.com/job/{job}/{build}/)
- RootCoz events: service:<ROOTCOZ_SERVICE>, tags job_name, build_number, tfa.classification,
  tfa.failure_pattern, rootcoz.failure_id, rootcoz.push_id
- RootCoz metrics (gauges): rootcoz.failures.classification by tfa.classification,
  rootcoz.failures.pattern by tfa.failure_pattern. Their only tags are job_name and build_number.

First check that this data exists and the names are correct. Tell me what's missing. Don't guess.

Template variables: job_name, build_number, push_id (default *). Use the full form $var inside
filters, never $var.value, so that "*" means no filter.
- Test queries:    @job_name:$job_name @build_number:$build_number
- RootCoz metrics: {$job_name,$build_number}
- RootCoz events:  $job_name $build_number $push_id   (push_id is a rootcoz.push_id value)

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
9. Table "RootCoz pushes": RootCoz events grouped by rootcoz.push_id, with the number of distinct
   rootcoz.failure_id per push, so I can set the push_id template variable to one push.
10. Event list "RootCoz events": service:<ROOTCOZ_SERVICE> for the selected build and push.

Rules:
- Every test query includes: test_level:test @test.service:<TEST_SERVICE>
- Base RootCoz filter: service:<ROOTCOZ_SERVICE> -diagnostic:true
- Test widgets (2, 3, 5, 6): fixed Past 1 Month.
- RootCoz widgets (4, 7, 8): follow dashboard time (no fixed time range).
- The rootcoz.failures.* gauges are the deduplicated counts: a gauge is overwritten on every push,
  so a re-push replaces the number instead of adding to it. Use them for the classification and
  pattern totals. They carry no team, version, reviewer or per-failure data.
- When a widget needs anything the metrics do not carry, query the RootCoz events instead and count
  distinct rootcoz.failure_id, not events: a re-push resends the same failures.
- A re-push can re-classify the same failure. To show the current classification, filter the event
  widgets by a single rootcoz.push_id (the push_id template variable).
- Event-query tables: at most 3 group-bys, and the product of the group limits must stay under 10,000.
  Cardinality with 3 group-bys is rejected - use a count there.
- Event widgets cannot compute on "timestamp", so there is no "latest push" sort.
- Check that every widget returns data with verify_widget_data, then give me the dashboard link.
```

## Optional: Team and Version on the RootCoz Events

RootCoz sends `job_name` and `build_number` on every event and metric, and nothing about who owns the job or which product version it tests — those values live in your CI job names, not in RootCoz. If you want `team` and `version` filters, Datadog has to add them.

An administrator sets this up once. Bits AI cannot create Event pipelines.

1. In Datadog, open **Event Management**, then **Pipelines**, and create a pipeline whose filter is `service:<ROOTCOZ_SERVICE>`. Add these processors in this order:
   1. **Remapper**: tag key `job_name` to attribute `job_name`. Keep *Preserve source* checked.
   2. **Lookup**, from the *Reference Table* tab: source `job_name`, target `rt`, using a table that maps every job name to its team.
   3. **Remapper**: attribute `rt.team` to attribute `team`. Check *Override on conflict*.
   4. **Grok parser** on `job_name`, to pull the version out of the name:

      ```text
      extract_version %{data}<product>-%{regex("[0-9]+\\.[0-9]+"):version}-%{data}
      ```

2. Keep the lookup table current. A job that is not in it gets no team. Ask Bits to refresh it from Test Optimization when new jobs appear.
3. Add `team` and `version` template variables to the dashboard, and add `$team $version` to the RootCoz widget filters.

Two limits come with this setup. A pipeline only enriches events that arrive **after** it is saved, so events pushed before that have no team and no version. And the metrics never carry these attributes, so any team- or version-filtered number has to come from events.

A cleaner long-term fix is for RootCoz to send the team and the version on its events and metrics itself, which would remove the pipeline, the lookup table, and the manual push selection.

## Advanced Usage
- Always select one build. When `build_number` is `*`, the RootCoz numbers aren't reliable.
- Right after a RootCoz push, set the time range to **Past 1 Hour**. Longer ranges can show older numbers for a few minutes.
- Wait at least 1 minute between RootCoz pushes for the same build.
- A finished dashboard looks like [this RootCoz dashboard in Datadog](https://app.datadoghq.eu/dashboard/9si-chz-sbj). You need access to that Datadog organization to open it.
- A child-scoped push (`rootcoz push JOB_ID --plugin datadog --child-job-name ... --child-build-number ...`) writes its own metric series, tagged with `child_job_name`, `child_build_number`, and `rootcoz.scope_id`, on top of the parent job and build. When you push both a parent and its children, one build holds one series per pushed scope — filter on the scope tags, or the RootCoz totals add the parent and every child together.
- Count unique failures by `rootcoz.failure_id`, not by events or by `rootcoz.push_id`: the same failure keeps its id across re-pushes, while every push gets a new push id.

## Troubleshooting
- Bits says the RootCoz data is missing.  
  Check that `DATADOG_SERVICE` is set to the value you gave as `<ROOTCOZ_SERVICE>` (the `service:` tag is omitted otherwise), and that at least one push has run — `rootcoz push JOB_ID --plugin datadog`.

- The RootCoz widgets are empty for the build I picked.  
  Confirm the push used the same job name and build number as the test run, remembering that spaces and most punctuation in a job name become `_` in Datadog tags. Then set the time range to **Past 1 Hour**.

- The tests show up but the RootCoz charts stay at zero.  
  The classification gauges are still published when `DD_PUSH_CLASSIFICATIONS` is off, but every count is zero. Turn that setting on and push again.

- The failure count is higher than the number of failed tests.  
  Something is counting events instead of distinct `rootcoz.failure_id` values, so every re-push adds to the total. Switch that widget to the distinct count, or to the `rootcoz.failures.*` gauge.

- The classification chart shows two classifications for the same failure.  
  More than one push is in the selected time range and a re-push re-classified the failure. Filter the dashboard to a single `rootcoz.push_id`.

- The team or version filter returns nothing.  
  The pipeline only enriches events that arrived after it was saved, and a job that is missing from the lookup table gets no team. The metrics never carry either attribute, so those numbers have to come from events.

- The push is refused with `409 job has N unreviewed failure(s)`.  
  Review the failures first, or set `DD_ALLOW_UNREVIEWED_PUSH=true` to push anyway. See [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html).

## Related Pages

- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [Exploring History and Reports](explore-history-and-reports.html)
- [Configuration Reference](configuration-reference.html)
