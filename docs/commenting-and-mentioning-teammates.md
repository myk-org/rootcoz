# Commenting and Mentioning Teammates

You want to keep failure follow-up attached to the exact report your team is already using, and pull the right person into the conversation without losing context. RootCoz lets you comment directly on a failure, `@mention` teammates, and use `Mentions` plus optional browser notifications to keep the thread moving.

## Prerequisites

- You are signed in with a RootCoz username.
- You have a report with at least one failure.
- Optional: your browser supports push notifications if you want mention alerts.
- If you still need to locate the report, see [Finding Runs on the Dashboard](finding-runs-on-the-dashboard.html).

## Quick Example

```text
Hey @alice check this
```

Post that in a failure's `Comments` box. RootCoz highlights the mention in the thread, adds it to `alice`'s `Mentions` inbox, and keeps the discussion attached to the failure.

## Step-by-Step

1. Open the report and expand the failure you want to discuss.

   Comments live inside each failure card under `Comments`. If several tests are grouped into one failure card, they share the same comment thread.

2. Write the comment, then add the mention.

```text
Opened bug: OCPBUGS-12345
cc @alice @bob
```

   Type `@` and the start of a username. RootCoz shows matching usernames so you can pick the right person instead of guessing.

> **Tip:** Use the suggestion list instead of typing a username from memory. Mentions are exact, so a typo or shortened name will not notify the right person.

3. Post the comment.

   Press `Enter` to post, or click `Post`. Use `Shift+Enter` when you want a new line without sending the comment.

4. Follow up from `Mentions`.

   Open `Mentions` from the top navigation. Unread mentions show a badge, and clicking a mention marks it read and opens the matching report with the relevant failure card expanded.

5. Clear the inbox when you are done.

   Open mentions one by one as you work through them, or use `Mark all as read` to clear everything at once. If you need to correct something, delete your own comment from the report and post a new one.

6. Enable browser notifications if you want faster follow-up.

   When push notifications are available, RootCoz can prompt you to enable them. You can also turn them on or off later from your profile, and notifications are only used for `@mentions`.

> **Note:** RootCoz refreshes comments in an open report and unread mention counts while you work, so you usually do not need to reload the page to see new discussion.

## Advanced Usage

- Keyboard controls make comment entry faster: `Enter` posts the comment, `Shift+Enter` adds a new line, `Arrow Up` and `Arrow Down` move through mention suggestions, `Enter` or `Tab` inserts the highlighted username, and `Esc` closes the suggestion list.

- If your comment clearly says the failure is already handled, RootCoz may ask whether you want to mark it reviewed right away.

- Add issue or PR links directly in the thread when they help the next person act faster:

```text
Fix merged: https://github.com/org/repo/pull/123
```

  Supported GitHub and Jira links become clickable in comments, and RootCoz can show live status such as `open`, `closed`, or `merged` next to them.

- Mention matching follows these rules:

| Text | Result |
| --- | --- |
| `@alice` | Mentions `alice` |
| `cc @alice @bob` | Mentions both users |
| `@alice-bob` | Valid mention |
| `@alice_bob` | Valid mention |
| `user@domain.com` | Not treated as a mention |
| `@alice ping @alice` | One mention entry for `alice` |

- Self-mentions still appear in `Mentions`, but they do not trigger a browser notification.

- If you prefer the CLI, these commands cover the same workflow:

```bash
rootcoz mentionable-users
rootcoz comments add job-1 --test "tests.TestFoo.test_bar" --message "Hey @alice check this"
rootcoz mentions --unread
rootcoz mentions-mark-read --ids 1,2,3
rootcoz mentions-mark-all-read
```

  Browser notifications are browser-only, so there is no CLI command for push subscription. See [CLI Command Reference](cli-command-reference.html) for full syntax.

- If the same failure keeps returning, review older comments and outcomes in its history. See [Exploring Failure History and Test Trends](exploring-failure-history-and-test-trends.html) for details.

## Troubleshooting

- The `@` suggestion list does not appear: Type `@` and keep typing the username prefix. If you are unsure of the exact name, run `rootcoz mentionable-users` and use one of the listed usernames.

- You are not getting browser notifications: Your browser must support push notifications, this site's notification permission must be allowed, and the RootCoz server must have push notifications configured. If you previously blocked notifications, re-enable this site's notification permission in your browser settings.

- You clicked `Not now` and the prompt did not come back: enable notifications later from your profile instead.

- You can open reports but cannot post comments: Your RootCoz server may be using an allow list for write actions. Ask an administrator to add your username.

- You need to fix a comment after posting: RootCoz supports delete, not in-place edit. Delete your own comment and repost the corrected version.

## Related Pages

- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)
- [Updating Your Profile and Notifications](updating-your-profile-and-notifications.html)
- [Creating GitHub and Jira Issues](creating-github-and-jira-issues.html)
- [Exploring Failure History and Test Trends](exploring-failure-history-and-test-trends.html)
- [CLI Command Reference](cli-command-reference.html)