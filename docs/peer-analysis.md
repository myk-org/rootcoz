# Using Multi-AI Peer Analysis

Improve failure classification accuracy by having multiple AI providers independently review each analysis and debate until they reach consensus. This is especially useful for ambiguous failures where a single AI might misclassify.

## Prerequisites

- A running rootcoz server with at least one AI provider configured (see [Quickstart](quickstart.html))
- Access to two or more AI providers (any combination of `claude`, `gemini`, and `cursor`)
- **Operator** or **admin** role to submit analyses (see [Managing Users and Roles](managing-users.html))

## Quick Example

Add the `--peers` flag to any analysis command:

```bash
rootcoz analyze my-job --build-number 42 \
  --peers "gemini:gemini-2.5-pro,cursor:gpt-5.4-xhigh"
```

This tells rootcoz to have Gemini and Cursor independently review the primary AI's analysis and debate until they agree on a classification.

## How Peer Analysis Works

Peer analysis adds a structured debate loop on top of the standard single-AI analysis:

1. **Primary AI analyzes** — The main AI provider (set via `--provider`/`--model` or server defaults) performs the initial failure analysis, identical to a standard single-AI run.
2. **Peers review in parallel** — Each peer AI receives the primary AI's classification and reasoning, then independently agrees or disagrees, providing its own classification and justification.
3. **Consensus check** — If all peers agree with the primary AI's classification, consensus is reached and the analysis is finalized.
4. **Primary AI revises** — If peers disagree, their feedback is sent back to the primary AI, which may revise its analysis. From round 2 onwards, each peer also sees the other peers' responses from the previous round.
5. **Repeat or finalize** — Steps 2–4 repeat until consensus is reached or the maximum number of rounds is exhausted.

> **Note:** Peers are explicitly instructed to be critical and independent — not sycophantic. The prompts include anti-sycophancy framing to encourage genuine disagreement when warranted.

## Step-by-Step: Enabling Peer Analysis

### 1. Choose Your Peer Configuration

Each peer is specified as a `provider:model` pair. Valid providers are `claude`, `gemini`, and `cursor`. Separate multiple peers with commas:

```
gemini:gemini-2.5-pro,cursor:gpt-5.4-xhigh
claude:claude-sonnet-4-20250514,gemini:gemini-2.5-pro
```

> **Tip:** For best results, use peers from different providers. Diversity in AI models increases the chance of catching misclassifications.

### 2. Run an Analysis with Peers

**Via CLI:**

```bash
rootcoz analyze my-job --build-number 42 \
  --provider claude --model claude-sonnet-4-20250514 \
  --peers "gemini:gemini-2.5-pro,cursor:gpt-5.4-xhigh"
```

**Via environment variable (server-wide default):**

```bash
export PEER_AI_CONFIGS="gemini:gemini-2.5-pro,cursor:gpt-5.4-xhigh"
```

When set as an environment variable, every analysis automatically uses peer review unless explicitly overridden per-request.

### 3. View the Debate Results

After analysis completes, the report page shows a **Peer Analysis** section for each failure group:

- **Consensus / No Consensus** badge — whether the AIs agreed
- **Rounds used** — how many debate rounds occurred (e.g., "2 of 3 rounds")
- **Per-round timeline** — expand to see each participant's classification, reasoning, and whether they agreed with the orchestrator

A summary at the top of the report page shows consensus status across all failure groups and which AI models participated.

See [Reviewing Analysis Results](reviewing-results.html) for more on navigating the report page.

## Configuration Options

### Setting Peer Configs

Peers can be configured at three levels, with this priority order:

| Level | How to Set | Scope |
|-------|-----------|-------|
| **Per-request** (highest) | `--peers` CLI flag or `peer_ai_configs` in request body | Single analysis |
| **Config file** | `peers` field in `~/.config/rootcoz/config.toml` | All analyses for that server |
| **Environment variable** (lowest) | `PEER_AI_CONFIGS` on the server | All analyses server-wide |

**CLI config file example** (`~/.config/rootcoz/config.toml`):

```toml
[servers.production]
url = "https://rootcoz.example.com"
peers = "gemini:gemini-2.5-pro,cursor:gpt-5.4-xhigh"
peer_analysis_max_rounds = 5
```

See [Setting Up the CLI](cli-setup.html) for the full config file reference.

### Disabling Peers for a Single Request

When peers are configured server-wide but you want to skip peer review for one analysis:

```bash
rootcoz analyze my-job --build-number 42 --peers ""
```

Sending an empty peers value disables peer analysis for that request only.

### Controlling Debate Rounds

The `--peer-analysis-max-rounds` flag controls how many rounds of debate are allowed before the analysis is finalized:

```bash
rootcoz analyze my-job --build-number 42 \
  --peers "gemini:gemini-2.5-pro" \
  --peer-analysis-max-rounds 5
```

| Setting | Default | Range | Description |
|---------|---------|-------|-------------|
| `--peer-analysis-max-rounds` | 3 | 1–10 | Maximum debate rounds before accepting the result |

The environment variable equivalent is `PEER_ANALYSIS_MAX_ROUNDS`.

> **Tip:** More rounds give AIs more chances to reach consensus, but increase analysis time and token usage. For most cases, the default of 3 rounds is sufficient.

### Controlling Parallelism

Peer AI calls run in parallel by default. The `MAX_CONCURRENT_AI_CALLS` environment variable (default: 3) limits how many peer calls execute simultaneously. You can also override this per-request with `--max-concurrent-ai-calls`.

## Advanced Usage

### Model Names with Parameters

Some providers support model parameters in bracket syntax. Commas inside brackets are handled correctly:

```bash
rootcoz analyze my-job --build-number 42 \
  --peers "cursor:gpt-5.4[context=272k,reasoning=medium],gemini:gemini-2.5-pro"
```

### Classification Values in the Debate

Peers classify failures using the same three categories as the primary AI:

- **CODE ISSUE** — The test itself is broken
- **PRODUCT BUG** — The product under test has a defect
- **INFRASTRUCTURE** — Environment or infrastructure problem

Agreement is determined by matching these classifications (case-insensitive). A peer that returns an invalid classification is excluded from consensus.

### What Happens When Consensus Fails

When the maximum number of rounds is exhausted without consensus:

- The **primary AI's latest analysis** is used as the final result.
- The debate trail is preserved in the results so you can review each AI's reasoning.
- If the primary AI returned an empty classification, rootcoz falls back to **peer consensus** — adopting the classification that the majority of peers agreed on. If there's no majority, the most frequent classification is used.

### Fallback Behavior

Peer analysis is designed to be resilient:

- If a peer AI call **fails** (timeout, error, unparseable response), that peer is excluded from the consensus check. The remaining peers continue.
- If **all peers fail** in a round, the primary AI's current analysis is used.
- If a **revision round fails**, the primary AI keeps its previous analysis and the debate continues.
- Peer failures never crash the overall analysis pipeline — you always get a result.

### Combining with Other Features

Peer analysis works with all analysis sources and features:

```bash
# With JUnit XML source
rootcoz analyze --source file --file results.xml \
  --peers "gemini:gemini-2.5-pro,claude:claude-sonnet-4-20250514"

# With additional repos for context
rootcoz analyze my-job --build-number 42 \
  --peers "gemini:gemini-2.5-pro" \
  --additional-repos "product:https://github.com/org/product"

# With custom prompt
rootcoz analyze my-job --build-number 42 \
  --peers "cursor:gpt-5.4-xhigh,gemini:gemini-2.5-pro" \
  --raw-prompt "Focus on network-related failures"
```

See [Analyzing Test Failures](analyzing-failures.html) for details on analysis sources and options.

### Re-Analyzing with Different Peers

You can re-analyze a completed job with different peer configurations from the web UI or CLI. The new peer debate replaces the previous analysis for the re-analyzed failure groups.

See [Reviewing Analysis Results](reviewing-results.html) for how to trigger re-analysis.

## Troubleshooting

**"Unsupported provider" error when setting peers**
Valid providers are `claude`, `gemini`, and `cursor`. Check for typos in your `--peers` value.

**Consensus never reached (always uses max rounds)**
This typically means the failure is genuinely ambiguous. Consider:
- Providing more context with `--additional-repos` or `--raw-prompt`
- Reducing max rounds if you're comfortable with majority-vote fallback
- Reviewing the debate trail in the UI to understand each AI's reasoning

**Peer analysis is slow**
Each round involves parallel AI calls plus a revision call. To speed things up:
- Reduce `--peer-analysis-max-rounds` (e.g., to 1 or 2)
- Use fewer peers
- Increase `MAX_CONCURRENT_AI_CALLS` if your providers support higher concurrency

**Token usage is higher than expected**
Peer analysis multiplies token usage — each round includes one call per peer plus a potential revision call by the primary AI. Monitor token usage on the admin dashboard. See [Environment Variables and Configuration](environment-variables.html) for token usage settings.

## Related Pages

- [Analyzing Test Failures](analyzing-failures.html)
- [Reviewing Analysis Results](reviewing-results.html)
- [Environment Variables and Configuration](environment-variables.html)
- [CLI Command Reference](cli-reference.html)
- [Configuring Integrations](configuring-integrations.html)