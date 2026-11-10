# Use Server Chat for Cross-Job Analysis

Ask RootCoz questions across all jobs when you need trends, summaries, or reusable reports without opening runs one by one. This guide shows administrators how to start a server-wide chat, switch models for one question, follow replies, and keep or clear the session on their own terms.

## Prerequisites

- An admin account in RootCoz. The `Chat` page in the admin sidebar is admin-only.
- A default AI provider and model configured on the server, or a provider and model you can choose before sending a message. See [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.
- For CLI use, the `rootcoz` CLI installed and authenticated with an admin API key. See [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html) for details.

## Quick Example

```bash
export ROOTCOZ_SERVER="https://rootcoz.example.com"
read -rsp "Admin API key: " ROOTCOZ_API_KEY
export ROOTCOZ_API_KEY
echo

rootcoz auth whoami
rootcoz admin-chat init --provider claude --model claude-opus-4-6
rootcoz admin-chat send "Summarize the top failure trends from the last 7 days."
```

This confirms the CLI is using an admin identity, then asks a server-wide question and waits for the assistant reply.

> **Note:** Server Chat is separate from chat on a single analysis result. For job-specific chat and comments, see [Collaborating on Results](collaborate-on-results.html) for details.

## Step-by-Step

1. Open Server Chat or confirm CLI admin access.

   In the web UI, open `Chat` from the admin section of the sidebar. The empty state prompts you to `Ask about server analytics`, which is the right place for cross-job questions such as failure trends, user activity, or report summaries.

   In the CLI, run this first when you want to confirm which admin account the shell is using:

   ```bash
   rootcoz auth whoami
   ```

2. Choose how RootCoz should pick the model.

   The header preselects the provider and model you last used on this account, so leaving it alone repeats your previous choice rather than the server default. To use a different pair, pick it — both a provider and a model are always required, because chat cannot start on an empty pair. A model the sidecar cannot open a session with is marked `NO SESSION` in the list and cannot be picked; the reason says to declare it in the provider's `models.json`. A session that is already running keeps the pair it was started with, so its reply is always attributed to what it actually used.

   | If you want | Web UI | CLI |
   | --- | --- | --- |
   | Use the pair already selected | It is loaded in the header — leave it. This is the pair you used last, not necessarily the server default. | Omit `--provider` and `--model`. |
   | Override for one question | Pick a different provider and model in the header before sending. | Add `--provider` and `--model` to `rootcoz admin-chat send`. |
   | Keep using the same override | Leave the header selection in place. | Repeat the same flags on the next `send` command. |

   If you need to change the shared defaults for everyone, see [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.

3. Ask a cross-job question and follow the reply.

   In the web UI, type your question and press `Enter` to send it. Use `Shift+Enter` when you want a newline instead. While RootCoz is working, the answer streams into the bubble as it is written and the thread keeps `Thinking...` with the elapsed time plus a `Stop` button; the stored reply is the final text (a reload shows it whole) and it stays in the same conversation.

   In the CLI, send the question directly:

   ```bash
   rootcoz admin-chat send "Show where humans overrode the AI most often this month."
   ```

   The CLI waits for the assistant reply when it can. If you want to come back later, or the response takes longer than expected, check the stored conversation:

   ```bash
   rootcoz admin-chat history --limit 20
   ```

   > **Tip:** Use the web UI when you want the clearest live experience. It shows the busy state from the moment you send, tells you how long the turn has been running, and gives you a `Stop` button while the assistant is still working.

   > **From an answer to a ticket:** ask the assistant to file it — "create a GitHub issue for this" or "open a Jira ticket" — and it drafts the title and body from the conversation. The server posts the preview to the chat itself, and nothing is filed until you reply there with the six-character code it shows. Server Chat itself resolves no tracker credential — the issue is filed with the token saved in *your* account, so add one in Profile Settings first; the server itself then posts the line naming who must act when the cause is its own configuration (an administrator, in Server Settings) rather than the account's. A `400` naming the server's own Jira credential is relayed verbatim too, so a token that is really the deployment's is answered with what to change. The same gate from the CLI, in the same two steps and with no token flags (the proposal files nothing and prints the code to confirm with):

   ```bash
   rootcoz propose-chat-issue --target jira \
     --title "Dashboard filters drop the team" --body "$(cat ticket-body.md)"
   # review the preview, then run the create command it printed, for example:
   rootcoz create-chat-issue --target jira \
     --confirmation-id CONFIRMATION_ID --confirmation-code CONFIRMATION_CODE \
     --title "Dashboard filters drop the team" --body "$(cat ticket-body.md)"
   ```

4. Stop the current reply or start over.

   Use the web UI `Stop` button when a reply is still running and you want to cancel it. When you want a completely fresh conversation, click `New Session` in the web UI or clear the current chat from the CLI:

   ```bash
   rootcoz admin-chat clear
   ```

   `New Session` and `clear` both reset your current Server Chat history so the next question starts clean.

   > **Note:** Server Chat history and saved artifacts are scoped to the current admin account, not shared across all admins.


   > **Warning:** Starting a new session in the web UI or running `rootcoz admin-chat clear` in the CLI also removes saved report artifacts for your account.

5. Save or download HTML report artifacts.

   Use artifacts when you want a reusable HTML summary that can be downloaded later. The CLI can upload a local HTML file into the Server Chat artifact store and download it back by ID.

   ```bash
   rootcoz admin-chat save-artifact ./weekly-summary.html
   rootcoz admin-chat save-artifact ./weekly-summary.html --filename "failure-summary-2026-07-31.html"
   rootcoz admin-chat download-artifact <artifact-id> --output ./failure-summary.html
   ```

   `save-artifact` returns a download URL. `download-artifact` writes the file to the path you choose, and when you do not pass `--output`, RootCoz creates a local HTML filename automatically.

   In the web UI, when a Server Chat reply contains one of these artifact links, RootCoz renders it as a download button instead of a raw URL.

## Advanced Usage

Use one-off model overrides when you want to compare answers without changing the server default:

```bash
rootcoz admin-chat send \
  "Compare issue-creation trends for the last 30 days." \
  --provider claude \
  --model claude-opus-4-6
```

Use JSON history output when you want to hand the conversation to another script or save a machine-readable record:

```bash
rootcoz --json admin-chat history --limit 50
```

Use custom filenames when you are saving reports for teammates or recurring reviews:

```bash
rootcoz admin-chat save-artifact ./report.html --filename "platform-weekly-review"
```

If the filename does not end in `.html`, RootCoz adds the extension for you.

These prompt patterns work well in Server Chat because they match the built-in cross-job reporting tools:

| Goal | Example prompt |
| --- | --- |
| Failure trends | `Summarize the top failure patterns from the last 7 days.` |
| Review accuracy | `Show where reviewers changed the AI classification most often this month.` |
| Follow-up volume | `Which jobs created the most GitHub issues or Jira bugs this quarter?` |

For every available command and flag, see [CLI Command Reference](cli-reference.html) for details. For raw endpoints, see [API Endpoint Reference](api-reference.html) for details.

## Troubleshooting

- `The send button stays disabled in the web UI.`  
  The chat session only starts once a valid provider and model pair is selected in the header. Pick both, type a message, and wait for any in-flight reply to finish.

- A model shows `NO SESSION` and cannot be picked, or `Start` answers `422 AI session refused`.  
  The sidecar will not open a session with that model, so RootCoz refuses it instead of failing with a server error. The `422` detail names the cause: declare the model in that provider's `models.json`, or re-enter the key, or ask an administrator to check the server log. See [Configuration Reference](configuration-reference.html#adding-a-custom-provider) for the `models.json` rule.

- `rootcoz admin-chat send` fails with an admin error, or returns `409 Start a new chat session first`. The 409 means no session exists yet — run `rootcoz admin-chat init` first, which needs `--provider` and `--model`. A genuine admin error is a different failure: check the caller's role.  
  The CLI is not using an admin identity. Confirm it with `rootcoz auth whoami`.

- Server Chat says AI is not configured, or the page never finishes initializing.  
  Set the default AI provider and model in Server Settings. See [Managing Users and Server Settings](manage-users-and-server-settings.html) and [Configuration Reference](configuration-reference.html) for details.

- `A saved artifact link returns 404.`  
  You started a new session or ran `rootcoz admin-chat clear`, which deletes saved artifacts for your account.

- `The admin assistant cannot read results or the database, though Server Chat started normally.`  
  Admin chat starts even when the server cannot mint the internal tools token — `get_job_result`, `db_schema`, `db_query` and `save_report` are then missing from its tool list and the server log carries `Failed to create AI session`. Job chat refuses that preparation and shows the failure message instead; admin chat is deliberately kept available, so start a new admin session once the storage fault is fixed to pick the tools up. The residual is tracked as issue #540.

- `The CLI did not show a final answer.`  
  `admin-chat send` waits up to about two minutes. Check the result later with `rootcoz admin-chat history --limit 20`. For the raw endpoints, see [API Endpoint Reference](api-reference.html) for details.

## Related Pages

- [Exploring History and Reports](explore-history-and-reports.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)
- [Collaborating on Results](collaborate-on-results.html)
- [CLI Command Reference](cli-reference.html)
- [API Endpoint Reference](api-reference.html)