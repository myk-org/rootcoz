# Finding Runs on the Dashboard

Use the dashboard filters to get from a long run list to the exact analysis you need, whether it is still active or already finished. Filtering first helps you avoid opening the wrong run when several jobs have similar names or many runs are happening at once.

## Prerequisites

- You can sign in and open `Dashboard`.
- At least one analysis run exists. See [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html) for details.

## Quick Example

1. Open `Dashboard`.
2. Set `Status` to `running`.
3. Choose a `Team`.
4. If tags are available, click one under `Filter by tag`.
5. Set `From` and `To` to the day range you care about.
6. Click the matching run.

> **Tip:** Watch the run count above the table while you filter. Once it drops to a short list, open the run you want.

## Step-by-Step

1. Start with the newest runs.  
   The dashboard shows the most recent runs first and refreshes automatically, so active runs can change status without a manual reload.

2. Narrow the list by status.  
   Use `Status` first to separate active work from finished or failed runs.

   | If you want to find | Set `Status` to |
   | --- | --- |
   | A run still waiting on Jenkins | `waiting` |
   | A run queued for analysis | `pending` |
   | A run currently being analyzed | `running` |
   | A finished report | `completed` |
   | A run that ended with an analysis error | `failed` |
   | An AI analysis timeout | `timeout` |

   > **Note:** Runs labeled `Analysis Timed Out` are found with the `timeout` filter.

3. Add metadata filters.  
   Use `Team`, `Tier`, and `Version` to narrow the list by ownership or release. If your runs use tags, click one or more tags under `Filter by tag` to narrow the list further.

   The `Clear metadata` button removes only the metadata filters, which makes it easy to try a different combination.

4. Limit the date range.  
   Use `From` and `To` to keep only runs created inside the time window you care about. Use the clear button next to the date fields to remove both dates at once.

5. Refine the short list.  
   Use the search box to narrow by run name. If you still have too many matches, sort by `Created`, `Failures`, `Reviewed`, `Comments`, or `Children`, and raise `Rows per page` to `50`.

6. Open the run you need.  
   Click the row or the run name. Active runs, failed runs, and timed-out runs open the live status view; completed runs open the full report.

## Advanced Usage

- Tag filters use AND logic. If you select two tags, the dashboard shows only runs that have both tags.
- `From` and `To` are inclusive. A same-day range keeps runs from the start of that UTC day through the end of that UTC day.
- Search is case-insensitive, which is useful after you have already narrowed the list with status or metadata.
- `Team`, `Tier`, `Version`, tag, and date filters stay in the page URL. Refreshing keeps them, and you can bookmark or share that filtered view.
- The search text and `Status` choice do not stay in the URL, so they reset on a full reload.
- Sort order is remembered for the current browser tab, which helps if you prefer scanning by `Created` or `Failures`.
- The dashboard shows the 500 most recent runs. If an older run is missing, it may have aged out of the dashboard list.

> **Warning:** Date filtering uses the run's created date in UTC. A run close to midnight can fall on a different day than the local time shown in your browser.

## Troubleshooting

- I do not see `Team`, `Tier`, `Version`, or tag filters: those controls only appear when runs already have metadata values to choose from.
- I see `No jobs match your filters`: remove filters one at a time, starting with tags or date, then reset `Status`, then clear the search text.
- I cleared metadata, but the list is still empty: `Clear metadata` does not remove the date range, search text, or `Status` filter.
- A run looks like it should match the date, but it does not: hover over `Created` to check the exact timestamp, then compare it with the UTC date range.

## Related Pages

- [Submitting Jenkins Builds and JUnit XML](submitting-jenkins-builds-and-junit-xml.html)
- [Tracking Analysis Progress](tracking-analysis-progress.html)
- [Reviewing and Classifying Failures](reviewing-and-classifying-failures.html)
- [Exploring Failure History and Test Trends](exploring-failure-history-and-test-trends.html)