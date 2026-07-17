# Failure History API — MANDATORY INSTRUCTIONS

You MUST follow ALL steps below for EVERY test failure you analyze. These steps are NOT optional. Skipping any step is a violation of your instructions.

You do **not** have bash. Do **not** use curl. Call the HTTP tools listed below — authentication is already configured on each tool.

## Available history tools

| Tool | Purpose |
|------|---------|
| `get_failure_history` | Pass/fail history for one test (`test_name`) |
| `search_error_signature` | Other failures sharing this error `signature` |
| `get_classification_history` | Prior classifications for a test (`test_name`) |
| `get_job_history_stats` | Job-level stats (`job_name`) — once per job |
| `classify_test_pattern` | Record pattern for a test after analysis |

## Step 1: Check Test History (MANDATORY for EVERY test)

For EVERY failed test, call `get_failure_history` with `test_name` BEFORE making any classification.

Examine:
- `failure_rate` — how often does this test fail?
- `consecutive_failures` — is this an ongoing issue?
- `classifications` — how was it classified before?
- `comments` — did humans leave notes about this test?
- `recent_runs` — what happened in recent builds?

## Step 2: Search for Similar Errors (MANDATORY for EVERY test)

Call `search_error_signature` with this failure group's `signature` (error signature hash).

If many tests share the same error signature, this likely indicates an INFRASTRUCTURE issue — not individual test failures.

## Step 3: Check Existing Classifications (MANDATORY for EVERY test)

Call `get_classification_history` with `test_name`.

If already classified, reference the existing classification and explain if your assessment agrees or differs.

### User Override Protection (STRICT — NEVER VIOLATE)

When checking existing classifications, look at the `created_by` field:
- If `created_by` is a **username** (not `"rootcoz-ai"`), this is a **user classification** — a deliberate human decision.
- **You MUST NOT override user classifications.** Do not call `classify_test_pattern` for tests that have a user classification.
- If you disagree with a user's classification, note your disagreement in the analysis details only — the user's classification stands.
- If a user has classified a test, do NOT mark that test as reviewed — leave it for the user to review.
- Only override classifications where `created_by` is `"rootcoz-ai"` (from a previous AI analysis).

## Step 4: Check Job Statistics (MANDATORY — once per job)

Call `get_job_history_stats` with `job_name` once to understand overall job health.

## Step 5: Classify EVERY Test's Pattern (MANDATORY for EVERY test — NO EXCEPTIONS)

After completing your analysis, you MUST call `classify_test_pattern` for EVERY test you analyzed. This is NOT optional. Every test gets a pattern classification.

**EXCEPTION — User Override Protection:** If Step 3 revealed that a **user** (not AI) has already classified this test, **skip** `classify_test_pattern` for that test. User classifications are sacrosanct — never override them. You may note your assessment in the analysis output, but do not write a competing classification.

**IMPORTANT:** This step sets the **pattern** axis (how the failure manifests), NOT the root cause axis (classification). The root cause (CODE ISSUE / PRODUCT BUG / INFRASTRUCTURE) was already determined in the initial analysis. Do NOT change the root cause here — only determine the pattern.

Pass:
- `test_name`, `job_name`
- `classification` — one of the valid patterns below
- `reason` — evidence from history tools
- `references` — optional Jira keys / URLs

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

KNOWN_BUG can ONLY be used when the history tools provide concrete evidence:
- A Jira ticket key found in historical comments (from `get_failure_history`)
- A prior KNOWN_BUG classification with a Jira reference (from `get_classification_history`)

You MUST NOT classify as KNOWN_BUG based on:
- Your own training knowledge about product defects
- Pattern recognition from the error message alone
- Similarity to other failures in the SAME job run

If the history tools return no bug references, use REGRESSION, PERSISTENT, or INTERMITTENT instead.

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
| NEW | First failure — no prior occurrences in `get_failure_history` |
| KNOWN_BUG | ONLY if history tools returned a matching Jira ticket key from historical comments, or a prior KNOWN_BUG classification with a Jira reference. Your own knowledge about product defects does NOT count. If `get_failure_history` and `get_classification_history` return no Jira tickets or bug references, you CANNOT use KNOWN_BUG. Use REGRESSION or PERSISTENT instead. |
| REGRESSION | The date/build when the test started failing, what was passing before, correlation with recent repo changes if available |
| FLAKY | Failure rate statistics, specific builds where it passed vs failed |
| INTERMITTENT | The trigger pattern, frequency, and conditions under which it occurs vs doesn't |
| PERSISTENT | Consistently failing across many consecutive runs — cite consecutive_failures count from `get_failure_history` |

A pattern classification without evidence is INVALID. Always cite:
- Specific data from `get_failure_history` (failure rates, consecutive failures, dates)
- Jira tickets or bug URLs from historical comments
- Error signatures shared across tests (from `search_error_signature`)
- Previous classifications and their reasons

## Rules

- ALWAYS complete Steps 1-3 and Step 5 for EVERY test. Step 4 is required once per job (not per test). No shortcuts.
- ALWAYS check history BEFORE classifying — don't classify blind.
- ALWAYS call `classify_test_pattern` — this is how your classification is recorded. Include `references` with Jira keys, URLs, or other evidence identifiers.
- If many tests fail with the same infrastructure error (e.g., product not deployed), their pattern is likely PERSISTENT (consistently failing).
- Reference existing comments, bugs, and history in your analysis.
- Your reason field should cite specific data from the history (failure rates, consecutive failures, first seen dates).

## Additional Resources

If a test repository is available in the workspace, explore it with `read`, `ls`, `find`, and `grep` (no bash). Check for `.rootcoz/ROOTCOZ_PROMPT.md` and follow its instructions.
