# Quickstart

You want RootCoz running fast so you can move from failing CI output to a report you can review and act on. The quickest path is to start the bundled Docker stack, create an operator account, and submit one Jenkins, Prow, or JUnit XML analysis from the web UI.

## Prerequisites

- Docker with Docker Compose
- One AI credential for `claude`, `gemini`, or `cursor`
- For Jenkins: a job name, build number, and Jenkins access
- For Prow: a job name, build ID, and access to the relevant Prow/GCS location
- For JUnit XML: a `.xml` file or raw JUnit XML text

## Quick Example

```bash
cp .env.example .env
```

```dotenv
AI_PROVIDER=claude
AI_MODEL=your-model-name
ANTHROPIC_API_KEY=your-anthropic-api-key
DEFAULT_USER_ROLE=operator
REQUIRE_APPROVAL=false
```

```bash
docker compose up -d
curl http://localhost:800/health
```

1. Open `http://localhost:800`.
2. Click `Register`, enter a username, save the API key RootCoz shows once, then click `I've saved my key — Continue`.
3. Open `New Analysis`.
4. Choose `Upload File` or `Paste XML` for the fastest first run, or switch to `Jenkins Job` or `Prow Job`.
5. Click `Submit Analysis`.

XML submissions open the result directly. Jenkins and Prow submissions open a live status page first, then redirect to the report when analysis completes.

> **Note:** The Docker image already includes the supported AI CLIs. For a first run, you only need one working provider credential plus `AI_PROVIDER` and `AI_MODEL`.


> **Warning:** `DEFAULT_USER_ROLE=operator` and `REQUIRE_APPROVAL=false` are convenient for a local trial. For a shared deployment, keep approval enabled and manage roles deliberately. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.

## Step-by-Step

1. Prepare `.env`.

   Start from `.env.example`, then add one AI provider, model, and the two quickstart lines shown above.

   | If your first analysis is... | Add before startup | You can also enter it in `New Analysis` |
   | --- | --- | --- |
   | JUnit XML | nothing else | n/a |
   | Jenkins | `JENKINS_URL`, `JENKINS_USER`, `JENKINS_PASSWORD` | yes |
   | Prow | `PROW_URL`, `GCS_BUCKET` | yes |

   | AI provider | Credential variable |
   | --- | --- |
   | Claude | `ANTHROPIC_API_KEY` |
   | Gemini | `GEMINI_API_KEY` |
   | Cursor | `CURSOR_API_KEY` |

   > **Warning:** The bundled Compose file uses example Jenkins values when you do not override them. Replace those placeholders before you submit a Jenkins run, or type the correct values into the form for that specific submission.

2. Start RootCoz.

   ```bash
   docker compose up -d
   curl http://localhost:800/health
   ```

   The web UI and API are both served from `http://localhost:800`. If the health check succeeds, the service is up and ready.

3. Sign in with a user that can submit analyses.

   With the quickstart config above, click `Register`, choose a username, save the API key, and continue into the app. RootCoz creates an active `operator` session immediately after registration.

   > **Tip:** If you already have an operator or admin API key, use `Log in` instead of `Register`.


   > **Tip:** Leave `GitHub Token`, `Jira Email`, and `Jira Token` blank for your first run. Add them later when you are ready to create issues or link external trackers. See [Managing Your Account and Notifications](manage-account-and-notifications.html) for details.

4. Open `New Analysis` and choose the input mode that matches what you already have.

   | Choose in the UI | Required fields | Optional first-run fields | What opens after submit |
   | --- | --- | --- | --- |
   | `Upload File` | a `.xml` file | `AI Provider`, `AI Model`, tags | Result page |
   | `Paste XML` | JUnit XML content | `AI Provider`, `AI Model`, tags | Result page |
   | `Jenkins Job` | `Job Name`, `Build Number` | `Jenkins URL`, `Jenkins User`, `Jenkins Password / Token`, `Wait for build completion` | Status page, then result |
   | `Prow Job` | `Job Name`, numeric `Build ID` | `Prow URL`, `GCS Bucket`, `GCS Prefix` | Status page, then result |

   Leave `GCS Prefix` empty unless you want to override auto-detection. For Jenkins, `Wait for build completion` is enabled by default.

   > **Tip:** `Upload File` and `Paste XML` are the fastest first-run options because they do not depend on Jenkins or Prow connectivity.

5. Submit and review the first result.

   Click `Submit Analysis`. RootCoz keeps the job on the dashboard so you can reopen it later without resubmitting.

   See [Submitting Analyses](submit-analyses.html) for details. See [Reviewing and Classifying Failures](review-and-classify-failures.html) for the next workflow. See [Tracking Analysis Progress](track-analysis-progress.html) for the live status view.

## Advanced Usage

- If you want admin login on day one, set `ADMIN_KEY` before startup, then log in as `admin` with that key.
- If you want to keep approval enabled, set `REQUIRE_APPROVAL=true`, log in as `admin`, and approve or create an `operator` before submitting jobs. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.
- If you want self-registered users to submit analyses without admin role changes, keep `DEFAULT_USER_ROLE=operator` before you start the stack.
- If you want richer analysis on the first run, add a tests repository, extra repositories, peer review models, or Jira search settings from the form. See [Configuring Analysis Context](configure-analysis-context.html) for details.
- If you prefer terminal automation once the server is running, see [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html) and [CLI Command Reference](cli-reference.html).
- If you need a production-style installation instead of the local Docker stack, see [Deploying RootCoz](deploy-rootcoz.html) for details.

## Troubleshooting

- If `docker compose up -d` fails with `AI_PROVIDER is required` or `AI_MODEL is required`, add both values to `.env` and start again.
- If you can log in but do not see `New Analysis`, your account is not `operator` or `admin`. Set `DEFAULT_USER_ROLE=operator` for a local trial or ask an admin to change your role.
- If registration succeeds but RootCoz says your account is awaiting approval, either disable approval for the local quickstart with `REQUIRE_APPROVAL=false` or have an admin approve the account.
- If a Jenkins submission fails immediately, verify `JENKINS_URL`, `JENKINS_USER`, and `JENKINS_PASSWORD`, or enter them directly on the form for that run.
- If a Prow submission fails immediately, provide `Prow URL` and `GCS Bucket` in the form or set `PROW_URL` and `GCS_BUCKET` in `.env` before restarting.
- If XML submission finishes with no useful report, make sure you pasted or uploaded real JUnit XML, not console output or HTML. RootCoz only analyzes failures and errors from the XML.# Quickstart

You want RootCoz running fast so you can move from failing CI output to a report you can review and act on. The quickest path is to start the bundled Docker stack, create an operator account, and submit one Jenkins, Prow, or JUnit XML analysis from the web UI.

## Prerequisites

- Docker with Docker Compose
- One AI credential for `claude`, `gemini`, or `cursor`
- For Jenkins: a job name, build number, and Jenkins access
- For Prow: a job name, build ID, and access to the relevant Prow/GCS location
- For JUnit XML: a `.xml` file or raw JUnit XML text

## Quick Example

```bash
cp .env.example .env
```

```dotenv
AI_PROVIDER=claude
AI_MODEL=your-model-name
ANTHROPIC_API_KEY=your-anthropic-api-key
DEFAULT_USER_ROLE=operator
REQUIRE_APPROVAL=false
```

```bash
docker compose up -d
curl http://localhost:8000/health
```

1. Open `http://localhost:8000`.
2. Click `Register`, enter a username, save the API key RootCoz shows once, then click `I've saved my key — Continue`.
3. Open `New Analysis`.
4. Choose `Upload File` or `Paste XML` for the fastest first run, or switch to `Jenkins Job` or `Prow Job`.
5. Click `Submit Analysis`.

XML submissions open the result directly. Jenkins and Prow submissions open a live status page first, then redirect to the report when analysis completes.

> **Note:** The Docker image already includes the supported AI CLIs. For a first run, you only need one working provider credential plus `AI_PROVIDER` and `AI_MODEL`.


> **Warning:** `DEFAULT_USER_ROLE=operator` and `REQUIRE_APPROVAL=false` are convenient for a local trial. For a shared deployment, keep approval enabled and manage roles deliberately. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.

## Step-by-Step

1. Prepare `.env`.

   Start from `.env.example`, then add one AI provider, model, and the two quickstart lines shown above.

   | If your first analysis is... | Add before startup | You can also enter it in `New Analysis` |
   | --- | --- | --- |
   | JUnit XML | nothing else | n/a |
   | Jenkins | `JENKINS_URL`, `JENKINS_USER`, `JENKINS_PASSWORD` | yes |
   | Prow | `PROW_URL`, `GCS_BUCKET` | yes |

   | AI provider | Credential variable |
   | --- | --- |
   | Claude | `ANTHROPIC_API_KEY` |
   | Gemini | `GEMINI_API_KEY` |
   | Cursor | `CURSOR_API_KEY` |

   > **Warning:** The bundled Compose file uses example Jenkins values when you do not override them. Replace those placeholders before you submit a Jenkins run, or type the correct values into the form for that specific submission.

2. Start RootCoz.

   ```bash
   docker compose up -d
   curl http://localhost:8000/health
   ```

   The web UI and API are both served from `http://localhost:8000`. If the health check succeeds, the service is up and ready.

3. Sign in with a user that can submit analyses.

   With the quickstart config above, click `Register`, choose a username, save the API key, and continue into the app. RootCoz creates an active `operator` session immediately after registration.

   > **Tip:** If you already have an operator or admin API key, use `Log in` instead of `Register`.


   > **Tip:** Leave `GitHub Token`, `Jira Email`, and `Jira Token` blank for your first run. Add them later when you are ready to create issues or link external trackers. See [Managing Your Account and Notifications](manage-account-and-notifications.html) for details.

4. Open `New Analysis` and choose the input mode that matches what you already have.

   | Choose in the UI | Required fields | Optional first-run fields | What opens after submit |
   | --- | --- | --- | --- |
   | `Upload File` | a `.xml` file | `AI Provider`, `AI Model`, tags | Result page |
   | `Paste XML` | JUnit XML content | `AI Provider`, `AI Model`, tags | Result page |
   | `Jenkins Job` | `Job Name`, `Build Number` | `Jenkins URL`, `Jenkins User`, `Jenkins Password / Token`, `Wait for build completion` | Status page, then result |
   | `Prow Job` | `Job Name`, numeric `Build ID` | `Prow URL`, `GCS Bucket`, `GCS Prefix` | Status page, then result |

   Leave `GCS Prefix` empty unless you want to override auto-detection. For Jenkins, `Wait for build completion` is enabled by default.

   > **Tip:** `Upload File` and `Paste XML` are the fastest first-run options because they do not depend on Jenkins or Prow connectivity.

5. Submit and review the first result.

   Click `Submit Analysis`. RootCoz keeps the job on the dashboard so you can reopen it later without resubmitting.

   See [Submitting Analyses](submit-analyses.html) for details. See [Reviewing and Classifying Failures](review-and-classify-failures.html) for the next workflow. See [Tracking Analysis Progress](track-analysis-progress.html) for the live status view.

## Advanced Usage

- If you want admin login on day one, set `ADMIN_KEY` before startup, then log in as `admin` with that key.
- If you want to keep approval enabled, set `REQUIRE_APPROVAL=true`, log in as `admin`, and approve or create an `operator` before submitting jobs. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.
- If you want self-registered users to submit analyses without admin role changes, keep `DEFAULT_USER_ROLE=operator` before you start the stack.
- If you want richer analysis on the first run, add a tests repository, extra repositories, peer review models, or Jira search settings from the form. See [Configuring Analysis Context](configure-analysis-context.html) for details.
- If you prefer terminal automation once the server is running, see [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html) and [CLI Command Reference](cli-reference.html).
- If you need a production-style installation instead of the local Docker stack, see [Deploying RootCoz](deploy-rootcoz.html) for details.

## Troubleshooting

- If `docker compose up -d` fails with `AI_PROVIDER is required` or `AI_MODEL is required`, add both values to `.env` and start again.
- If you can log in but do not see `New Analysis`, your account is not `operator` or `admin`. Set `DEFAULT_USER_ROLE=operator` for a local trial or ask an admin to change your role.
- If registration succeeds but RootCoz says your account is awaiting approval, either disable approval for the local quickstart with `REQUIRE_APPROVAL=false` or have an admin approve the account.
- If a Jenkins submission fails immediately, verify `JENKINS_URL`, `JENKINS_USER`, and `JENKINS_PASSWORD`, or enter them directly on the form for that run.
- If a Prow submission fails immediately, provide `Prow URL` and `GCS Bucket` in the form or set `PROW_URL` and `GCS_BUCKET` in `.env` before restarting.
- If XML submission finishes with no useful report, make sure you pasted or uploaded real JUnit XML, not console output or HTML. RootCoz only analyzes failures and errors from the XML.

## Related Pages

- [Deploying RootCoz](deploy-rootcoz.html)
- [Submitting Analyses](submit-analyses.html)
- [Configuring Analysis Context](configure-analysis-context.html)
- [Tracking Analysis Progress](track-analysis-progress.html)
- [Reviewing and Classifying Failures](review-and-classify-failures.html)