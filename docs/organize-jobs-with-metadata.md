# Organize Jobs with Metadata

Tag jobs with ownership and release context so you can cut a noisy dashboard down to the work your team actually needs to handle. JJI lets you assign metadata manually, seed it in bulk, and preview automatic name-based matches before you rely on them.

## Prerequisites

- At least one analyzed job in JJI.
- Admin access if you want to set, import, or delete metadata.
- A working `jji` CLI profile if you want to use the commands below. See [CLI Command Reference](cli-command-reference.html) for full syntax.
- Access to server configuration if you want automatic rule-based assignment. See [Configuration and Environment Reference](configuration-and-environment-reference.html) for deployment details.

## Quick Example

```bash
jji metadata set "my-job" \
  --team platform \
  --tier critical \
  --version v1.0 \
  --label nightly \
  --label smoke

jji metadata get "my-job"
```

One update tags the Jenkins job itself, not just a single build, so the same metadata shows up on every analyzed run of that job.

## Step-by-Step

1. **Tag one job manually.**

```bash
jji metadata set "my-job" \
  --team platform \
  --tier critical \
  --version v1.0 \
  --label nightly \
  --label smoke
```

Use `jji metadata get "my-job"` to confirm what is stored.

2. **Update only the field that changed.**

```bash
jji metadata set "my-job" --team beta
```

This changes only `team`. Fields you leave out stay unchanged.

> **Tip:** When you pass `--label`, you replace the stored label list for that job. Pass every label you want to keep in the same command.

3. **Import metadata for existing jobs in bulk.**

```json
[
  {"job_name": "job-a", "team": "alpha"},
  {"job_name": "job-b", "team": "beta", "labels": ["ci"]}
]
```

```bash
jji metadata import metadata.json
```

Use bulk import when you already know the mapping and want to tag many existing jobs quickly. The import file can be JSON or YAML.

> **Note:** Import fully rewrites each job entry in the file. Omitted optional fields reset to blank or empty for those jobs.

4. **Configure automatic matching for new analyses.**

```yaml
metadata_rules:
  - pattern: 'test-*'
    team: qa
    labels: [smoke]
  - pattern: 'dev-*'
    labels: [dev]
```

Set the server's `METADATA_RULES_FILE` to your rules file, restart JJI, and then verify what the server loaded:

```bash
jji metadata rules
jji metadata preview "test-something"
```

Use the preview command to check a job name safely before a new analysis writes metadata. See [Configuration and Environment Reference](configuration-and-environment-reference.html) for the server setting.

5. **Let new analyses pick up metadata automatically.**

After the rules file is active, analyze or re-analyze a matching job. See [Analyze a Jenkins Job](analyze-a-jenkins-job.html) for the analysis workflow.

> **Warning:** Automatic rules do not overwrite existing job metadata. Manual or imported values stay in place until you change or delete them.

6. **Filter the dashboard into a smaller triage queue.**

```bash
jji metadata list --team alpha --tier critical
```

This shows only jobs whose stored metadata matches those filters. In the Dashboard, use the Team, Tier, and Version dropdowns plus the label chips to narrow the list, and look for metadata badges beside each job name.

> **Tip:** Dashboard metadata filters stay in the URL, so you can bookmark or share a focused queue for one team, one tier, or one release.

## Advanced Usage

| If you want to... | Use | What happens |
| --- | --- | --- |
| Change one field and keep the rest | `jji metadata set "my-job" --team beta` | Only the fields you pass are updated. |
| Replace metadata for one or many jobs from a file | `jji metadata import metadata.json` | Each imported job is fully rewritten. |
| Test automatic rules without storing anything | `jji metadata preview "test-something"` | JJI shows the match but does not save metadata. |

```bash
jji metadata set "folder/subfolder/my-job" --team platform
```

Quote folder-style job names when they include slashes.

- Rule order matters. If several rules match one job, the first matching rule wins for `team`, `tier`, and `version`.
- Labels accumulate across matching rules. A job like `test-smoke-gating` can pick up labels from more than one rule, and duplicates are removed automatically.
- Use simple glob patterns such as `test-*` for straightforward matching.
- If you want JJI to extract a value like `version`, use a named regex capture such as `(?P<version>[\d.]+z?)`.

> **Note:** JJI loads metadata rules once per server start. After you edit the rules file, restart the server before testing with `jji metadata preview`.

## Troubleshooting

- The Dashboard does not show metadata filters: add metadata to at least one job first. The filter bar only appears when JJI has stored metadata values to offer.
- `jji metadata set` or `jji metadata import` fails with a permission error: metadata write operations require admin access.
- Rule changes are not taking effect: confirm `METADATA_RULES_FILE` points to the right file, restart JJI, and run `jji metadata rules` or `jji metadata preview` again.
- A job still shows the old tags: existing metadata is preserved. Update it manually, re-import it, or run `jji metadata delete "my-job"` and analyze the job again.
- A multi-label filter is too narrow: selecting more than one label uses AND logic, so a job must contain every selected label.

## Related Pages

- [Manage Users, Access, and Token Usage](manage-users-access-and-token-usage.html)
- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [CLI Command Reference](cli-command-reference.html)
- [REST API Reference](rest-api-reference.html)
- [Configuration and Environment Reference](configuration-and-environment-reference.html)