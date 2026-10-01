# Configuring Analysis Context

RootCoz's analysis quality comes from what you give it: the repository that owns the failing tests, any additional repositories the failure touches, the AI models that review each other, the tracker it searches for duplicates, and the build artifacts it reads. This page covers those inputs, plus the per-project `.rootcoz/` files a repository can ship to teach RootCoz about itself.

## Prerequisites

- You can submit analyses, so your role is `operator` or `admin`.
- A reachable clone URL for the tests repository. Only `https://` and `git://` URLs are accepted.
- For a private tests repository, a token with read access to it.
- For Jira search, a Jira URL, project key, and credentials that can read the project.
- For per-project customization, write access to the repository that holds the `.rootcoz/` files.

## Quick Example

In `New Analysis`, fill the `Source Repositories` section:

| Field | Value |
| --- | --- |
| `Tests Repo URL` | `https://github.com/org/my-tests` |
| `Ref / Branch` | `main` |
| `Additional Repositories` | Two rows, one per repository: `Name` `api` with `URL` `https://github.com/org/api`, and `Name` `web` with `URL` `https://github.com/org/web` |

`Additional Repositories` is a list of inputs, not a single string: click `Add Repository` once per extra repository and fill the `Name`, `URL`, and optional `Ref` fields on that row. The comma-separated `name:url` spelling belongs to the CLI `--additional-repos` flag and the `additional_repos` server setting, not to this form.

This clones the tests repository plus both extra repositories into the analysis workspace and tells the AI that all three are available to read.

## Step-by-Step

1. **Point RootCoz at the tests repository.**

   `Tests Repo URL` is the repository that owns the failing tests — usually a test repository rather than the product repository. `Ref / Branch` pins the checkout; leave it empty to use the remote default branch. RootCoz clones with a shallow history and exposes the clone to the AI as a readable resource.

   The field is per submission. Setting it once on the server (`tests_repo_url` in the `GitHub` category of Server Settings) makes it the default for every later submission.

   | Detail | Behaviour |
   | --- | --- |
   | URL schemes | `https://` and `git://` only |
   | Clone directory | Derived from the repository name; it must not collide with an additional repository name |
   | Files it unlocks | Any `.rootcoz/` files in that repository, which are loaded after the clone completes |

2. **Authenticate with the tests repository.**

   Private repositories need a token. There are two ways to supply one:

   | Source | Field | Notes |
   | --- | --- | --- |
   | Per submission | `tests_repo_token` on the request | Encrypted at rest with the rest of the job parameters |
   | Server default | `tests_repo_token` in the `GitHub` server settings category | A database override wins over the environment variable |

   Additional repositories use their own per-repository token, set in the request, not in `.rootcoz/settings.json`.

   > **Note:** A token given in the request takes priority over the server setting for that job. Leave both empty for public repositories.

3. **Add repositories the failure actually touches.**

   `Additional Repositories` accepts a list of `name`, `url`, and optional `ref` entries. Each clone becomes a subdirectory of the workspace, and the AI is told the path and what is in it — for example that a repository contains VCS metadata it can browse.

   | Rule | Behaviour |
   | --- | --- |
   | `name` | Becomes the clone directory name. No slashes, no `..`, no leading dot. |
   | Reserved name | `build-artifacts` is reserved for downloaded artifacts and is rejected. |
   | Collision | A name matching the tests repository clone directory fails the job. |
   | `ref` | Branch, tag, or commit. Empty means the remote default branch. |

   The server-side equivalent is the `additional_repos` setting, stored as `name:url` or `name:url:ref` separated by commas. `max_concurrent_repo_clones` caps how many clones run at once.

4. **Add peer AI configs for cross-model review.**

   Turn on `Enable peer review` in the `Peer Analysis` section and add one entry per peer model. The main model analyzes first, the peers review in parallel, and the loop continues until they agree or the round limit is reached. Nobody has veto power — it is a debate that ends in a consensus answer.

   | Field | Server setting | Default |
   | --- | --- | --- |
   | Peer list | `peer_ai_configs`, stored as `provider:model,provider:model` | empty (no peers) |
   | Debate rounds | `peer_analysis_max_rounds`, between 1 and 10 | 3 |

   `claude`, `gemini`, and `cursor` are convenience aliases, not the only accepted values — as with `AI_PROVIDER`, any exact provider ID that pi-sidecar reports is accepted, and the chart's restricted `ai.provider` enum does not apply here. Peer models cost real tokens and real wall-clock time, so start with one peer and a small round limit.

   > **Tip:** Peer models need credentials. Either they use your own stored key or the server's, depending on the `Use server credentials` permission you were granted.

5. **Turn on Jira search for duplicate bugs.**

   Enable `Enable Jira search` in the `Jira Integration` section and supply `Jira URL` and `Project Key`. While analyzing, RootCoz asks the model for three to five short `jira_search_keywords` for each `PRODUCT BUG` instead of generic words, and uses them to find existing bugs before creating a new one. The same pattern produces `tests_repo_search_keywords` for matching GitHub issues.

   | Server setting | What it controls |
   | --- | --- |
   | `enable_jira` | Whether the search runs |
   | `jira_url` | Base URL of the Jira instance |
   | `jira_project_key` | Project to search in |
   | `jira_max_results` | Maximum matches returned per search; default 5 |
   | `enable_jira_issues` | Whether new Jira bugs can be created, independently of search |

   Search is enrichment only. Creating issues still requires working credentials, and a per-user `Jira Email` and `Jira Token` lets created issues land under your name. See [Managing Your Account and Notifications](manage-account-and-notifications.html) for details.

6. **Collect build artifacts when text output is not enough.**

   For a Jenkins run, `Fetch build artifacts` downloads the build's artifacts into a `build-artifacts/` directory in the workspace, subject to `Max Size (MB)` (`jenkins_artifacts_max_size_mb`, default 500). For a Prow run the same toggle downloads the non-JUnit artifacts from GCS.

   The prompt tells the AI that `build-artifacts/` exists, and every classification it produces carries an `artifacts_evidence` field with file references in the form `[build-artifacts/logs/app.log]: ...`. That is how screenshots and log files become evidence in the report instead of guesswork from the JUnit XML alone.

   | Setting | Default | Notes |
   | --- | --- | --- |
   | `get_job_artifacts` | on | Runtime toggle per submission or server-wide |
   | `jenkins_artifacts_max_size_mb` | 500 | Size cap for the download |

   Turn artifact collection off for large builds where the console output already tells the whole story — it is the slowest part of setting up a job.

7. **Let the repository configure itself with `.rootcoz/`.**

   Any cloned repository can carry a `.rootcoz/` directory that changes how it is analyzed:

   ```text
   <analyzed-repo>/
     .rootcoz/
       settings.json                  # Per-repo analysis settings
       ROOTCOZ_PROMPT.md              # Custom analysis instructions
       ROOTCOZ_HISTORY_PROMPT.md      # Custom history analysis instructions
       ROOTCOZ_ISSUE_PROMPT.md        # Custom issue generation prompt
       agents/                        # Project pi agents
       skills/                        # Project pi skills
       extensions/                    # Project pi extensions
   ```

   RootCoz discovers this after the tests repository is cloned. Only files under `.rootcoz/` are recognized; the old prompt filenames in the repository root are no longer supported.

8. **Use `.rootcoz/settings.json` for per-repo defaults.**

   This file is the only schema-validated file in the directory. Every key is optional, unknown keys are rejected, and secrets are not allowed.

   ```json
   {
     "ai_provider": "claude",
     "ai_model": "claude-sonnet-4",
     "ai_call_timeout": 15,
     "max_concurrent_ai_calls": 4,
     "peer_ai_configs": [
       { "ai_provider": "gemini", "ai_model": "gemini-2.5-pro" }
     ],
     "peer_analysis_max_rounds": 2,
     "force_server_credentials": true,
     "additional_repos": [
       { "name": "api", "url": "https://github.com/org/api", "ref": "main" }
     ]
   }
   ```

   | Key | Type | Purpose |
   | --- | --- | --- |
   | `ai_provider` | string | Provider for this repository's analyses |
   | `ai_model` | string | Model for this repository's analyses |
   | `ai_call_timeout` | integer > 0 | Per-call timeout, in minutes |
   | `max_concurrent_ai_calls` | integer > 0 | Parallel AI calls |
   | `peer_ai_configs` | array of `{ai_provider, ai_model}` | Peer reviewers |
   | `peer_analysis_max_rounds` | integer 1–10 | Debate round limit |
   | `force_server_credentials` | boolean | Use server keys instead of personal keys |
   | `additional_repos` | array of `{name, url, ref}` | Repositories to clone alongside the tests repo |

   Resolution order for every one of these keys is **request → `.rootcoz/settings.json` → server settings**. Because the file is read from a repository you do not control, loading it is deliberately strict:

   | Constraint | Behaviour |
   | --- | --- |
   | Schema | Validated against `src/rootcoz/schemas/rootcoz-settings.schema.json` |
   | Unknown or secret keys | Rejected. A `token` key anywhere in the file fails validation. |
   | Size | 256 KiB maximum |
   | Path | Must be a regular file inside the repository. Symlinks and paths escaping the repo are rejected. |
   | Invalid file | The job fails with a validation error rather than silently ignoring the file. |

   > **Warning:** Do not put credentials in this file. It is read from an untrusted clone and is not encrypted anywhere in the pipeline. Use per-repository tokens in the request or server settings instead.

9. **Write the prompt files for project-specific instructions.**

   | File | Applies to | How it is loaded |
   | --- | --- | --- |
   | `ROOTCOZ_PROMPT.md` | Failure analysis | Discovered in each cloned repository; the prompt advertises the path and the AI is instructed to open it before analyzing. |
   | `ROOTCOZ_HISTORY_PROMPT.md` | History-aware classification | Only advertised when history-aware classification is active — the job has a server URL and a bearer token. |
   | `ROOTCOZ_ISSUE_PROMPT.md` | Issue and bug generation | Fetched from the tests repository through the GitHub Contents API, so a tests repo token is needed for private repositories. |

   The prompt body is never inlined into the analysis prompt. RootCoz tells the AI that the file exists and requires it to read the file with its `read` tool, which keeps prompts small and lets you iterate on instructions without touching RootCoz.

   > **Note:** `ROOTCOZ_ISSUE_PROMPT.md` is only consulted when the job does not already carry an issue prompt from the request.

10. **Ship project pi resources.**

    After cloning, `agents/`, `skills/`, and `extensions/` are copied from `.rootcoz/` into the workspace's `.pi/` directory so the agent runtime discovers them for analysis, re-analysis, and chat. Project agents are listed to the AI in a first-step gate and named in the resources section. Built-in agents are copied afterwards and never overwrite a project agent with the same name.

    | Directory | Purpose |
    | --- | --- |
    | `agents/` | Project agents whose files the AI is pointed at for analysis guidance |
    | `skills/` | Project skills the agent runtime loads |
    | `extensions/` | Project extensions the agent runtime loads |

    Symlinks are skipped rather than followed. However, a `settings.json` that exists but fails validation is not merely logged — the analysis stops and the job is marked failed with the reason redacted. Treat a malformed `.rootcoz/settings.json` as a job-failing condition, not a warning.

    > **Warning:** Anything you add under `.rootcoz/` ships with your repository and steers AI behavior for everyone who analyzes it. Treat it as reviewed code, and document every new file you add — the repository's `AGENTS.md` requires that each repo-side customization file is described alongside the code that reads it.

## Advanced Usage

- **Per-request raw prompt.** `Raw Prompt` on `New Analysis` is appended to the analysis prompt for that one submission. Use it for one-off nudges; use `ROOTCOZ_PROMPT.md` for instructions that should apply every time.
- **Tune timeouts and concurrency.** `ai_call_timeout` (minutes, default 10) and `max_concurrent_ai_calls` (default 3) are the two knobs most worth raising for large, slow analyses. Both can be set per submission, in `.rootcoz/settings.json`, or server-wide.
- **Defer the AI choice.** When a tests repository is set and no provider or model is chosen, RootCoz can resolve the AI configuration from `.rootcoz/settings.json` after the clone, so the repository picks its own model.
- **Repository-wide defaults.** `tests_repo_url`, `additional_repos`, `peer_ai_configs`, `peer_analysis_max_rounds`, `ai_call_timeout`, and `max_concurrent_ai_calls` all exist as server settings in the `AI`, `GitHub`, and `Server` categories, which makes them defaults for every submission. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.
- **Metadata rules.** `metadata_rules_file` extracts team, tier, and version metadata from job names so history and reports can filter by them. It requires a restart when changed.
- **Auto-review.** `enable_auto_review` marks a failure reviewed automatically when the same job, test, and error signature was reviewed by a human before — useful once you have history to match against.

## Troubleshooting

- The AI cannot find the test source.  
  The tests repository is not set for this submission, or the clone failed. Check `Tests Repo URL` on the submission and the clone progress on the status page.

- The analysis does not mention `.rootcoz/ROOTCOZ_PROMPT.md`.  
  The file lives in the repository root, not in `.rootcoz/`, or it is in a repository that was not cloned. Only `.rootcoz/` is recognized.

- `settings.json` is ignored.  
  It is only read from the tests repository, after that repository is cloned. A `settings.json` in an additional repository is not read.

- The job fails with `Unknown keys in .rootcoz/settings.json`.  
  Only the eight keys in the table above are allowed. Remove anything else — including secrets like `token`.

- The job fails with `Invalid additional_repos url`.  
  The URL must be a valid HTTP(S) URL — `additional_repos` entries are validated as `HttpUrl`, so `git://` is rejected — and the entry must have a non-empty `name`.

- The job fails with `additional_repos contains name '…' which collides with the tests repo clone directory`.  
  Rename the additional repository so it differs from the tests repository's clone directory name.

- Peer review never converges and burns tokens.  
  Lower `peer_analysis_max_rounds` or reduce the number of peers. Ten rounds across several models is the most expensive configuration.

- Jira or GitHub search returns nothing.  
  Check that search is enabled, that the credentials can read the target, and that the generated keywords are specific. The model is told to avoid generic terms like `timeout` or `failure`; keyword quality is what narrows the results.

- A private repository fails to clone.  
  Supply `tests_repo_token`, or a per-repository token for an additional repository. Embedded credentials in the clone URL are not used.

- Artifacts are missing from the report.  
  Artifact collection was off, the size cap was hit, or the artifacts live outside what the job uploaded. `build-artifacts/` only contains what the CI system published.

## Related Pages

- [Submitting Analyses](submit-analyses.html)
- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)
- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [Configuration Reference](configuration-reference.html)