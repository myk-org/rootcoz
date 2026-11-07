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
 * An `unmatched` test was not pushed at all: it carries no analysis (a
 * provider-outage placeholder or a narration-only answer), so pushing it would
 * overwrite whatever defect type a human set on the Report Portal item. The
 * entry names that, so it is listed apart from the headline instead of being
 * crammed into one sentence.
 */
export function unmatchedFailure(
  pushed: number,
  unmatched: string[],
): { message: string; detail: string } {
  const skipped = unmatched.length
  const reason = `${skipped} test(s) have no classification to map to a Report Portal defect type`
  // A partial push must be visible at a glance, not inferred from the sentence.
  // Only a real split counts: "Pushed 0" is not a push.
  const isPartial = pushed > 0 && skipped > 0
  const message = isPartial ? `Pushed ${pushed} · skipped ${skipped} — ${reason}` : reason
  return { message, detail: listDetail(unmatched) }
}

/**
 * Copy for tests the backend pushed under the TO INVESTIGATE fallback because
 * their classification had no defect type to map to — either empty or one the
 * backend does not know. It never covers a test with no analysis at all: those
 * are left untouched and surface as `unmatched` instead. They were pushed, so
 * this is information — never a warning, and never counted as skipped. The
 * detail line names the classification (`none` when it is empty), so the
 * sentence must hold for both.
 */
export function defaultedNotice(
  defaulted: string[],
): { message: string; detail: string } {
  const message = `${defaulted.length} test(s) were pushed as TO INVESTIGATE because they have no mappable classification`
  return { message, detail: listDetail(defaulted) }
}
