# Review and Classify Failures

Use the report page to turn an analysis into an actionable team record: confirm what is understood, call in the right person, and correct the AI when needed. The goal is to leave each failure card with a clear review state, useful comments, and the right manual classification.

## Prerequisites
- A completed analysis report. If you need one first, see [Analyze Your First Jenkins Job](analyze-your-first-jenkins-job.html).
- A saved username in the web UI so comments, reviews, and mentions are attributed correctly.

## Quick Example

```text
Hey @alice, can you check this?
```

1. Open a completed report and expand a card that shows `+N more with same error`.
2. Read the `Error` and `Analysis` sections.
3. Paste the comment above into `Add a comment...`, then click `Post`.
4. Click `Review All` for the whole group, or `Review` if the card contains only one test.
5. If the failure belongs to the environment rather than the code or product, choose `INFRASTRUCTURE` from the classification dropdown.

> **Tip:** `Shift+Enter` adds a new line. If the mention picker is open, `Enter` or `Tab` inserts the highlighted username instead of posting.

## Step-by-Step
1. Start with grouped failures. In the `Failures` section, a card with `+N more with same error` means multiple tests are being worked as one review unit. Expand the card to see the affected tests, the raw `Error`, the AI `Analysis`, and any evidence or fix details.

2. Mark items as reviewed as you finish them. Single-test cards use `Review`. Grouped cards add `Review All`, and you can still mark individual tests inside `Affected Tests` if only part of the group is ready. When a card is marked, the button shows who reviewed it.

3. Leave comments in the card where the decision belongs. Use `Add a comment...` and `Post` at the bottom of the expanded card. Comments save immediately, show the author and timestamp, and stay attached to that failure card scope. The trash icon only appears for comments posted under your current username.

4. Use `@mentions` when you need a handoff or second opinion. Type `@` and start a username to open the suggestion list. `Enter` or `Tab` inserts the selected name, and `Escape` closes the picker. Mentions are only recognized as standalone names, so `user@domain.com` does not count as a mention.

```text
@alice
@my-user
@another_user
@user123
```

5. Override the AI classification when human judgment should win. The classification dropdown is intentionally limited to `CODE ISSUE`, `PRODUCT BUG`, and `INFRASTRUCTURE`. On a grouped card, the change applies to every test shown in that group within the current scope.

| Override | Use it when | What changes in the card |
| --- | --- | --- |
| `CODE ISSUE` | The failure should be fixed in the product code or the tests | `Bug Report` is removed, and any `Suggested Fix` stays visible |
| `PRODUCT BUG` | The product behavior is the real defect | `Suggested Fix` is removed, and any `Bug Report` stays visible |
| `INFRASTRUCTURE` | Jenkins, the lab, credentials, or another external dependency caused the failure | Both `Suggested Fix` and `Bug Report` are removed |

6. Review nested child jobs in place. If the report has a `Child Jobs` section, expand the failing child build and keep drilling down until you reach the exact branch of the pipeline you need. Reviews, comments, and overrides are scoped to that child build, so the same test name in another child build is handled separately.

7. Check progress in the sticky header and move on. The header shows whether nothing, some, or all failures are reviewed. Comment and review changes appear automatically while the report is open. Manual classification overrides save immediately, but other people with the same report already open need to reload to see them.

> **Note:** There is no final save step. Review toggles, comments, and classification overrides are stored as soon as you make the change.

## Advanced Usage
Use `Expand all` and `Collapse all` in both `Failures` and `Child Jobs` when you are working through a long report. This is the fastest way to compare repeated failures or walk a multi-level pipeline without reopening cards one by one.

If a grouped card shows more than one classification badge, the tests inside that group already have mixed saved overrides. Picking one value in the classification dropdown normalizes the entire group again in that scope.

Paste tracker references directly into comments when you want the card to carry follow-up status. JJI recognizes full GitHub issue URLs, full GitHub pull request URLs, and Jira keys.

```text
Fix merged: https://github.com/org/repo/pull/123
See https://github.com/RedHatQE/mtv-api-tests/issues/359
Opened bug: OCPBUGS-12345
```

When JJI can resolve those references, it adds live status badges such as `merged`, `open`, or `closed` next to the link or ticket key. While you are typing a draft comment, automatic comment and review refresh pauses so your text is not interrupted.

Use the copy buttons beside the test name, error, analysis, and other card sections when you need to paste evidence into chat, tickets, or commit messages. For scripted review workflows instead of the browser, see [CLI Command Reference](cli-command-reference.html) or [REST API Reference](rest-api-reference.html).

## Troubleshooting
- I can open the report, but posting comments, marking review, or changing the classification fails: your server may be using a write allow list, or your username is missing. Ask an administrator to add your username if needed.
- I do not see the trash icon for a comment I wrote earlier: switch back to the same username that was active when you posted it.
- A teammate's new comment or review is not showing up yet: wait until you are not typing in a comment box, or reload the report.
- A teammate's manual override is not showing up yet: reload the report. Manual override changes are not live-polled into other open tabs.
- My `@mention` did not trigger: use a standalone token such as `@alice`, not an email address like `user@domain.com`.
- Reclassifying one child build did not change another one with the same test name: repeat the change inside each child build you want to update.
- A grouped `Review All` or classification change only partly succeeded: retry from the same card. JJI keeps the successful updates and names the tests that still failed.
- A tracker link has no status badge: use a full GitHub issue or pull-request URL, or a Jira key such as `OCPBUGS-12345`, and make sure the server can reach that tracker.

## Related Pages

- [Analyze a Jenkins Job](analyze-a-jenkins-job.html)
- [Investigate Failure History](investigate-failure-history.html)
- [Create GitHub Issues and Jira Bugs](create-github-issues-and-jira-bugs.html)
- [Push Classifications to Report Portal](push-classifications-to-report-portal.html)
- [Configure Your Profile and Notifications](configure-your-profile-and-notifications.html)