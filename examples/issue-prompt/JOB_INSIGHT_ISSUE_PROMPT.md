# Issue Creation Guidelines

These instructions apply when generating a GitHub or Jira issue from
an already-analyzed test failure. Use ONLY the data provided in this
prompt (test name, error, classification, analysis, artifacts evidence).
Do NOT attempt to read files or access external systems.

## Environment section

Extract component versions from all provided data (analysis details,
artifacts evidence, error messages, product bug report) and include a
"## Environment" section at the top of the issue body:

```
## Environment

| Component | Version |
|-----------|---------|
| OpenShift | 4.22.0-rc.2 |
| CNV       | 4.22.0 |
| KubeVirt  | v1.8.1-87-gdd92d50470 |
```

Only include versions found in the provided analysis data. If no versions
are mentioned, omit the Environment section entirely.

## Issue title

- Max 120 characters
- Start with the affected component in brackets (e.g., `[KubeVirt]`, `[CDI]`, `[NMState]`)
- Describe the problem, not the test
- Do NOT include the test name in the title

Good: `[KubeVirt] Live migration fails when source node loses network connectivity`
Bad: `test_live_migration_with_network_loss fails with TimeoutError`

## Issue body structure

### PRODUCT BUG

```markdown
## Environment
<!-- version table if versions available -->

## Description
<!-- What product component is broken and how it manifests -->

## Steps to Reproduce
1. <!-- specific steps to trigger the bug based on the analysis -->

## Expected Behavior
<!-- what should happen -->

## Actual Behavior
<!-- what happens instead, include error message -->

## Evidence
<!-- verbatim lines from the artifacts evidence provided -->

## Affected Component
<!-- component name and code path if identified in the analysis -->

## References
<!-- Jenkins build and RootCoz analysis links if provided -->
```

### INFRASTRUCTURE

```markdown
## Environment
<!-- version table if versions available -->

## Description
<!-- What infrastructure component failed -->

## Symptoms
<!-- Observable symptoms from the analysis -->

## Evidence
<!-- verbatim lines from the artifacts evidence provided -->

## Impact
<!-- Scope of impact based on the analysis -->

## References
<!-- Jenkins build and RootCoz analysis links if provided -->
```

### CODE ISSUE

```markdown
## Environment
<!-- version table if versions available -->

## Description
<!-- What is wrong with the test code -->

## Failing Test
<!-- test file, line number, and test name from the analysis -->

## Root Cause
<!-- Why the test fails based on the analysis -->

## Suggested Fix
<!-- Code change from the code_fix if provided -->

## Evidence
<!-- Error message and stack trace -->

## References
<!-- Jenkins build and RootCoz analysis links if provided -->
```

## Sensitive data

Before generating the issue, verify that **NO secrets, sensitive data or
internal links** are included anywhere in the title or body:

- API keys, tokens, passwords, credentials
- Internal hostnames, IP addresses, kubeconfig paths
- User-specific paths (e.g., `/home/jenkins/...`)
- Environment variables containing secrets

If any sensitive value appears in the provided data, replace it with
`***MASKED***`.
