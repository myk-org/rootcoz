/** First three entries, with a tail count for the rest. */
function listDetail(entries: string[]): string {
  const shown = entries.slice(0, 3)
  return entries.length > shown.length
    ? `${shown.join(', ')} and ${entries.length - shown.length} more`
    : shown.join(', ')
}

/**
 * User-facing copy for a push result that left tests out.
 *
 * An `unmatched` test was not pushed at all (no classification, or one the
 * backend has no defect type for). The entry carries the offending
 * classification, so it is listed apart from the headline instead of being
 * crammed into one sentence.
 */
export function unmatchedFailure(
  pushed: number,
  unmatched: string[],
): { message: string; detail: string } {
  const skipped = unmatched.length
  const reason = `${skipped} test(s) have no classification to map to a Report Portal defect type`
  // A partial push must be visible at a glance, not inferred from the sentence.
  const message = pushed !== skipped ? `Pushed ${pushed} · skipped ${skipped} — ${reason}` : reason
  return { message, detail: listDetail(unmatched) }
}

/**
 * Copy for tests the backend pushed under the TO INVESTIGATE fallback because
 * their classification was empty or unmapped. They were pushed, so this is
 * information — never a warning, and never counted as skipped.
 */
export function defaultedNotice(
  defaulted: string[],
): { message: string; detail: string } {
  const message = `${defaulted.length} test(s) were pushed as TO INVESTIGATE because they have no classification`
  return { message, detail: listDetail(defaulted) }
}
