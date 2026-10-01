import { api } from '@/lib/api'
import { getUsername } from '@/lib/cookies'
import { scopedGroups } from '@/lib/grouping'
import type { AnalysisResult, GroupedFailure, ReviewState, TrackedInEntry } from '@/types'

/** Every per-failure mutation is scoped by the reviewKey triple. */
export interface FailureScope {
  testName: string
  childJobName?: string
  childBuildNumber?: number
}

/** A selected failure group (one card) with all of its tests. */
export interface SelectedGroup {
  /** Failure-group id — the stable UUID-based key used by the card. */
  id: string
  testNames: string[]
  childJobName?: string
  childBuildNumber?: number
  /** Set when a partial-failure retry narrowed this selection to the tests that
   *  failed. Deliberate, user-visible state — the bulk bar counts exactly those
   *  tests, so the next refresh must not undo what the user is looking at.
   *
   *  A selected group re-widens to its current members only when the user has not
   *  narrowed it; a selection narrowed by a partial-failure retry stays narrowed
   *  across refreshes until that run completes. */
  narrowed?: boolean
}

/** Every test name of a list of groups. */
function groupNames(groups: GroupedFailure[]): string[] {
  return groups.flatMap((x) => x.tests.map((t) => t.test_name))
}

/** `scopedGroups` memoized per scope for the duration of ONE reconciliation or
 *  widening pass. The grouping walk is O(report), so a selection of N groups must
 *  not repeat it per group: the count is once per DISTINCT scope per pass, not
 *  once per selected group.
 *
 *  The cache lives on this closure and is thrown away when the pass returns — it
 *  must never outlive the call, because a later pass reconciles a later
 *  (refreshed) result and would otherwise answer from stale membership. */
function scopedGroupPass(result: Pick<AnalysisResult, 'failures' | 'child_job_analyses'>) {
  const cache = new Map<string, GroupedFailure[]>()
  return (childJobName?: string, childBuildNumber?: number): GroupedFailure[] => {
    const key = `${childJobName ?? ''}#${childBuildNumber ?? 0}`
    const hit = cache.get(key)
    if (hit) return hit
    const groups = scopedGroups(result, childJobName, childBuildNumber)
    cache.set(key, groups)
    return groups
  }
}

function scopeBody({ childJobName, childBuildNumber }: Omit<FailureScope, 'testName'>) {
  return { child_job_name: childJobName ?? '', child_build_number: childBuildNumber ?? 0 }
}

/** Identity of a live group AS SEEN FROM ONE SCOPE. The group id alone is not an
 *  identity: `groupFailures` builds every scope's groups with the same id prefix,
 *  so a legacy failure without a UUID yields the same `group-<signature>` id in the
 *  top-level report and in a child job. Anything that decides "these two refer to
 *  the same group" must compare this, never the bare id. */
function scopeKey({ childJobName, childBuildNumber }: Pick<SelectedGroup, 'childJobName' | 'childBuildNumber'>, liveId: string) {
  return `${childJobName ?? ''}#${childBuildNumber ?? 0}:${liveId}`
}

/* -- Per-failure endpoints (shared by the single-row controls and bulk bar) -- */

export function putReviewed(jobId: string, scope: FailureScope, reviewed: boolean) {
  return api.put<{ status: string; reviewed_by: string }>(`/results/${jobId}/reviewed`, {
    test_name: scope.testName,
    reviewed,
    ...scopeBody(scope),
  })
}

export function putOverrideClassification(jobId: string, scope: FailureScope, classification: string) {
  return api.put(`/results/${jobId}/override-classification`, {
    test_name: scope.testName,
    classification,
    ...scopeBody(scope),
  })
}

export function putOverridePattern(jobId: string, scope: FailureScope, pattern: string) {
  return api.put(`/results/${jobId}/override-pattern`, {
    test_name: scope.testName,
    pattern,
    ...scopeBody(scope),
  })
}

export function putTrackedIn(jobId: string, scope: FailureScope, url: string, type: string) {
  return api.put(`/results/${jobId}/tracked-in`, {
    test_name: scope.testName,
    url,
    type,
    ...scopeBody(scope),
  })
}

export function getTrackedIn(jobId: string) {
  return api.get<{ tracked_in: Record<string, TrackedInEntry[]> }>(`/results/${jobId}/tracked-in`)
}

/** Validate an issue URL before saving a tracked-in link.
 *  Empty input is not an error (callers handle "nothing typed"); returns the
 *  message to show, or null when the URL is usable as a link. */
export function trackedInUrlError(url: string): string | null {
  const trimmed = url.trim()
  if (!trimmed) return null
  try {
    const parsed = new URL(trimmed)
    if (!['http:', 'https:'].includes(parsed.protocol)) {
      return 'URL must start with http:// or https://'
    }
    return null
  } catch {
    return 'Please enter a valid URL (e.g., https://github.com/org/repo/issues/123)'
  }
}

/** Notify AllReviewedPrompt that review state changed. */
export function notifyReviewChanged(jobId: string) {
  setTimeout(() => window.dispatchEvent(new CustomEvent('rootcoz:review-changed', { detail: { jobId } })), 100)
}

/** Reviewer name for optimistic review state when the API omits it. */
function reviewActor(reviewedBy?: string) {
  return reviewedBy ?? getUsername()
}

/** Optimistic review state after a successful /reviewed call. */
export function scopedReviewState(reviewed: boolean, reviewedBy?: string): ReviewState {
  return { reviewed, username: reviewActor(reviewedBy), updated_at: new Date().toISOString() }
}

/** Flatten selected groups into one scope per affected test. */
export function selectedScopes(groups: SelectedGroup[]): FailureScope[] {
  return groups.flatMap((g) =>
    g.testNames.map((testName) => ({
      testName,
      childJobName: g.childJobName,
      childBuildNumber: g.childBuildNumber,
    })),
  )
}

/** Reconcile the selection against the live group membership of a refreshed result.
 *
 *  THE RULE: a selected group re-widens to its current members only when the user
 *  has not narrowed it; a selection narrowed by a partial-failure retry stays
 *  narrowed across refreshes until that run completes.
 *
 *  Both branches reconcile MEMBERSHIP, not bare name existence:
 *  - `narrowed` entry: keeps exactly its own names, minus any the refresh removed.
 *    NEVER re-widened — the retry leftover is what the user is looking at.
 *  - any other entry: tracks the group's ACTUAL live members, so a test that joins
 *    the signature is picked up by the next request. When a refresh splits the
 *    entry across several live groups it holds the union, so a name is dropped
 *    here only when it no longer exists in the report.
 *
 *  DE-DUPLICATION applies to EVERY entry, narrowed or not — narrowing exempts an
 *  entry from RE-WIDENING, never from folding. Entries are keyed by the live group
 *  they resolve to, and entries that resolve to the SAME live group become ONE
 *  entry holding the union of their names. Without this, two groups a refresh
 *  merged would stay two entries over one live group: the bulk bar counts each
 *  test twice, sends a duplicate request, and a failed duplicate leaves an
 *  already-updated test selected for retry.
 *
 *  The bucket key is SCOPE-QUALIFIED (see `scopeKey`) and the fold is taken to its
 *  CLOSURE (see the union below): both are load-bearing, not tidiness.
 *  - scope-qualified: `scopedGroups` builds every scope's groups with the same
 *    `group-` id prefix, so a legacy failure without a UUID gets a signature-
 *    derived id that is IDENTICAL in the top-level report and in a child job.
 *    Folding on the bare id merges those two and keeps the last contributor's
 *    `childJobName`/`childBuildNumber` — the entry then sends its tests to the
 *    wrong child job.
 *  - closed: a bridging entry (it resolves to two live groups that were separate
 *    when the user selected them) grows its bucket past a bucket folded earlier,
 *    so a first-match fold leaves two entries claiming one live group.
 *
 *  ponytail: an entry that spans two live groups (the split) can only be keyed by
 *  ONE of them, so the other half's card reads unchecked while the bar counts its
 *  tests — the same 1-vs-2 disagreement the split display test pins. Making every
 *  spanned card selected means one entry per live group; add when the product
 *  owner picks that side.
 */
export function reconcileSelection(
  result: AnalysisResult,
  selection: Record<string, SelectedGroup>,
): Record<string, SelectedGroup> {
  const scoped = scopedGroupPass(result)
  // Each entry reconciled on its own, with the live groups it resolves to.
  const resolved = Object.entries(selection).map(([id, g]) => {
    const live = scoped(g.childJobName, g.childBuildNumber)
    const selected = new Set(g.testNames)
    const alive = new Set(groupNames(live))
    const siblings = live.filter((x) => x.tests.some((t) => selected.has(t.test_name)))
    const testNames = g.narrowed
      ? g.testNames.filter((n) => alive.has(n))
      : groupNames(siblings)
    return { id, g, testNames, liveIds: siblings.map((x) => x.id) }
  }).filter((r) => r.testNames.length > 0)

  // Fold: ONE entry per CONNECTED set of live groups — a merge (two selected groups
  // that now share a signature), a split touching an already-selected group, or a
  // bridging entry that reaches two buckets at once. Connected components of the
  // "shares a live group" relation, so the result is closed: no two surviving
  // entries claim the same live group, however the chain got there. The entry
  // holds the union of the contributors' names, and `narrowed` if ANY contributor
  // was narrowed.
  //
  // Every contributor to a component shares a scope-qualified key with it, so a
  // component never spans two scopes: the emitted scope is every contributor's.
  const parent = resolved.map((_, i) => i)
  const find = (i: number): number => (parent[i] === i ? i : (parent[i] = find(parent[i])))
  const owners = new Map<string, number>()
  resolved.forEach((r, i) => {
    for (const key of r.liveIds.map((liveId) => scopeKey(r.g, liveId))) {
      const owner = owners.get(key)
      if (owner === undefined) owners.set(key, i)
      else parent[find(owner)] = find(i)
    }
  })

  // One entry per component, keyed by its root and inserted in first-appearance
  // order, so the emitted order is the order the user selected in.
  const buckets = new Map<number, SelectedGroup>()
  for (let i = 0; i < resolved.length; i++) {
    const root = find(i)
    const existing = buckets.get(root)
    if (!existing) buckets.set(root, { ...resolved[i].g, testNames: resolved[i].testNames })
    else {
      existing.testNames = [...new Set([...existing.testNames, ...resolved[i].testNames])]
      existing.narrowed = existing.narrowed || resolved[i].g.narrowed
    }
  }

  // THE EMIT KEY is a LIVE group id of the component, in the scope the entry is
  // emitted under — a card is `isSelected = !!selection[group.id]`, so a key no
  // card answers to renders unchecked AND cannot be unticked, with its tests still
  // counted in the bulk bar. Not the union-find root's contributor: a merge roots
  // on the LATER contributor, whose id a refresh that kept the earlier group's id
  // has already dropped. Prefer the FIRST contributor's id when the refresh kept
  // it (that is the card the user is looking at), else the first live id the
  // component claims. A component always has one — a surviving entry has a live
  // sibling by construction — so no entry is dropped here.
  return Object.fromEntries(
    [...buckets].flatMap(([root, group]) => {
      const members = resolved.filter((_, i) => find(i) === root)
      const liveIds = members.flatMap((r) => r.liveIds)
      const id = [members[0].id, ...liveIds].find((candidate) => liveIds.includes(candidate))
      return id === undefined ? [] : [[id, { ...group, id }]]
    }),
  )
}

/** Widen each group back to its full error-signature group(s): the backend applies an
 *  override to every sibling, so the optimistic patch must cover them too. A refresh
 *  can split one selected signature group across several live ones — every match is
 *  emitted, so each still gets its own request instead of losing the moved half. */
export function widenToSignatureGroups(
  result: AnalysisResult | null,
  groups: SelectedGroup[],
): SelectedGroup[] {
  if (!result) return groups
  const scoped = scopedGroupPass(result)
  const seen = new Set<string>()
  const widened: SelectedGroup[] = []
  for (const g of groups) {
    const selected = new Set(g.testNames)
    const siblings = scoped(g.childJobName, g.childBuildNumber)
      .filter((x) => x.tests.some((t) => selected.has(t.test_name)))
    const targets: SelectedGroup[] = siblings.length === 0
      ? [g]
      : siblings.map((x) => ({
        id: x.id,
        testNames: groupNames([x]),
        childJobName: g.childJobName,
        childBuildNumber: g.childBuildNumber,
      }))
    for (const t of targets) {
      // One request per signature group: two selection entries that widen to the
      // same live group must not send it twice.
      const key = `${t.childJobName ?? ''}#${t.childBuildNumber ?? 0}:${t.testNames.join(',')}`
      if (seen.has(key)) continue
      seen.add(key)
      widened.push(t)
    }
  }
  return widened
}

/** Run a mutation over every scope in batches; never rejects — returns the failed scopes. */
export async function runBatched<T>(
  items: T[],
  fn: (item: T) => Promise<unknown>,
  batchSize = 5,
): Promise<T[]> {
  const failed: T[] = []
  for (let start = 0; start < items.length; start += batchSize) {
    const batch = items.slice(start, start + batchSize)
    const results = await Promise.allSettled(batch.map(fn))
    results.forEach((r, i) => {
      if (r.status === 'rejected') failed.push(batch[i])
    })
  }
  return failed
}
