# Collaborating on Results

Use per-job chat and comments when you need to understand one analysis together with other people: ask the AI questions about a specific result, leave context for the next reviewer, and pull teammates into the discussion with `@mentions`. Both features live on a single result page and are scoped to one job.

> **Note:** This page covers chat on one result. For questions that span many jobs — trends, review accuracy, cross-job reporting — use the admin-only Server Chat. See [Use Server Chat for Cross-Job Analysis](use-server-chat-for-cross-job-analysis.html) for details.

## Prerequisites

- A completed analysis result. See [Reviewing and Classifying Failures](review-and-classify-failures.html) for details.
- `reviewer`, `operator`, or `admin` access. Both chat and posting comments are gated at reviewer level.
- An AI provider and model you can select when you start a chat, either your own key or server credentials.
- Optional: a `GitHub Token` and Jira credentials on your profile if you want the assistant to search GitHub issues and Jira tickets. See [Managing Your Account and Notifications](manage-account-and-notifications.html) for details.
- For the CLI examples, an authenticated CLI profile. See [Automating Common Tasks with the CLI](automate-common-tasks-with-the-cli.html) for details.

## Quick Example

```bash
rootcoz chat send JOB_ID "Which failures share the same error signature?"
rootcoz comments add JOB_ID --test "tests.test_auth.test_login" -m "Reproduced locally, see PROJ-1234."
```

This asks the job's assistant for a cross-failure answer and leaves the evidence in the result's comment thread for the next reviewer.

## Step-by-Step

1. Open the result and start the chat.

   From the report header, click `Chat`. The button is hidden for viewers, and the chat page opens at `/chat/{job_id}` for the job you came from. Pick a provider and model in the header, then click `Start Chat`. `Use server credentials` chooses whose AI key runs the session; the toggle is only available when your account may use server providers.

   Starting a chat creates a per-user, per-job workspace and session. RootCoz clones the job's tests repo and any additional repositories, then copies CI build data into that workspace when the source provides it.

   > **Note:** Chat history and sessions are scoped to your own account on that job. Two reviewers opening the same result get two separate conversations, and RootCoz pins the provider, model, and credential source you started with for the life of the session.

2. Check what the assistant can reach before you ask.

   The welcome message at the top of a new session lists the resources available for that job, and the assistant's working context is this one job's result and comments. Its history tools are the exception: `get_failure_history` and `get_classification_history` query by test name across the server and exclude only the current job, so the assistant can pull how a test has failed in *other* jobs. It has read-only filesystem tools (`read`, `ls`, `find`, `grep`, `subagent`) plus authenticated HTTP tools:

   | Tool | What it returns |
   | --- | --- |
   | `get_job_result` | The full result: failures, classifications, AI analysis |
   | `get_job_comments` | Comments and review discussion on this job |
   | `get_job_tests` | Paginated test entries (`passed`, `skipped`, `failed`) |
   | `get_failure_history` | Pass/fail history for one test, plus prior classifications |
   | `get_classification_history` | Who changed a test's classification, when, and from what to what |
   | `search_jira` / `get_jira_issue` | Jira search and issue lookup — only when your account has Jira credentials |
   | `search_github_issues` / `get_github_issue` | GitHub issue and PR search in the test repository — only when your account has a GitHub token |

   When the workspace has source or CI data, the assistant can also read the cloned repositories and the build files `console-output.txt`, `build-info.json`, and `build-artifacts/`.

   > **Warning:** The assistant has no shell and cannot write or edit files. It reads what RootCoz gave it and calls only the endpoints listed above; it will not execute commands or make changes on your behalf.

3. Ask your question.

   Type in the `Ask a question...` box and press `Enter` to send, `Shift+Enter` for a newline. While the answer is being generated the thread shows `Thinking...` and a `Stop` button. Replies stream into the same conversation and are saved, so you can close the page and come back later.

   > **Tip:** Because the assistant can read the cloned test code, questions like `Why does test_login assert on the old token format?` are often answered faster than asking for a re-analysis with a different model.

4. Comment on the failure, not just in the chat.

   Expand a failure card and use the `Add a comment...` box, then click `Post`. Comments are attached to the specific test — including grouped cards, where the comment covers the group you clicked — and they persist independently of any chat session.

   Type `@` in the comment box to get a list of usernames you can mention. A mentioned teammate gets a navbar badge and unread entry in `Mentions`; if web push is configured on the server, they also get a push notification.

   Jira keys and GitHub PR links pasted into a comment are enriched with their live status as a badge next to the link.

   > **Note:** After you post, RootCoz checks whether the comment suggests the failure is already resolved. If it does, it asks `Mark as reviewed?` and you confirm. That suggestion never marks anything on its own.

5. Delete or clear when you are done.

   Comments show a delete control to their author; admins can delete any comment. For chat, `New Session` clears your messages, session, and cloned repositories for that job so you can start again with a different model.

   > **Warning:** `New Session` deletes your chat history for that job. It is per-user, so it does not touch a colleague's conversation.

6. Use the same workflow from the CLI.

   ```bash
   rootcoz chat init JOB_ID --provider claude --model claude-opus-4-6
   rootcoz chat send JOB_ID "Which failures are infrastructure rather than code issues?"
   rootcoz chat history JOB_ID --limit 20
   rootcoz chat abort JOB_ID
   rootcoz chat clear JOB_ID
   rootcoz chat close JOB_ID

   rootcoz comments list JOB_ID
   rootcoz comments add JOB_ID --test "tests.test_auth.test_login" -m "Fixed in PROJ-1234."
   rootcoz comments delete JOB_ID 42
   ```

   `chat send` waits for the assistant reply and prints it; `chat close` tells the server you left the page, while `chat clear` removes the conversation.

## Advanced Usage

Check your mention inbox and tracker status from the terminal:

```bash
rootcoz mentionable-users
rootcoz mentions --unread --limit 20
rootcoz mentions-mark-read --ids 91,92
rootcoz mentions-mark-all-read
rootcoz enrich-comments JOB_ID
```

`enrich-comments` refreshes the live status badges for Jira tickets and GitHub PRs found in that job's comments, and `analyze-comment-intent` returns the reviewed/not-reviewed judgment behind the `Mark as reviewed?` prompt:

```bash
rootcoz analyze-comment-intent "Fixed by updating the token fixture." --job-id JOB_ID
```

These prompts match the tools the job assistant actually has:

| Goal | Example question |
| --- | --- |
| Group related failures | `Which failures share this error signature?` |
| Check flakiness | `Has tests.test_auth.test_login failed in previous runs of this job?` |
| Review the discussion | `What did reviewers say about the failures we marked reviewed?` |
| Trace the change | `Which code in the test repo asserts on the old token format?` |
| Check for existing work | `Is there an open Jira issue for this product bug?` |

Use `--json` on any of these commands when you want machine-readable output. For every flag, see [CLI Command Reference](cli-reference.html) for details. For the raw endpoints, see [API Endpoint Reference](api-reference.html) for details.

## Troubleshooting

- The `Chat` button is missing from the report header.  
  Chat requires `reviewer` access. Viewers can read results and comments but cannot start or use chat.

- `Start Chat` fails with `Chat session unavailable.`  
  The session could not be created. Clear the chat with `New Session` and start again, and check that the provider and model are still available for your credential source.

- The assistant says Jira or GitHub search is not available.  
  Those tools are only registered when your account has the matching credentials. Add them on your profile settings page, then start a new chat session so the new tool list is picked up. See [Managing Your Account and Notifications](manage-account-and-notifications.html) for details.

- The assistant refuses to answer your question.  
  Job chat is scoped to that one analysis. Off-topic questions are rejected by design. For cross-job or server-wide questions, use [Use Server Chat for Cross-Job Analysis](use-server-chat-for-cross-job-analysis.html) for details.

- My teammate is not notified about an `@mention`.  
  The navbar badge and `Mentions` list always update for signed-in users. Browser push notifications additionally require web push to be configured on the server.

- I cannot delete a comment someone else wrote.  
  Deleting is limited to the comment author; admins can delete any comment.

## Related Pages

- [Reviewing and Classifying Failures](review-and-classify-failures.html)
- [Use Server Chat for Cross-Job Analysis](use-server-chat-for-cross-job-analysis.html)
- [Creating Follow-Up Issues and Pushing Results](create-follow-up-issues-and-push-results.html)
- [Managing Your Account and Notifications](manage-account-and-notifications.html)
- [CLI Command Reference](cli-reference.html)
