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

   RootCoz can use the server default AI settings or a one-off override for the next question.

   | If you want | Web UI | CLI |
   | --- | --- | --- |
   | Use the server default | Leave the provider and model already loaded in the header. | Omit `--provider` and `--model`. |
   | Override for one question | Pick a different provider and model in the header before sending. | Add `--provider` and `--model` to `rootcoz admin-chat send`. |
   | Keep using the same override | Leave the header selection in place. | Repeat the same flags on the next `send` command. |

   If you need to change the shared defaults for everyone, see [Managing Users and Server Settings](manage-users-and-server-settings.html) for details.

3. Ask a cross-job question and follow the reply.

   In the web UI, type your question and press `Enter` to send it. Use `Shift+Enter` when you want a newline instead. While RootCoz is working, the thread shows `Thinking...` and keeps the response in the same conversation.

   In the CLI, send the question directly:

   ```bash
   rootcoz admin-chat send "Show where humans overrode the AI most often this month."
   ```

   The CLI waits for the assistant reply when it can. If you want to come back later, or the response takes longer than expected, check the stored conversation:

   ```bash
   rootcoz admin-chat history --limit 20
   ```

   > **Tip:** Use the web UI when you want the clearest live experience. It shows the active reply immediately and gives you a `Stop` button while the assistant is still working.

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

- The `Send` button is disabled in the web UI: choose both an AI provider and a model, type a message, and wait for any current reply to finish.
- `rootcoz admin-chat send` fails with an admin error: verify the CLI is using an admin API key with `rootcoz auth whoami`.
- Server Chat says AI is not configured or the page never finishes initializing: configure the default AI provider and model in Server Settings. See [Managing Users and Server Settings](manage-users-and-server-settings.html) and [Configuration Reference](configuration-reference.html) for details.
- A saved artifact link returns `404`: you likely started a new session or ran `rootcoz admin-chat clear`, which removes saved artifacts for your account.
- The CLI did not show a final answer: check later with `rootcoz admin-chat history --limit 20`. If you need the raw endpoints instead, see [API Endpoint Reference](api-reference.html) for details.

## Related Pages

- [Exploring History and Reports](explore-history-and-reports.html)
- [Managing Users and Server Settings](manage-users-and-server-settings.html)
- [Collaborating on Results](collaborate-on-results.html)
- [CLI Command Reference](cli-reference.html)
- [API Endpoint Reference](api-reference.html)