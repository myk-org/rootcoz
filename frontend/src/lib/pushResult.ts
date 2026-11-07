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
 * Defensive path: the backend now pushes every unmappable test — including one
 * with no analysis at all — as TO INVESTIGATE and reports it in `defaulted`, so
 * a current payload always carries `unmatched: []`. Only an older backend can
 * still leave a test unpushed, and the entry says why, so it is listed apart
 * from the headline instead of being crammed into one sentence.
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
 * backend does not know. It covers every such test, a test with no analysis at
 * all included: those are pushed under the same fallback, which is why
 * `unmatched` stays empty. They were pushed, so this is information — never a
 * warning, and never counted as skipped. The detail line names the
 * classification (`none` when it is empty), so the sentence must hold for both.
 */
export function defaultedNotice(
  defaulted: string[],
): { message: string; detail: string } {
  const message = `${defaulted.length} test(s) were pushed as TO INVESTIGATE because they have no mappable classification`
  return { message, detail: listDetail(defaulted) }
}
