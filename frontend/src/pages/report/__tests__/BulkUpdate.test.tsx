import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ReportProvider, useReportDispatch, useReportState } from '../ReportContext'
import { FailureCard } from '../FailureCard'
import { GroupSelectAll } from '../GroupSelectAll'
import { BulkUpdateBar } from '../BulkUpdateBar'
import { groupFailures, scopedGroups } from '@/lib/grouping'
import { reviewKey } from '@/lib/reviewKey'
import { reconcileSelection, selectedScopes, type SelectedGroup } from '../failureUpdates'
import type { AnalysisResult, FailureAnalysis, GroupedFailure } from '@/types'

// The real grouping behaviour, with `scopedGroups` wrapped in a spy so the
// reconciliation pass can be counted instead of timed.
vi.mock('@/lib/grouping', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/grouping')>()
  return { ...actual, scopedGroups: vi.fn(actual.scopedGroups) }
})

const { put, get, role } = vi.hoisted(() => ({ put: vi.fn(), get: vi.fn(), role: { current: 'reviewer' } }))
vi.mock('@/lib/api', () => ({ api: { get, put, post: vi.fn(), delete: vi.fn() }, extractApiDetail: () => null }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ role: role.current, isOperator: false, isAdmin: false, username: 'rev' }) }))

function failure(testName: string, signature: string): FailureAnalysis {
  return {
    id: `id-${testName}`,
    test_name: testName,
    error: 'boom',
    error_signature: signature,
    analysis: { classification: 'CODE ISSUE', pattern: '', affected_tests: [], details: '', artifacts_evidence: '' },
  }
}

// Two single-test failures plus one group of two tests sharing a signature.
const FAILURES = [failure('test-a', 'sig-a'), failure('test-b', 'sig-b'), failure('test-c1', 'sig-c'), failure('test-c2', 'sig-c')]

/** The report page always has a result loaded; the bulk bar reads the job id from it. */
function resultPayload(failures: FailureAnalysis[] = FAILURES) {
  return {
    result: {
      job_id: 'job-1', job_name: 'test-job', build_number: 1, jenkins_url: null,
      status: 'completed' as const, summary: '', ai_provider: 'p', ai_model: 'm',
      failures, child_job_analyses: [],
    },
    createdAt: '', completedAt: '', analysisStartedAt: '',
  }
}

function SeedResult() {
  const dispatch = useReportDispatch()
  useEffect(() => { dispatch({ type: 'SET_RESULT', payload: resultPayload() }) }, [dispatch])
  return null
}

/** Stands in for the SSE status-changed refresh that re-dispatches SET_RESULT. */
function SseRefresh({ label = 'Simulate background refresh', failures }: { label?: string; failures?: FailureAnalysis[] }) {
  const dispatch = useReportDispatch()
  return <button onClick={() => dispatch({ type: 'SET_RESULT', payload: resultPayload(failures) })}>{label}</button>
}

function Harness({ seedGroupId, removedFailures, refreshedFailures }: { seedGroupId?: string; removedFailures?: FailureAnalysis[]; refreshedFailures?: FailureAnalysis[] }) {
  const groups = groupFailures(FAILURES)
  return (
    <MemoryRouter>
      <TooltipProvider delayDuration={0}>
        <ReportProvider>
          <SeedResult />
          <SseRefresh />
          {removedFailures && (
            <SseRefresh label="Simulate refresh without removed failures" failures={removedFailures} />
          )}
          {refreshedFailures && (
            <SseRefresh label="Simulate refresh with changed failures" failures={refreshedFailures} />
          )}
          <GroupSelectAll groups={groups} scopeLabel="Failures" />
          {groups.map((g, i) => (
            <FailureCard key={g.id} group={g} jobId="job-1" index={i} />
          ))}
          {seedGroupId && <SeedSelection groupId={seedGroupId} groups={groups} />}
          <BulkUpdateBar />
        </ReportProvider>
      </TooltipProvider>
    </MemoryRouter>
  )
}

/** Pre-select a group without clicking — used to prove the bulk bar hides from viewers. */
function SeedSelection({ groupId, groups }: { groupId: string; groups: ReturnType<typeof groupFailures> }) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    const g = groups.find((x) => x.id === groupId)
    if (g) dispatch({ type: 'TOGGLE_GROUP_SELECTION', payload: { id: g.id, testNames: g.tests.map((t) => t.test_name), childJobName: '', childBuildNumber: 0 } })
  }, [dispatch, groupId, groups])
  return null
}

function renderHarness(props: { seedGroupId?: string; removedFailures?: FailureAnalysis[]; refreshedFailures?: FailureAnalysis[] } = {}) {
  render(<Harness {...props} />)
  return userEvent.setup()
}

async function confirmBulkAction(user: ReturnType<typeof userEvent.setup>, action: string, confirmLabel = action) {
  await user.click(screen.getByRole('button', { name: action }))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getByRole('button', { name: confirmLabel }))
}

/** The confirm dialog is modal, so the page behind it is aria-hidden while a run
 *  is in flight — query it with `hidden` to drive a selection change underneath. */
function behindDialog(name: string, role: 'checkbox' | 'button') {
  return screen.getByRole(role, { name, hidden: true })
}

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

beforeEach(() => {
  sessionStorage.clear()
  role.current = 'reviewer'
  put.mockReset()
  get.mockReset()
  put.mockResolvedValue({ reviewed_by: 'rev' })
  get.mockResolvedValue({ tracked_in: {} })
})

/** One row of the reconciliation table: a selection the user left against
 *  `before`, the report it is reconciled against (`after`), and optionally a
 *  further refresh (`second`). `expected` is the selection after reconciliation —
 *  which entry survived, which names it holds and whether it stays narrowed. */
interface ReconcileTransition {
  name: string
  selection: SelectedGroup[]
  after: AnalysisResult
  second?: AnalysisResult
  expected: { tests: string[]; narrowed: boolean }[]
}

/** Report holding exactly these failures, no child jobs. */
function report(...failures: FailureAnalysis[]): AnalysisResult {
  return resultPayload(failures).result
}

/** A selection entry as a card toggle builds it: the id of the live group that
 *  holds `idFrom`, holding `idFrom` (or the narrowed `testNames`). */
function selected(before: AnalysisResult, idFrom: string[], over: Partial<SelectedGroup> = {}): SelectedGroup {
  const g = scopedGroups(before).find((x) => x.tests.some((t) => idFrom.includes(t.test_name)))
  if (!g) throw new Error(`no live group holds ${idFrom.join(',')}`)
  return { id: g.id, testNames: idFrom, ...over }
}

/** Every way the live membership of a selection can change between two refreshes.
 *  Table-driven on purpose: a hand-picked set is always one transition short —
 *  the merged-signature case shipped while split, add, narrow and remove all
 *  passed. Each row asserts two properties instead of one expected value:
 *  exactly one entry per (scope, liveGroupId), and a bulk count equal to the
 *  number of distinct tests sent. */
const RECONCILE_TRANSITIONS: ReconcileTransition[] = [
  {
    // One selected signature spreads over two live groups: one entry holding the
    // union, so the split half keeps its own override request later.
    name: 'split: one selected group fans out over two live groups',
    selection: [selected(report(failure('test-a', 'sig-x'), failure('test-b', 'sig-x')), ['test-a', 'test-b'])],
    after: report(failure('test-a', 'sig-x'), failure('test-b', 'sig-y')),
    expected: [{ tests: ['test-a', 'test-b'], narrowed: false }],
  },
  {
    // Two separately selected groups now share a signature. One live group, so
    // one entry: counting both would double every test.
    name: 'merge: two selected groups become one live group',
    selection: (() => {
      const before = report(failure('test-a', 'sig-a'), failure('test-b', 'sig-b'))
      return [selected(before, ['test-a']), selected(before, ['test-b'])]
    })(),
    after: report(failure('test-a', 'sig-a'), failure('test-b', 'sig-a')),
    expected: [{ tests: ['test-a', 'test-b'], narrowed: false }],
  },
  {
    // A joining test is picked up: an un-narrowed entry tracks live members.
    name: 'add: a test joins the selected signature',
    selection: (() => {
      const before = report(failure('test-a', 'sig-a'), failure('test-b', 'sig-b'))
      return [selected(before, ['test-a']), selected(before, ['test-b'])]
    })(),
    after: report(failure('test-a', 'sig-a'), failure('test-b', 'sig-b'), failure('test-c', 'sig-a')),
    expected: [
      { tests: ['test-a', 'test-c'], narrowed: false },
      { tests: ['test-b'], narrowed: false },
    ],
  },
  {
    // A removed test drops out; the empty entry disappears with it.
    name: 'remove: a selected test is gone from the report',
    selection: (() => {
      const before = report(failure('test-a', 'sig-a'), failure('test-b', 'sig-b'), failure('test-c', 'sig-b'))
      return [selected(before, ['test-a']), selected(before, ['test-b', 'test-c'])]
    })(),
    after: report(failure('test-a', 'sig-a'), failure('test-c', 'sig-b')),
    expected: [
      { tests: ['test-a'], narrowed: false },
      { tests: ['test-c'], narrowed: false },
    ],
  },
  {
    // A member of the selected group is replaced by a new test with the same
    // signature: the entry tracks the live members, so the newcomer is picked up.
    name: 'replace: a member of the selected signature is replaced',
    selection: [selected(report(failure('test-a', 'sig-x'), failure('test-b', 'sig-x')), ['test-a', 'test-b'])],
    after: report(failure('test-a', 'sig-x'), failure('test-z', 'sig-x')),
    expected: [{ tests: ['test-a', 'test-z'], narrowed: false }],
  },
  {
    // The retry leftover: one name of a two-test group, and it stays that way.
    name: 'narrow: a partial-failure retry leaves one test of the group',
    selection: [selected(report(failure('test-a', 'sig-a'), failure('test-b', 'sig-a')), ['test-a', 'test-b'], {
      testNames: ['test-b'], narrowed: true,
    })],
    after: report(failure('test-a', 'sig-a'), failure('test-b', 'sig-a')),
    expected: [{ tests: ['test-b'], narrowed: true }],
  },
  {
    // The same narrow, then two more refreshes that move the selected test into
    // another group and grow that group: still one name, still narrowed.
    name: 're-widen-after-narrow: further refreshes never re-widen it',
    selection: [selected(report(failure('test-a', 'sig-a'), failure('test-b', 'sig-b')), ['test-b'], { narrowed: true })],
    after: report(failure('test-a', 'sig-a'), failure('test-b', 'sig-a')),
    second: report(failure('test-a', 'sig-a'), failure('test-b', 'sig-a'), failure('test-c', 'sig-a'), failure('test-d', 'sig-b')),
    expected: [{ tests: ['test-b'], narrowed: true }],
  },
  {
    // The case the narrowed exemption let through: two groups narrowed by two
    // separate retries, then merged. Exempt from re-widening, never from folding.
    name: 'two-narrowed-entries-merge: two retry-narrowed groups share a signature',
    selection: (() => {
      const before = report(failure('test-a', 'sig-a'), failure('test-b', 'sig-b'))
      return [
        selected(before, ['test-a'], { testNames: ['test-a'], narrowed: true }),
        selected(before, ['test-b'], { testNames: ['test-b'], narrowed: true }),
      ]
    })(),
    after: report(failure('test-a', 'sig-a'), failure('test-b', 'sig-a')),
    expected: [{ tests: ['test-a', 'test-b'], narrowed: true }],
  },
  {
    // Mixed contributors: the un-narrowed entry tracks the live members, the
    // narrowed one is not re-widened, and one entry results either way.
    name: 'merge of a narrowed and an un-narrowed entry',
    selection: (() => {
      const before = report(failure('test-a', 'sig-a'), failure('test-b', 'sig-b'), failure('test-c', 'sig-b'))
      return [
        selected(before, ['test-a']),
        selected(before, ['test-b', 'test-c'], { testNames: ['test-b'], narrowed: true }),
      ]
    })(),
    after: report(failure('test-a', 'sig-a'), failure('test-b', 'sig-a'), failure('test-c', 'sig-b')),
    expected: [{ tests: ['test-a', 'test-b'], narrowed: true }],
  },
  {
    // Same signature, different child jobs: scope keeps them apart, so the
    // buckets do too. Two entries, one per (scope, liveGroupId).
    name: 'the same signature in two child jobs stays two entries',
    selection: [
      { id: 'top', testNames: ['test-a'] },
      { id: 'child', testNames: ['test-b'], childJobName: 'child', childBuildNumber: 7 },
    ],
    after: {
      ...report(failure('test-a', 'sig-a'), failure('test-c', 'sig-a')),
      child_job_analyses: [{
        id: 'child-1', job_name: 'child', build_number: 7, jenkins_url: null, summary: null,
        note: null, failed_children: [], failures: [failure('test-b', 'sig-a')],
      }],
    },
    expected: [
      { tests: ['test-a', 'test-c'], narrowed: false },
      { tests: ['test-b'], narrowed: false },
    ],
  },
]

// -- Property test: reconciliation over GENERATED selections ---------------
//
// The 10 rows above are a regression net, not a proof: a hand-listed set always
// has one more transition nobody thought of, and that is exactly how both the
// bridging fold and the cross-scope merge shipped with every row green. So the
// invariants are also asserted over generated cases — random selections, random
// refreshes, several scopes, failures with and without ids — where the shape of
// the transition is nobody's job to enumerate.

/** Deterministic PRNG: a failing case is reproducible from its seed. */
function seeded(seed: number) {
  let s = seed >>> 0
  return () => {
    s = (s + 0x6d2b79f5) >>> 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

type Scope = { childJobName: string; childBuildNumber: number }

/** Top level plus up to two child jobs — distinct name AND build, so a scope
 *  never aliases another and the fold has to keep them apart itself. */
function genScopes(rand: () => number): Scope[] {
  const scopes: Scope[] = [{ childJobName: '', childBuildNumber: 0 }]
  for (const [name, build] of [['job-a', 7], ['job-b', 9]] as const) {
    if (rand() < 0.6) scopes.push({ childJobName: name, childBuildNumber: build })
  }
  return scopes
}

/** A failure with `id: ''` is a legacy row: `groupFailures` falls back to the
 *  signature-derived group id, and `scopedGroups` gives EVERY scope the same
 *  `group-` prefix — so such a group id repeats across scopes. That is the input
 *  finding 2 lives on, so the generator must produce it. */
function genFailure(rand: () => number, testName: string, signature: string): FailureAnalysis {
  const f = failure(testName, signature)
  return rand() < 0.5 ? { ...f, id: '' } : f
}

function genReport(scopes: Scope[], lists: FailureAnalysis[][]): AnalysisResult {
  return {
    ...resultPayload([]).result,
    failures: lists[0] ?? [],
    child_job_analyses: scopes.slice(1).map((s, i) => ({
      id: `child-${s.childJobName}`, job_name: s.childJobName, build_number: s.childBuildNumber,
      jenkins_url: null, summary: null, note: null, failed_children: [], failures: lists[i + 1] ?? [],
    })),
  }
}

const NAMES = ['test-0', 'test-1', 'test-2', 'test-3', 'test-4']
const SIGS = ['sig-0', 'sig-1', 'sig-2']

const pick = <T,>(rand: () => number, xs: T[]): T => xs[Math.floor(rand() * xs.length)]

/** `count` distinct test names. A real report has no test name twice in one
 *  scope — only across scopes — so the generator keeps it that way and lets the
 *  SAME name recur in several scopes, which is what makes two scopes collide. */
function distinctNames(rand: () => number, count: number): string[] {
  return [...NAMES].sort(() => rand() - 0.5).slice(0, count)
}

interface Generated {
  scopes: Scope[]
  before: AnalysisResult
  selection: SelectedGroup[]
  live: AnalysisResult
}

/** One generated transition: a random selection over `before`, then a random
 *  refresh that merges signatures, splits them, adds, removes, and rewrites ids
 *  (so a refreshed group id may be signature-derived instead of UUID-derived). */
function generate(seed: number): Generated {
  const rand = seeded(seed)
  const scopes = genScopes(rand)
  const before = genReport(scopes, scopes.map(() => {
    const names = distinctNames(rand, 1 + Math.floor(rand() * 4))
    return names.map((n) => genFailure(rand, n, pick(rand, SIGS)))
  }))

  // A selection: most live groups picked, some retry-narrowed to a subset.
  const selection: SelectedGroup[] = []
  for (const s of scopes) {
    for (const grp of scopedGroups(before, s.childJobName, s.childBuildNumber)) {
      if (rand() < 0.35) continue
      const narrowed = rand() < 0.2
      selection.push({
        id: grp.id,
        testNames: narrowed ? [pick(rand, grp.tests).test_name] : grp.tests.map((t) => t.test_name),
        narrowed: narrowed || undefined,
        childJobName: s.childJobName || undefined,
        childBuildNumber: s.childBuildNumber || undefined,
      })
    }
  }

  // The refresh: re-signature (merge/split), drop, add, rewrite ids.
  const lists = scopes.map((_s, i) => {
    const failures = (i === 0 ? before.failures : (before.child_job_analyses[i - 1]?.failures ?? []))
      .filter(() => rand() > 0.15)
      .map((f) => ({ ...f, ...(rand() < 0.35 ? { error_signature: pick(rand, SIGS) } : {}), ...(rand() < 0.35 ? { id: '' } : {}) }))
    const free = NAMES.filter((n) => !failures.some((f) => f.test_name === n))
    return rand() < 0.5 && free.length
      ? [...failures, genFailure(rand, pick(rand, free), pick(rand, SIGS))]
      : failures
  })
  return { scopes, before, selection, live: genReport(scopes, lists) }
}

/** The four invariants, asserted after EVERY generated pass. */
function expectInvariants({ scopes, live }: Generated, state: Record<string, SelectedGroup>, label: string) {
  const groups = Object.values(state)
  if (groups.length === 0) return
  const msg = (m: string) => `${label}: ${m}`

  // P1 — a live group is claimed by at most one entry, within a scope.
  const claims = groups.flatMap((g) =>
    scopedGroups(live, g.childJobName, g.childBuildNumber)
      .filter((x) => x.tests.some((t) => g.testNames.includes(t.test_name)))
      .map((x) => `${g.childJobName ?? ''}#${g.childBuildNumber ?? 0}:${x.id}`))
  expect(new Set(claims).size, msg('P1 a live group is claimed twice')).toBe(claims.length)

  // P2 — the bulk count is the number of distinct tests actually sent.
  const sent = selectedScopes(groups)
  expect(new Set(sent.map((s) => reviewKey(s.testName, s.childJobName, s.childBuildNumber))).size,
    msg('P2 a test is sent twice')).toBe(sent.length)
  expect(sent.length, msg('P2 the count is not the number of tests sent')).toBe(groups.flatMap((g) => g.testNames).length)

  // P3 — an entry carries the scope of the live group(s) it claims. Every scope it
  // sends to must be a live scope holding that test, and a test that lives in
  // exactly one scope of the report pins that scope: an entry tagged with the
  // wrong child job sends its tests to the wrong job. This is what pins the
  // same-signature-across-scopes merge, where `scopedGroups` hands both scopes the
  // SAME group id and a bare-id fold keeps the last contributor's scope.
  const liveNames = scopes.map((s) => [s, new Set(groupNamesOf(scopedGroups(live, s.childJobName, s.childBuildNumber)))] as const)
  for (const g of groups) {
    const mine = new Set(groupNamesOf(scopedGroups(live, g.childJobName, g.childBuildNumber)))
    for (const n of g.testNames) {
      expect(mine.has(n), msg(`P3 ${n} is not in the scope the entry claims`)).toBe(true)
      const owners = liveNames.filter(([, names]) => names.has(n))
      if (owners.length === 1) {
        expect({ childJobName: g.childJobName ?? '', childBuildNumber: g.childBuildNumber ?? 0 },
          msg(`P3 ${n} lives in one scope but the entry claims another`)).toEqual(owners[0][0])
      }
    }
  }

  // P4 — the EMITTED KEY is a live group id in the entry's own scope, so a card
  // exists for it. Cards render `isSelected = !!selection[group.id]`: a key no
  // card answers to leaves the tests counted in the bulk bar with nothing ticked
  // on screen and nothing the user can untick. This is the merge case where the
  // refresh keeps the EARLIER selected group's id while the fold roots on the
  // later one.
  const liveIds = new Set(scopes.flatMap((s) =>
    scopedGroups(live, s.childJobName, s.childBuildNumber).map((x) => `${s.childJobName}#${s.childBuildNumber}:${x.id}`)))
  for (const [key, g] of Object.entries(state)) {
    expect(liveIds.has(`${g.childJobName ?? ''}#${g.childBuildNumber ?? 0}:${key}`),
      msg(`P4 ${key} is not a live group id, so no card can be unchecked`)).toBe(true)
  }
}

function groupNamesOf(groups: GroupedFailure[]): string[] {
  return groups.flatMap((x) => x.tests.map((t) => t.test_name))
}

describe('bulk failure selection', () => {
  it('bulk-marks individual selections as reviewed and clears the selection', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select test-b' }))

    // A selected group covers every test that shares its error signature.
    await user.click(screen.getByRole('checkbox', { name: 'Select test-c1' }))
    expect(screen.getByText(/4 tests selected in 3 failures/)).toBeInTheDocument()

    // The confirmation states the scope of the bulk action.
    await user.click(screen.getByRole('button', { name: 'Mark reviewed' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Apply to 4 tests in 3 failures?')
    await user.click(within(dialog).getByRole('button', { name: 'Mark reviewed' }))

    await waitFor(() => expect(put).toHaveBeenCalledTimes(4))
    for (const name of ['test-a', 'test-b', 'test-c1', 'test-c2']) {
      expect(put).toHaveBeenCalledWith('/results/job-1/reviewed', {
        test_name: name, reviewed: true, child_job_name: '', child_build_number: 0,
      })
    }
    // Every selected failure now shows the reviewed toggle state.
    await waitFor(() => expect(screen.queryByText(/selected in/)).not.toBeInTheDocument())
  })

  it('selects every listed failure with select-all and un-marks them when all are reviewed', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    expect(screen.getByText(/4 tests selected in 3 failures/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Mark reviewed' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Mark reviewed' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(4))

    // Re-select: all are reviewed, so the bar offers the reverse action.
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    await confirmBulkAction(user, 'Unmark reviewed')
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/results/job-1/reviewed', {
        test_name: 'test-c2', reviewed: false, child_job_name: '', child_build_number: 0,
      }),
    )
  })

  it('applies classification and pattern once per signature group, not once per test', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))

    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'PRODUCT BUG' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Apply' }))
    // Three groups, one request each — the backend propagates to the whole
    // signature group, so test-c2 must not get its own request.
    await waitFor(() => expect(put).toHaveBeenCalledTimes(3))
    expect(put).toHaveBeenCalledWith('/results/job-1/override-classification', {
      test_name: 'test-a', classification: 'PRODUCT BUG', child_job_name: '', child_build_number: 0,
    })
    expect(put).toHaveBeenCalledWith('/results/job-1/override-classification', {
      test_name: 'test-c1', classification: 'PRODUCT BUG', child_job_name: '', child_build_number: 0,
    })
    expect(put).not.toHaveBeenCalledWith('/results/job-1/override-classification', expect.objectContaining({ test_name: 'test-c2' }))

    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    await user.click(screen.getByRole('combobox', { name: 'Bulk pattern' }))
    await user.click(await screen.findByRole('option', { name: 'FLAKY' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(6))
    expect(put).toHaveBeenCalledWith('/results/job-1/override-pattern', {
      test_name: 'test-b', pattern: 'FLAKY', child_job_name: '', child_build_number: 0,
    })
  })

  it('links the whole selection to one issue and refreshes tracked-in links', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))

    await user.click(screen.getByRole('button', { name: 'Track in...' }))
    const urlDialog = await screen.findByRole('dialog')
    await user.type(within(urlDialog).getByLabelText('Issue URL'), 'https://github.com/org/repo/issues/7')
    await user.click(within(urlDialog).getByRole('button', { name: 'Continue' }))

    const confirmDialog = await screen.findByRole('dialog')
    expect(confirmDialog).toHaveTextContent('https://github.com/org/repo/issues/7')
    expect(confirmDialog).toHaveTextContent('1 test in 1 failure')
    await user.click(within(confirmDialog).getByRole('button', { name: 'Link' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/results/job-1/tracked-in', {
        test_name: 'test-a', url: 'https://github.com/org/repo/issues/7', type: 'github', child_job_name: '', child_build_number: 0,
      }),
    )
    expect(get).toHaveBeenCalledWith('/results/job-1/tracked-in')
  })

  it('reports partial failures and retries only the tests that failed', async () => {
    let failTestB = true
    put.mockImplementation(async (_path: string, body: { test_name: string }) => {
      if (body.test_name === 'test-b' && failTestB) throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    await confirmBulkAction(user, 'Mark reviewed')

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Failed to update 1 of 4: test-b'))
    // Only the failed test stays selected — the three that succeeded are gone.
    expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument()

    failTestB = false
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(put).toHaveBeenCalledTimes(5))
    expect(put).toHaveBeenLastCalledWith('/results/job-1/reviewed', {
      test_name: 'test-b', reviewed: true, child_job_name: '', child_build_number: 0,
    })
    await waitFor(() => expect(screen.queryByText(/selected in/)).not.toBeInTheDocument())
  })

  it('keeps a selection made while the bulk run is in flight', async () => {
    let release: () => void = () => {}
    put.mockImplementation(() => new Promise((resolve) => { release = () => resolve({ reviewed_by: 'rev' }) }))
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))
    await confirmBulkAction(user, 'Mark reviewed')

    fireEvent.click(behindDialog('Select test-b', 'checkbox'))
    release()
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    // test-a is done, but the selection made during the run survives.
    await waitFor(() => expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument())
  })

  it('keeps the selection through a background refresh and drops a stale dialog', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))

    // A status-changed SSE refresh re-dispatches SET_RESULT.
    await user.click(screen.getByRole('button', { name: 'Simulate background refresh' }))
    expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument()

    // An open dialog must not survive a cleared selection and act on stale scopes.
    await user.click(screen.getByRole('button', { name: 'Mark reviewed' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    fireEvent.click(behindDialog('Clear', 'button'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()
  })

  it('rejects an unusable issue URL before the bulk confirm', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))

    await user.click(screen.getByRole('button', { name: 'Track in...' }))
    const urlDialog = await screen.findByRole('dialog')
    await user.type(within(urlDialog).getByLabelText('Issue URL'), 'https://')
    expect(await within(urlDialog).findByText('Please enter a valid URL (e.g., https://github.com/org/repo/issues/123)')).toBeInTheDocument()
    expect(within(urlDialog).getByRole('button', { name: 'Continue' })).toBeDisabled()
    // Only the URL prompt is open — no bulk confirmation.
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    expect(put).not.toHaveBeenCalled()
  })

  it('reports saved links as applied when only the refresh fails', async () => {
    get.mockRejectedValue(new Error('boom'))
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))

    await user.click(screen.getByRole('button', { name: 'Track in...' }))
    const urlDialog = await screen.findByRole('dialog')
    await user.type(within(urlDialog).getByLabelText('Issue URL'), 'https://jira.example.com/browse/PROJ-1')
    await user.click(within(urlDialog).getByRole('button', { name: 'Continue' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Link' }))

    await waitFor(() => expect(put).toHaveBeenCalledWith('/results/job-1/tracked-in', expect.objectContaining({ test_name: 'test-a' })))
    // The write is not reported as a bulk failure — only the refresh is, and it
    // outlives the bar that the successful run removed.
    expect(await screen.findByRole('alert')).toHaveTextContent('Links saved, but refreshing the tracked links failed.')
    expect(screen.queryByRole('alert')).not.toHaveTextContent('Failed to update')
    expect(screen.queryByText(/selected in/)).not.toBeInTheDocument()
  })

  it('reports a failed link write even when the tracked-in refresh also fails', async () => {
    put.mockImplementation(async (path: string, body: { test_name: string }) => {
      if (path.endsWith('/tracked-in') && body.test_name === 'test-b') throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    get.mockRejectedValue(new Error('boom'))
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))

    await user.click(screen.getByRole('button', { name: 'Track in...' }))
    const urlDialog = await screen.findByRole('dialog')
    await user.type(within(urlDialog).getByLabelText('Issue URL'), 'https://jira.example.com/browse/PROJ-1')
    await user.click(within(urlDialog).getByRole('button', { name: 'Continue' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Link' }))

    // Both problems are reported: the link that was never written and the refresh.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Failed to update 1 of 4: test-b')
    expect(alert).toHaveTextContent('Refreshing the tracked links also failed.')
    // Only the link that was not written stays selected for a retry.
    expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument()
  })

  it('prunes a removed failure from the selection on a background refresh', async () => {
    // The refreshed report dropped test-b entirely and split the sig-c group.
    const user = renderHarness({ removedFailures: [FAILURES[0], FAILURES[3]] })
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    expect(screen.getByText(/4 tests selected in 3 failures/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Simulate refresh without removed failures' }))
    expect(screen.getByText(/2 tests selected in 2 failures/)).toBeInTheDocument()

    // The retained names are the live ones — a mutation must not send a dead name.
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    for (const name of ['test-a', 'test-c2']) {
      expect(put).toHaveBeenCalledWith('/results/job-1/reviewed', {
        test_name: name, reviewed: true, child_job_name: '', child_build_number: 0,
      })
    }
    expect(put).not.toHaveBeenCalledWith('/results/job-1/reviewed', expect.objectContaining({ test_name: 'test-b' }))
  })

  it('picks up a test that joined the selected group on a background refresh', async () => {
    // The refreshed report has a new test-c3 failing with the sig-c signature the
    // selection already covers. The group id is unchanged — only membership grew.
    const user = renderHarness({ refreshedFailures: [...FAILURES, failure('test-c3', 'sig-c')] })
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))
    expect(screen.getByText(/4 tests selected in 3 failures/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Simulate refresh with changed failures' }))
    // The entry was not narrowed by a retry, so it re-widens to the live members.
    expect(screen.getByText(/5 tests selected in 3 failures/)).toBeInTheDocument()

    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(put).toHaveBeenCalledTimes(5))
    // The new test is in the next request, not just in the count.
    expect(put).toHaveBeenCalledWith('/results/job-1/reviewed', {
      test_name: 'test-c3', reviewed: true, child_job_name: '', child_build_number: 0,
    })
  })

  it('does not re-widen a retry-narrowed selection on a later refresh', async () => {
    // The opposite direction of the rule above, on the same refresh: a
    // partial-failure retry is the only thing that sets `narrowed`.
    put.mockImplementation(async (path: string, body: { test_name: string }) => {
      if (path.endsWith('/reviewed') && body.test_name === 'test-c2') throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    const user = renderHarness({ refreshedFailures: [...FAILURES, failure('test-c3', 'sig-c')] })
    await user.click(screen.getByRole('checkbox', { name: 'Select test-c1' }))
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument())

    // Identical refresh, narrowed entry: the retry leftover stands, so neither
    // test-c1 (already done) nor test-c3 (newly joined) is dragged back in.
    await user.click(screen.getByRole('button', { name: 'Simulate refresh with changed failures' }))
    expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument()

    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(put).toHaveBeenCalledTimes(3))
    expect(put).toHaveBeenLastCalledWith('/results/job-1/reviewed', {
      test_name: 'test-c2', reviewed: true, child_job_name: '', child_build_number: 0,
    })
  })

  it('reconciles a selection in one grouping pass per scope, not one pass per group', () => {
    // One report, two scopes: the top level and one child job.
    const result: AnalysisResult = {
      ...resultPayload(FAILURES).result,
      child_job_analyses: [{
        id: 'child-1', job_name: 'child', build_number: 7, jenkins_url: null, summary: null,
        note: null, failed_children: [], failures: [failure('test-d1', 'sig-d')],
      }],
    }
    // Four selected groups over those two scopes: three top-level, one child.
    const groups: SelectedGroup[] = [
      { id: 'g-a', testNames: ['test-a'] },
      { id: 'g-b', testNames: ['test-b'] },
      { id: 'g-c', testNames: ['test-c1', 'test-c2'] },
      { id: 'g-d', testNames: ['test-d1'], childJobName: 'child', childBuildNumber: 7 },
    ]
    const spy = vi.mocked(scopedGroups)
    spy.mockClear()

    const next = reconcileSelection(result, Object.fromEntries(groups.map((g) => [g.id, g])))
    // The count is the claim: 4 groups, 2 calls — once per DISTINCT scope. Before
    // the per-pass memo this was one full grouping walk per selected group.
    expect(spy).toHaveBeenCalledTimes(2)
    expect(Object.values(next).flatMap((g) => g.testNames)).toEqual([
      'test-a', 'test-b', 'test-c1', 'test-c2', 'test-d1',
    ])
  })

  it.each(RECONCILE_TRANSITIONS)('$name', ({ selection, after, second, expected }) => {
    // Reconcile once, and a second time when the row carries a further refresh —
    // the fold must be idempotent.
    let live: AnalysisResult = after
    let state = reconcileSelection(live, Object.fromEntries(selection.map((g) => [g.id, g])))
    if (second) {
      live = second
      state = reconcileSelection(live, state)
    }
    const groups = Object.values(state)

    // PROPERTY 1: one entry per (scope, liveGroupId), whatever the mix of
    // narrowed inputs. A merged signature claimed twice is the defect.
    const claims = groups.flatMap((g) =>
      scopedGroups(live, g.childJobName, g.childBuildNumber)
        .filter((x) => x.tests.some((t) => g.testNames.includes(t.test_name)))
        .map((x) => `${g.childJobName ?? ''}#${g.childBuildNumber ?? 0}:${x.id}`),
    )
    expect(claims.length).toBeGreaterThan(0)
    expect([...new Set(claims)]).toHaveLength(claims.length)

    // PROPERTY 2: the bulk count is the number of DISTINCT tests actually sent.
    const sent = selectedScopes(groups)
    expect(new Set(sent.map((s) => reviewKey(s.testName, s.childJobName, s.childBuildNumber))).size).toBe(sent.length)
    expect(sent.length).toBe(groups.flatMap((g) => g.testNames).length)

    // Membership source and the `narrowed` flag, which keepSelectionAfterRun reads.
    expect(groups.map((g) => ({ tests: g.testNames, narrowed: !!g.narrowed }))).toEqual(expected)
  })

  it('holds the reconciliation invariants over 400 generated refreshes', () => {
    // Every generated case, twice: the first pass folds a random selection
    // against a random refresh, the second re-reconciles the folded state (the
    // fold must be idempotent — a second pass is another refresh).
    for (let seed = 1; seed <= 400; seed++) {
      const c = generate(seed)
      const state = reconcileSelection(c.live, Object.fromEntries(c.selection.map((g) => [g.id, g])))
      expectInvariants(c, state, `seed ${seed} pass 1`)
      expectInvariants(c, reconcileSelection(c.live, state), `seed ${seed} pass 2`)
    }
  })

  it('sends one override request per group when a refresh splits the selected signature', async () => {
    // The refreshed report gives test-c2 a signature of its own, splitting the
    // selected sig-c group across two live groups.
    const user = renderHarness({
      refreshedFailures: [FAILURES[0], FAILURES[1], failure('test-c1', 'sig-c'), failure('test-c2', 'sig-c-split')],
    })
    await user.click(screen.getByRole('checkbox', { name: 'Select test-c1' }))
    expect(screen.getByText(/2 tests selected in 1 failure/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Simulate refresh with changed failures' }))
    // Reconciled membership is the union of both halves, still under the one id.
    expect(screen.getByText(/2 tests selected in 1 failure/)).toBeInTheDocument()

    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'PRODUCT BUG' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('every test sharing the error signature — 2 tests in 2 failures')
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Apply' }))

    // Both halves get their own request — the moved test is not silently dropped.
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    for (const name of ['test-c1', 'test-c2']) {
      expect(put).toHaveBeenCalledWith('/results/job-1/override-classification', {
        test_name: name, classification: 'PRODUCT BUG', child_job_name: '', child_build_number: 0,
      })
    }
    // ...and both were covered, so neither half is left stranded in the selection.
    await waitFor(() => expect(screen.queryByText(/selected in/)).not.toBeInTheDocument())
  })

  // PRODUCT DECISION PENDING — this test pins CONTESTED display behaviour, not
  // settled correctness. It exists so the disagreement is visible and so the fix
  // is a one-line change once the product owner decides which number is right.
  //
  // A selected signature group splits across two live groups on refresh. The bulk
  // BAR counts selection entries (BulkUpdateBar.tsx:273 renders
  // `{scopes.length} tests selected in {groups.length} failures`, groups = entries)
  // and therefore says ONE failure. The CONFIRMATION DIALOG counts live groups
  // after widening and says TWO. The same fact is reported two ways on one screen.
  //
  // The argument for "2 failures" being correct: the user checked one failure, the
  // refresh split it into two, and they now own two. Under-reporting the blast
  // radius of a bulk mutation is the worse error — the bar exists to tell the user
  // how wide the action they are about to confirm is.
  //
  // If the product owner decides the bar should say 2, the fix is to hold one
  // selection entry per live group after reconciliation (the stronger invariant),
  // and this expectation flips from 1 to 2. Nothing else in the test changes.
  it('DISPLAY: a split group is reported as 1 failure by the bar and 2 by the dialog', async () => {
    const user = renderHarness({
      refreshedFailures: [FAILURES[0], FAILURES[1], failure('test-c1', 'sig-c'), failure('test-c2', 'sig-c-split')],
    })
    await user.click(screen.getByRole('checkbox', { name: 'Select test-c1' }))
    await user.click(screen.getByRole('button', { name: 'Simulate refresh with changed failures' }))

    // What the user reads on the bar.
    expect(screen.getByText(/2 tests selected in 1 failure/)).toBeInTheDocument()

    // What the user reads when asked to confirm the blast radius.
    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'PRODUCT BUG' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('every test sharing the error signature — 2 tests in 2 failures')
  })

  it('collapses a card toggle that re-selects half of a split group into one override request', async () => {
    // A split leaves the entry spanning both live groups under the ORIGINAL id, so
    // ticking one of the two cards is a SECOND entry for a live group already in
    // the selection — a toggle is never reconciled. `widenToSignatureGroups` is what
    // turns that back into one request per live group.
    const refreshed = [FAILURES[0], FAILURES[1], failure('test-c1', 'sig-c'), failure('test-c2', 'sig-c-split')]
    function LiveCards() {
      const { result } = useReportState()
      return <>{groupFailures(result?.failures ?? []).map((g, i) => (
        <FailureCard key={g.id} group={g} jobId="job-1" index={i} />
      ))}</>
    }
    render(
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>
          <ReportProvider>
            <SeedResult />
            <SseRefresh label="Split" failures={refreshed} />
            <LiveCards />
            <BulkUpdateBar />
          </ReportProvider>
        </TooltipProvider>
      </MemoryRouter>,
    )
    const user = userEvent.setup()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-c1' }))
    expect(screen.getByText(/2 tests selected in 1 failure/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Split' }))
    expect(screen.getByText(/2 tests selected in 1 failure/)).toBeInTheDocument()

    fireEvent.click(behindDialog('Select test-c2', 'checkbox'))
    // test-c2 is now counted twice: the split entry and the toggled half.
    await waitFor(() => expect(screen.getByText(/3 tests selected in 2 failures/)).toBeInTheDocument())

    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'PRODUCT BUG' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Apply' }))
    // ...but the duplicate collapses: one request per live group, not per entry.
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
  })

  it('applies an override to the whole signature group after the selection was narrowed', async () => {
    // Narrow the sig-c group down to a single test with a failed review run.
    put.mockImplementation(async (path: string, body: { test_name: string }) => {
      if (path.endsWith('/reviewed') && body.test_name === 'test-c2') throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-c1' }))
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(screen.getByText(/1 test selected in 1 failure/)).toBeInTheDocument())

    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'INFRASTRUCTURE' }))
    const dialog = await screen.findByRole('dialog')
    // The widened scope is stated, not silently applied.
    expect(dialog).toHaveTextContent('every test sharing the error signature — 2 tests in 1 failure')
    await user.click(within(dialog).getByRole('button', { name: 'Apply' }))

    // One request for the group (the backend propagates it to both tests), but
    // the optimistic patch now covers the unselected sibling.
    await waitFor(() => expect(put).toHaveBeenLastCalledWith('/results/job-1/override-classification', {
      test_name: 'test-c1', classification: 'INFRASTRUCTURE', child_job_name: '', child_build_number: 0,
    }))
    const card = within(document.getElementById('group-id-test-c1') as HTMLElement)
    expect(card.getByText('INFRASTRUCTURE')).toBeInTheDocument()
    expect(card.queryByText('CODE ISSUE')).toBeNull()
  })

  it('sends only the live scope when a refresh removes a selected test mid-confirmation', async () => {
    // The refreshed report dropped test-b; test-a and the sig-c group survive.
    const user = renderHarness({ removedFailures: [FAILURES[0], FAILURES[2], FAILURES[3]] })
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))

    await user.click(screen.getByRole('button', { name: 'Mark reviewed' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Apply to 4 tests in 3 failures?')

    // A status-changed refresh lands while the confirmation is open.
    fireEvent.click(behindDialog('Simulate refresh without removed failures', 'button'))
    await waitFor(() => expect(screen.getByRole('dialog')).toHaveTextContent('Apply to 3 tests in 2 failures?'))
    expect(screen.getByText(/3 tests selected in 2 failures/)).toBeInTheDocument()

    // The text is what gets sent: three requests, and never the removed test.
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Mark reviewed' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(3))
    for (const name of ['test-a', 'test-c1', 'test-c2']) {
      expect(put).toHaveBeenCalledWith('/results/job-1/reviewed', {
        test_name: name, reviewed: true, child_job_name: '', child_build_number: 0,
      })
    }
    expect(put).not.toHaveBeenCalledWith('/results/job-1/reviewed', expect.objectContaining({ test_name: 'test-b' }))
  })

  it('re-derives a widened override scope after a refresh removes a selected test', async () => {
    // The refreshed report holds test-a and test-c1: test-b is gone and the
    // sig-c group lost its sibling.
    const user = renderHarness({ removedFailures: [FAILURES[0], FAILURES[2]] })
    await user.click(screen.getByRole('checkbox', { name: 'Select all failures in Failures' }))

    await user.click(screen.getByRole('combobox', { name: 'Bulk classification' }))
    await user.click(await screen.findByRole('option', { name: 'PRODUCT BUG' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('every test sharing the error signature — 4 tests in 3 failures')

    fireEvent.click(behindDialog('Simulate refresh without removed failures', 'button'))
    // The widening is re-computed against the refreshed result: sig-c no longer
    // has a sibling to widen to, so the scope is the two surviving tests.
    await waitFor(() =>
      expect(screen.getByRole('dialog')).toHaveTextContent('every test sharing the error signature — 2 tests in 2 failures'),
    )

    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put).toHaveBeenCalledWith('/results/job-1/override-classification', {
      test_name: 'test-a', classification: 'PRODUCT BUG', child_job_name: '', child_build_number: 0,
    })
    expect(put).toHaveBeenCalledWith('/results/job-1/override-classification', {
      test_name: 'test-c1', classification: 'PRODUCT BUG', child_job_name: '', child_build_number: 0,
    })
  })

  it('clears the previous bulk error when the selection is cleared', async () => {
    put.mockImplementation(async (_path: string, body: { test_name: string }) => {
      if (body.test_name === 'test-b') throw new Error('boom')
      return { reviewed_by: 'rev' }
    })
    const user = renderHarness()
    await user.click(screen.getByRole('checkbox', { name: 'Select test-b' }))
    await confirmBulkAction(user, 'Mark reviewed')
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Failed to update 1 of 1: test-b'))

    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    await user.click(screen.getByRole('checkbox', { name: 'Select test-a' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('hides selection checkboxes and the bulk bar from viewers', async () => {
    role.current = 'viewer'
    const groups = groupFailures(FAILURES)
    renderHarness({ seedGroupId: groups[0].id })

    expect(screen.queryByRole('checkbox', { name: 'Select all failures in Failures' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Select test-a' })).not.toBeInTheDocument()
    expect(screen.queryByText(/selected in/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mark reviewed' })).not.toBeInTheDocument()
  })

  it('hides every review control from viewers', async () => {
    role.current = 'viewer'
    const user = renderHarness()

    // Same read-only rule as the selection checkbox and the bulk bar above: no
    // control is rendered at all, so nothing can start the 403-guarded review edit.
    await user.click(screen.getByRole('button', { name: /test-c1/ }))
    for (const name of ['Review', 'Review 0/2', 'Review All (0/2)']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(put).not.toHaveBeenCalled()
  })

  it('keeps the existing signature-scoped "Review all" behaviour', async () => {
    const user = renderHarness()
    await user.click(screen.getByRole('button', { name: /test-c1/ }))
    await user.click(screen.getByRole('button', { name: 'Review All (0/2)' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put).toHaveBeenCalledWith('/results/job-1/reviewed', {
      test_name: 'test-c2', reviewed: true, child_job_name: '', child_build_number: 0,
    })
    expect(screen.queryByText(/selected in/)).not.toBeInTheDocument()
  })
})
