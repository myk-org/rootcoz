/**
 * User-facing copy for a push result that left tests out.
 *
 * An `unmatched` test is not a Report Portal matching failure: it has no
 * (or an unknown) AI classification, so there is no defect type to map it to.
 * The entry carries the offending classification, so it is listed apart from
 * the headline instead of being crammed into one sentence.
 */
export function unmatchedFailure(
  pushed: number,
  unmatched: string[],
): { message: string; detail: string } {
  const skipped = unmatched.length
  const reason = `${skipped} test(s) have no classification to map to a Report Portal defect type`
  // A partial push must be visible at a glance, not inferred from the sentence.
  const message = pushed !== skipped ? `Pushed ${pushed} · skipped ${skipped} — ${reason}` : reason
  const shown = unmatched.slice(0, 3)
  const detail =
    skipped > shown.length
      ? `${shown.join(', ')} and ${skipped - shown.length} more`
      : shown.join(', ')
  return { message, detail }
}
