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

### User Override Protection (STRICT — NEVER VIOLATE)

When checking existing classifications, look at the `created_by` field:
- If `created_by` is a **username** (not `"ai"`), this is a **user classification** — a deliberate human decision.
- **You MUST NOT override user classifications.** Do not call `POST /history/classify` for tests that have a user classification.
- If you disagree with a user's classification, note your disagreement in the analysis details only — the user's classification stands.
- If a user has classified a test, do NOT mark that test as reviewed — leave it for the user to review.
- Only override classifications where `created_by` is `"ai"` (from a previous AI analysis).

## Step 4: Check Job Statistics (MANDATORY — once per job)

Understand the overall health of this job:
```bash
curl -s "{server_url}/history/stats/{job_name}?exclude_job_id={job_id}" | python3 -m json.tool
```

## Step 5: Classify EVERY Test's Pattern (MANDATORY for EVERY test — NO EXCEPTIONS)

After completing your analysis, you MUST call POST /history/classify for EVERY test you analyzed. This is NOT optional. Every test gets a pattern classification.

**EXCEPTION — User Override Protection:** If Step 3 revealed that a **user** (not AI) has already classified this test, **skip** the POST /history/classify call for that test. User classifications are sacrosanct — never override them. You may note your assessment in the analysis output, but do not write a competing classification.

**IMPORTANT:** This step sets the **pattern** axis (how the failure manifests), NOT the root cause axis (classification). The root cause (CODE ISSUE / PRODUCT BUG / INFRASTRUCTURE) was already determined in the initial analysis. Do NOT change the root cause here — only determine the pattern.

```bash
curl -s -X POST "{server_url}/history/classify" \
  -H "Content-Type: application/json" \
  -d '{
    "test_name": "{test_name}",
    "classification": "KNOWN_BUG",
    "reason": "Explain why with specific evidence from history data",
    "job_name": "{job_name}",
    "job_id": "{job_id}",
    "references": "MTV-2385, https://github.com/org/repo/pull/123",
    "source": "ai"
  }'
```

### Valid Pattern Classifications

| Pattern | When to Use |
|---|---|
| `NEW` | First occurrence — no prior history of this failure. |
| `REGRESSION` | Test was previously passing and recently started failing. This applies to BOTH code issues AND product bugs — a product bug can be a regression. |
| `FLAKY` | Test sometimes passes, sometimes fails. Inconsistent results across runs. |
| `INTERMITTENT` | Similar to flaky but with a known trigger (e.g., timing, resource contention). |
| `KNOWN_BUG` | Failure matches a known, already-reported bug. Reference the bug in the reason. |
| `PERSISTENT` | Consistently failing across many runs — not intermittent, not new. |

### KNOWN_BUG Restriction (STRICT)

KNOWN_BUG can ONLY be used when the history API provides concrete evidence:
- A Jira ticket key found in historical comments (from /history/test/ response)
- A prior KNOWN_BUG classification with a Jira reference (from /history/classifications)

You MUST NOT classify as KNOWN_BUG based on:
- Your own training knowledge about product defects
- Pattern recognition from the error message alone
- Similarity to other failures in the SAME job run

If the history API returns no bug references, use REGRESSION, PERSISTENT, or INTERMITTENT instead.

### Pattern Classification Rules

1. You MUST classify the pattern for EVERY test. No exceptions.
2. The root cause (CODE ISSUE / PRODUCT BUG / INFRASTRUCTURE) was already determined — do NOT change it here. Only determine the pattern.
3. A test can be BOTH a PRODUCT BUG and a REGRESSION — these are on two orthogonal axes:
   - **Root cause** (classification): CODE ISSUE / PRODUCT BUG / INFRASTRUCTURE
   - **Pattern**: NEW / REGRESSION / FLAKY / INTERMITTENT / KNOWN_BUG / PERSISTENT
4. If many tests share the same infrastructure error, their pattern may still vary (NEW vs PERSISTENT vs REGRESSION).
5. Always include a clear `reason` explaining your pattern classification.
6. Always reference historical data in your reason (e.g., "This test failed in 8 of the last 10 runs" or "First failure, was passing in all prior builds").

### Evidence Requirements (MANDATORY)

Every pattern classification MUST include evidence in the `reason` field:

| Pattern | Required Evidence |
|---|---|
| NEW | First failure — no prior occurrences in /history/test/ |
| KNOWN_BUG | ONLY if the history API returned a matching Jira ticket key from historical comments, or a prior KNOWN_BUG classification with a Jira reference. Your own knowledge about product defects does NOT count. If /history/test/ and /history/classifications return no Jira tickets or bug references, you CANNOT use KNOWN_BUG. Use REGRESSION or PERSISTENT instead. |
| REGRESSION | The date/build when the test started failing, what was passing before, correlation with git commits if available |
| FLAKY | Failure rate statistics, specific builds where it passed vs failed |
| INTERMITTENT | The trigger pattern, frequency, and conditions under which it occurs vs doesn't |
| PERSISTENT | Consistently failing across many consecutive runs — cite consecutive_failures count from /history/test/ |

A pattern classification without evidence is INVALID. Always cite:
- Specific data from /history/test/ (failure rates, consecutive failures, dates)
- Jira tickets or bug URLs from historical comments
- Error signatures shared across tests (from /history/search)
- Previous classifications and their reasons

## Rules

- ALWAYS complete Steps 1-3 and Step 5 for EVERY test. Step 4 is required once per job (not per test). No shortcuts.
- ALWAYS check history BEFORE classifying — don't classify blind.
- ALWAYS call POST /history/classify — this is how your classification is recorded. Include `references` with Jira keys, URLs, or other evidence identifiers.
- If many tests fail with the same infrastructure error (e.g., product not deployed), their pattern is likely PERSISTENT (consistently failing).
- Reference existing comments, bugs, and history in your analysis.
- Your reason field should cite specific data from the history (failure rates, consecutive failures, first seen dates).

## Additional Resources

If a test repository is available as your working directory:
- Run `git log --oneline -20` to check for recent commits that may have caused regressions
- Check if a `JOB_INSIGHT_PROMPT.md` file exists in the repo root and follow its instructions
