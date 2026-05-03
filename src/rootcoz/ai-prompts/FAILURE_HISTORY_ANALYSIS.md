# Failure History API — MANDATORY INSTRUCTIONS

You MUST follow ALL steps below for EVERY test failure you analyze. These steps are NOT optional. Skipping any step is a violation of your instructions.

## Step 1: Check Test History (MANDATORY for EVERY test)

For EVERY failed test, check its history BEFORE making any classification.

> **Note:** Test names and job names may contain special characters (brackets, spaces, slashes, `::`, etc.) that must be URL-encoded in curl commands. For example, `test_foo[param1]` becomes `test_foo%5Bparam1%5D`.

```bash
curl -s "{server_url}/history/test/{test_name}?exclude_job_id={job_id}" | python3 -m json.tool
```

Examine:
- `failure_rate` — how often does this test fail?
- `consecutive_failures` — is this an ongoing issue?
- `classifications` — how was it classified before?
- `comments` — did humans leave notes about this test?
- `recent_runs` — what happened in recent builds?

## Step 2: Search for Similar Errors (MANDATORY for EVERY test)

Check if other tests fail with the same error pattern:
```bash
curl -s "{server_url}/history/search?signature={error_signature}&exclude_job_id={job_id}" | python3 -m json.tool
```

If many tests share the same error signature, this likely indicates an INFRASTRUCTURE issue — not individual test failures.

## Step 3: Check Existing Classifications (MANDATORY for EVERY test)

See if this test was previously classified:
```bash
curl -s "{server_url}/history/classifications?test_name={test_name}" | python3 -m json.tool
```

If already classified, reference the existing classification and explain if your assessment agrees or differs.

## Step 4: Check Job Statistics (MANDATORY — once per job)

Understand the overall health of this job:
```bash
curl -s "{server_url}/history/stats/{job_name}?exclude_job_id={job_id}" | python3 -m json.tool
```

## Step 5: Classify EVERY Test (MANDATORY for EVERY test — NO EXCEPTIONS)

After completing your analysis, you MUST call POST /history/classify for EVERY test you analyzed. This is NOT optional. Every test gets a classification.

```bash
curl -s -X POST "{server_url}/history/classify" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "{test_name}",
    "classification": "KNOWN_BUG",
    "reason": "Explain why with specific evidence from history data",
    "job_name": "{job_name}",
    "job_id": "{job_id}",
    "references": "MTV-2385, https://github.com/org/repo/pull/123"
  }'
```

### Valid Classifications

| Classification | When to Use |
|---|---|
| `FLAKY` | Test sometimes passes, sometimes fails. Inconsistent results across runs. |
| `REGRESSION` | Test was previously passing and recently started failing. This applies to BOTH code issues AND product bugs — a product bug can be a regression. |
| `INFRASTRUCTURE` | Failure caused by infrastructure problems (cluster not deployed, network issues, resource limits), not by the test code or the product. |
| `KNOWN_BUG` | Failure matches a known, already-reported bug. Reference the bug in the reason. |
| `INTERMITTENT` | Similar to flaky but with a known trigger (e.g., timing, resource contention). |

### KNOWN_BUG Restriction (STRICT)

KNOWN_BUG can ONLY be used when the history API provides concrete evidence:
- A Jira ticket key found in historical comments (from /history/test/ response)
- A prior KNOWN_BUG classification with a Jira reference (from /history/classifications)

You MUST NOT classify as KNOWN_BUG based on:
- Your own training knowledge about product defects
- Pattern recognition from the error message alone
- Similarity to other failures in the SAME job run

If the history API returns no bug references, use REGRESSION, INFRASTRUCTURE, or INTERMITTENT instead.

### Classification Rules

1. You MUST classify EVERY test. No exceptions.
2. A test can be BOTH a PRODUCT BUG and a REGRESSION — these are orthogonal:
   - PRODUCT BUG / CODE ISSUE = the TYPE of issue (what's broken)
   - FLAKY / REGRESSION / INFRASTRUCTURE / KNOWN_BUG = the PATTERN (how it manifests)
3. If many tests fail because the core infrastructure wasn't deployed, classify ALL of them as INFRASTRUCTURE — not as individual regressions.
4. Always include a clear `reason` explaining your classification.
5. Always reference historical data in your reason (e.g., "This test failed in 8 of the last 10 runs" or "First failure, was passing in all prior builds").

### Evidence Requirements (MANDATORY)

Every classification MUST include evidence in the `reason` field:

| Classification | Required Evidence |
|---|---|
| KNOWN_BUG | ONLY if the history API returned a matching Jira ticket key from historical comments, or a prior KNOWN_BUG classification with a Jira reference. Your own knowledge about product defects does NOT count. If /history/test/ and /history/classifications return no Jira tickets or bug references, you CANNOT use KNOWN_BUG. Use REGRESSION or INFRASTRUCTURE instead. |
| REGRESSION | The date/build when the test started failing, what was passing before, correlation with git commits if available |
| FLAKY | Failure rate statistics, specific builds where it passed vs failed |
| INFRASTRUCTURE | The infrastructure error (e.g., "cluster not deployed", "node not ready"), evidence that multiple unrelated tests failed with the same root cause |
| INTERMITTENT | The trigger pattern, frequency, and conditions under which it occurs vs doesn't |

A classification without evidence is INVALID. Always cite:
- Specific data from /history/test/ (failure rates, consecutive failures, dates)
- Jira tickets or bug URLs from historical comments
- Error signatures shared across tests (from /history/search)
- Previous classifications and their reasons

## Rules

- ALWAYS complete Steps 1-3 and Step 5 for EVERY test. Step 4 is required once per job (not per test). No shortcuts.
- ALWAYS check history BEFORE classifying — don't classify blind.
- ALWAYS call POST /history/classify — this is how your classification is recorded. Include `references` with Jira keys, URLs, or other evidence identifiers.
- If many tests fail with the same infrastructure error (e.g., product not deployed), classify ALL as INFRASTRUCTURE.
- Reference existing comments, bugs, and history in your analysis.
- Your reason field should cite specific data from the history (failure rates, consecutive failures, first seen dates).

## Additional Resources

If a test repository is available as your working directory:
- Run `git log --oneline -20` to check for recent commits that may have caused regressions
- Check if a `JOB_INSIGHT_PROMPT.md` file exists in the repo root and follow its instructions
