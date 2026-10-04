import { describe, expect, it } from 'vitest'
import {
  analyzedFailingTests,
  peerGroups,
  primaryGroups,
  spendAverages,
  stageBreakdown,
} from '../aiSpend'
import type {
  AnalysisResult,
  ChildJobAnalysis,
  FailureAnalysis,
  PeerDebate,
  TokenUsageEntry,
  TokenUsageSummary,
} from '@/types'

const call = (over: Partial<TokenUsageEntry> = {}): TokenUsageEntry => ({
  provider: 'gemini', model: 'pro', call_type: 'primary',
  input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0,
  total_tokens: 15, cost_usd: 0.02, duration_ms: 100, success: true,
  ...over,
})

const summary = (over: Partial<TokenUsageSummary> = {}): TokenUsageSummary => ({
  total_input_tokens: 10, total_output_tokens: 5, total_cache_read_tokens: 0,
  total_cache_write_tokens: 0, total_tokens: 15, total_cost_usd: 0.02,
  total_duration_ms: 100, total_calls: 1, calls: [call()], ...over,
})

const failure = (over: Partial<FailureAnalysis> = {}): FailureAnalysis => ({
  id: 'f1', test_name: 'test_a', error: 'boom', analysis: {} as FailureAnalysis['analysis'],
  error_signature: 'sig-a', ...over,
})

const child = (over: Partial<ChildJobAnalysis> = {}): ChildJobAnalysis => ({
  id: 'c1', job_name: 'child', build_number: 7, jenkins_url: null, summary: null,
  failures: [], failed_children: [], note: null, ...over,
})

const result = (over: Partial<AnalysisResult> = {}): AnalysisResult => ({
  job_id: 'j1', job_name: 'job', build_number: 1, jenkins_url: null, status: 'completed',
  summary: '', ai_provider: 'gemini', ai_model: 'pro', failures: [], child_job_analyses: [],
  ...over,
})

const usageSummary = (calls: TokenUsageEntry[], extra: Partial<TokenUsageSummary> = {}) =>
  summary({ calls, total_calls: calls.length, ...extra })

describe('stageBreakdown', () => {
  it('groups every recorded call type and keeps cache tokens out of the billed total', () => {
    const stages = stageBreakdown(usageSummary([
      call({ call_type: 'primary', input_tokens: 10, output_tokens: 5, cache_read_tokens: 7, cache_write_tokens: 3, total_tokens: 15 }),
      call({ call_type: 'peer', input_tokens: 1, output_tokens: 1, total_tokens: 2, cost_usd: 0.01 }),
      call({ call_type: 'agent_routing', input_tokens: 2, output_tokens: 2, total_tokens: 4, cost_usd: 0.03 }),
      call({ call_type: 'cross_failure', input_tokens: 5, output_tokens: 5, total_tokens: 10, cost_usd: 0.05 }),
    ], { total_cost_usd: 0.11 }))

    expect(stages.map(s => s.callType)).toEqual(['primary', 'peer', 'agent_routing', 'cross_failure'])
    const primary = stages[0]
    expect(primary.calls).toBe(1)
    expect(primary.inputTokens).toBe(10)
    expect(primary.outputTokens).toBe(5)
    expect(primary.cacheReadTokens).toBe(7)
    expect(primary.cacheWriteTokens).toBe(3)
    // cache tokens are reported apart, never folded into the billed total
    expect(primary.totalTokens).toBe(15)
    expect(primary.costUsd).toBeCloseTo(0.02)
  })

  it('counts a card rendered many times once per stored call', () => {
    const one = call()
    expect(stageBreakdown(usageSummary([one, one, one]))[0].calls).toBe(3)
  })

  it('reports stage cost as a lower bound when any call cost is unknown', () => {
    const [stage] = stageBreakdown(usageSummary([
      call({ cost_usd: 0.02 }),
      call({ cost_usd: null }),
    ]))
    // The unpriced call counts as $0 but is disclosed, not discarded.
    expect(stage.costUsd).toBeCloseTo(0.02)
    expect(stage.partial).toBe(true)
  })

  it('keeps a genuine zero cost as zero', () => {
    const [stage] = stageBreakdown(usageSummary([call({ cost_usd: 0 })]))
    expect(stage.costUsd).toBe(0)
    expect(stage.partial).toBe(false)
  })

  it('attributes failed-call waste and flags unknown outcomes', () => {
    const [known] = stageBreakdown(usageSummary([
      call({ call_type: 'primary', success: true, cost_usd: 0.02 }),
      call({ call_type: 'primary', success: false, cost_usd: 0.05 }),
    ]))
    expect(known.failedCalls).toBe(1)
    expect(known.outcomeKnown).toBe(true)
    expect(known.failedCostUsd).toBeCloseTo(0.05)

    const [missingOutcome] = stageBreakdown(usageSummary([
      call({ call_type: 'reanalysis', success: undefined, cost_usd: 0.01 }),
    ]))
    expect(missingOutcome.failedCalls).toBe(0)
    expect(missingOutcome.outcomeKnown).toBe(false)
    expect(missingOutcome.failedCostUsd).toBe(0)
  })

  it('marks a stage whose cost is a lower bound', () => {
    const [stage] = stageBreakdown(usageSummary([
      call({ cost_usd: 0.02, cost_partial: true }),
      call({ cost_usd: 0.01 }),
    ]))
    expect(stage.partial).toBe(true)
    expect(stage.costUsd).toBeCloseTo(0.03)
  })

  it('is not partial when no call was flagged', () => {
    const [stage] = stageBreakdown(usageSummary([call()]))
    expect(stage.partial).toBe(false)
  })

  it('has no stages at all for a legacy result without usage', () => {
    expect(stageBreakdown(null)).toEqual([])
    expect(stageBreakdown(undefined)).toEqual([])
  })
})

describe('primaryGroups', () => {
  it('groups by signature, counts shared failures, and keeps child scopes distinct', () => {
    const groups = primaryGroups(result({
      failures: [
        failure({ test_name: 'test_a' }),
        failure({ test_name: 'test_b', token_usage: summary() }),
      ],
      child_job_analyses: [
        child({
          failures: [
            failure({ test_name: 'test_c', token_usage: summary({ total_cost_usd: 0.5 }) }),
            failure({ test_name: 'test_d' }),
          ],
        }),
      ],
    }))

    expect(groups).toHaveLength(2)
    const top = groups.find(g => g.childLabel === '')!
    expect(top.testName).toBe('test_a')
    expect(top.failureCount).toBe(2)
    expect(top.usage?.total_cost_usd).toBe(0.02)

    const nested = groups.find(g => g.childLabel === 'child #7')!
    expect(nested.failureCount).toBe(2)
    expect(nested.usage?.total_cost_usd).toBe(0.5)
  })

  it('keeps same-named children in different builds apart', () => {
    const usageA = summary({ total_cost_usd: 0.1 })
    const usageB = summary({ total_cost_usd: 0.2 })
    const groups = primaryGroups(result({
      child_job_analyses: [
        child({ build_number: 1, failures: [failure({ token_usage: usageA })] }),
        child({ build_number: 2, failures: [failure({ token_usage: usageB })] }),
      ],
    }))
    expect(groups.map(g => g.childLabel)).toEqual(['child #1', 'child #2'])
    expect(groups.map(g => g.usage?.total_cost_usd)).toEqual([0.1, 0.2])
  })

  it('keeps failures with differing legacy signatures but one v2 signature in one group', () => {
    const usage = summary({ total_cost_usd: 0.7 })
    const job = result({
      failures: [
        failure({ test_name: 'test_a', error_signature: 'legacy-a', error_signature_v2: 'v2-sig', token_usage: usage }),
        failure({ test_name: 'test_b', error_signature: 'legacy-b', error_signature_v2: 'v2-sig', token_usage: usage }),
      ],
    })
    const groups = primaryGroups(job)
    expect(groups).toHaveLength(1)
    expect(groups[0].failureCount).toBe(2)
    expect(groups[0].usage?.total_cost_usd).toBe(0.7)
    // one debate, one spend row — not two rows repeating the same cost
    expect(peerGroups(job)).toHaveLength(0)
    const averages = spendAverages(1.4, 2, groups, [])
    expect(averages.totalGroups).toBe(1)
    expect(averages.costPerUniqueGroup).toBeCloseTo(0.7)
  })

  it('keeps failures with different v2 signatures in separate groups', () => {
    const groups = primaryGroups(result({
      failures: [
        failure({ test_name: 'test_a', error_signature: 'legacy', error_signature_v2: 'v2-a' }),
        failure({ test_name: 'test_b', error_signature: 'legacy', error_signature_v2: 'v2-b' }),
      ],
    }))
    expect(groups).toHaveLength(2)
  })

  it('falls back to the test name when a legacy failure has no signature', () => {
    const groups = primaryGroups(result({ failures: [failure({ error_signature: '', test_name: 'legacy' })] }))
    expect(groups).toHaveLength(1)
    expect(groups[0].usage).toBeNull()
  })

  it('never reports a re-analysis attempt as the group primary cost', () => {
    const groups = primaryGroups(result({
      failures: [
        // re-analysis leaves the attempt summary on the failure and no primary one
        failure({ test_name: 'reanalyzed', usage_attempt: 'attempt-1', token_usage: summary({ total_cost_usd: 9 }) }),
        failure({ test_name: 'original', token_usage: summary({ total_cost_usd: 0.4 }) }),
      ],
    }))
    expect(groups[0].usage?.total_cost_usd).toBe(0.4)
  })

  it('leaves group cost unavailable when only a re-analysis attempt has usage', () => {
    const groups = primaryGroups(result({
      failures: [failure({ usage_attempt: 'attempt-1', token_usage: summary({ total_cost_usd: 9 }) })],
    }))
    expect(groups[0].usage).toBeNull()
    expect(spendAverages(0.5, 1, groups, []).costPerUniqueGroup).toBeNull()
  })
})

describe('peerGroups', () => {
  const debate = (): PeerDebate => ({
    consensus_reached: true, rounds_used: 2, max_rounds: 2,
    ai_configs: [{ ai_provider: 'gemini', ai_model: 'pro' }, { ai_provider: 'gemini', ai_model: 'pro' }],
    rounds: [
      { round: 1, ai_provider: 'gemini', ai_model: 'pro', role: 'orchestrator', classification: 'CODE ISSUE', pattern: '', details: '', agrees_with_orchestrator: true, token_usage: call({ call_type: 'peer', cost_usd: 0.1 }) },
      { round: 1, ai_provider: 'gemini', ai_model: 'pro', role: 'peer', classification: 'CODE ISSUE', pattern: '', details: '', agrees_with_orchestrator: true, token_usage: call({ call_type: 'peer', cost_usd: 0.2 }) },
      { round: 1, ai_provider: 'gemini', ai_model: 'pro', role: 'peer', classification: 'CODE ISSUE', pattern: '', details: '', agrees_with_orchestrator: false, token_usage: call({ call_type: 'peer', cost_usd: 0.3 }) },
      { round: 2, ai_provider: 'gemini', ai_model: 'pro', role: 'orchestrator', classification: 'CODE ISSUE', pattern: '', details: '', agrees_with_orchestrator: true, token_usage: call({ call_type: 'peer', cost_usd: 0.4 }) },
      { round: 2, ai_provider: 'gemini', ai_model: 'pro', role: 'peer', classification: 'CODE ISSUE', pattern: '', details: '', agrees_with_orchestrator: null, token_usage: null },
    ],
  })

  /** The same debate with usage on every round (no unattributed spend). */
  const complete = (d: PeerDebate): PeerDebate => ({
    ...d,
    rounds: d.rounds.map(r => r.token_usage ? r : { ...r, token_usage: call({ call_type: 'peer', cost_usd: 0.5 }) }),
  })

  it('distinguishes rounds and same-model peers', () => {
    const [group] = peerGroups(result({ failures: [failure({ peer_debate: debate() })] }))
    expect(group.rounds.map(r => `R${r.round} ${r.agentLabel}`)).toEqual([
      'R1 Main AI', 'R1 Peer 1', 'R1 Peer 2', 'R2 Main AI', 'R2 Peer 1',
    ])
    expect(group.rounds.map(r => r.agent).every(a => a === 'gemini/pro')).toBe(true)
    // the round-2 peer has no recorded usage
    expect(group.rounds[4].usage).toBeNull()
  })

  it('counts a debate shared by sibling failures once', () => {
    const shared = debate()
    const groups = peerGroups(result({
      failures: [failure({ peer_debate: shared }), failure({ test_name: 'test_b', peer_debate: shared })],
    }))
    expect(groups).toHaveLength(1)
    expect(groups[0].siblingCount).toBe(1)
    // the shared debate's round-2 peer has no usage, so the total is a floor
    expect(groups[0].partial).toBe(true)
  })

  it('reports a lower bound when an attributable round cost is unknown', () => {
    const rounds = debate().rounds.map(r => ({ ...r, token_usage: r.token_usage ? { ...r.token_usage, cost_usd: null } : r.token_usage }))
    const [group] = peerGroups(result({ failures: [failure({ peer_debate: { ...debate(), rounds } })] }))
    expect(group.costUsd).toBe(0)
    expect(group.partial).toBe(true)
  })

  it('is a marked floor when an attempted round has no usage at all', () => {
    // the round-2 peer keeps no usage after a failed call, so the debate total is partial
    const [group] = peerGroups(result({ failures: [failure({ peer_debate: debate() })] }))
    expect(group.rounds[4].usage).toBeNull()
    expect(group.partial).toBe(true)
  })

  it('keeps a debate cost when only the round-1 orchestrator lacks usage', () => {
    const full = complete(debate())
    const rounds = full.rounds.map(r =>
      r.round === 1 && r.role === 'orchestrator' ? { ...r, token_usage: null } : r)
    const [group] = peerGroups(result({ failures: [failure({ peer_debate: { ...full, rounds } })] }))
    expect(group.costUsd).toBeCloseTo(0.2 + 0.3 + 0.4 + 0.5)
  })

  it('counts sibling debates separately when one sibling was re-analyzed', () => {
    const original = complete(debate())
    const reanalyzed = {
      ...original,
      // a re-analysis produces a new execution, even with identical content
      debate_id: 'exec-2',
      rounds: original.rounds.map(r => ({ ...r, details: 'new attempt', token_usage: { ...r.token_usage!, cost_usd: 0.01 } })),
    }
    const groups = peerGroups(result({
      failures: [
        failure({ test_name: 'test_a', peer_debate: original }),
        failure({ test_name: 'test_b', peer_debate: reanalyzed }),
        failure({ test_name: 'test_c', peer_debate: original }),
      ],
    }))
    expect(groups).toHaveLength(2)
    expect(groups.map(g => g.siblingCount).sort()).toEqual([0, 1])
    expect(groups.map(g => g.testName).sort()).toEqual(['test_a', 'test_b'])
    // both debates are reported in full; neither is dropped in favour of the first
    expect(groups.map(g => g.costUsd).sort((a, b) => (a ?? 0) - (b ?? 0))).toEqual([0.05, 1.5])
  })

  it('keeps identical-content debates apart when they have different execution ids', () => {
    const first = { ...complete(debate()), debate_id: 'exec-1' }
    const second = { ...first, debate_id: 'exec-2' }
    const groups = peerGroups(result({
      failures: [failure({ peer_debate: first }), failure({ test_name: 'test_b', peer_debate: second })],
    }))
    expect(groups).toHaveLength(2)
    expect(groups.map(g => g.siblingCount)).toEqual([0, 0])
  })

  it('shares one debate across failures carrying the same execution id', () => {
    const shared = { ...complete(debate()), debate_id: 'exec-1' }
    const groups = peerGroups(result({
      failures: [
        failure({ peer_debate: shared }),
        failure({ test_name: 'test_b', peer_debate: { ...shared } }),
      ],
    }))
    expect(groups).toHaveLength(1)
    expect(groups[0].siblingCount).toBe(1)
  })

  it('names the model the call was actually billed on', () => {
    const [group] = peerGroups(result({
      failures: [failure({
        peer_debate: {
          ...complete(debate()),
          rounds: [{
            ...complete(debate()).rounds[0],
            ai_provider: 'gemini', ai_model: 'configured-model',
            token_usage: call({ provider: 'openrouter', model: 'billed-model', cost_usd: 0.4 }),
          }],
        },
      })],
    }))
    expect(group.rounds[0].agent).toBe('openrouter/billed-model')
  })

  it('falls back to the configured identity when usage records none', () => {
    const [group] = peerGroups(result({
      failures: [failure({
        peer_debate: {
          ...complete(debate()),
          rounds: [{ ...complete(debate()).rounds[0], token_usage: { ...call({ cost_usd: 0.4 }), provider: '', model: '' } }],
        },
      })],
    }))
    expect(group.rounds[0].agent).toBe('gemini/pro')
  })
  it('excludes a debate with unattributed rounds from the peer cost average', () => {
    const [group] = peerGroups(result({ failures: [failure({ peer_debate: debate() })] }))
    expect(group.partial).toBe(true)
    const averages = spendAverages(1.0, 1, [], [group])
    expect(averages.peerCostPerDebatedGroup).toBeNull()
    expect(averages.peersWithKnownCost).toBe(0)
    expect(averages.peersWithFloorCost).toBe(1)
    expect(averages.debatedGroups).toBe(1)
  })

  it('averages a fully priced debate and counts it in the denominator', () => {
    const [group] = peerGroups(result({ failures: [failure({ peer_debate: complete(debate()) })] }))
    expect(group.partial).toBe(false)
    const averages = spendAverages(2.0, 1, [], [group])
    expect(averages.peersWithKnownCost).toBe(1)
    expect(averages.peersWithFloorCost).toBe(0)
    expect(averages.peerCostPerDebatedGroup).toBeCloseTo(group.costUsd)
  })
})


describe('spendAverages', () => {
  const groups = primaryGroups(result({
    failures: [failure({ token_usage: summary({ total_cost_usd: 0.2 }) })],
    child_job_analyses: [child({ failures: [failure({ test_name: 'test_b' })] })],
  }))

  it('divides job cost by analyzed failing tests', () => {
    const averages = spendAverages(0.5, 4, groups, [])
    expect(averages.costPerFailingTest).toBeCloseTo(0.125)
    expect(averages.totalGroups).toBe(2)
  })

  it('is N/A with zero analyzed failures or unknown total cost', () => {
    expect(spendAverages(0.5, 0, groups, []).costPerFailingTest).toBeNull()
    expect(spendAverages(null, 4, groups, []).costPerFailingTest).toBeNull()
  })

  it('keeps the unique-group denominator honest when cost is unavailable', () => {
    const averages = spendAverages(0.5, 2, groups, [])
    expect(averages.groupsWithKnownCost).toBe(1)
    expect(averages.totalGroups).toBe(2)
    expect(averages.costPerUniqueGroup).toBeCloseTo(0.2)
  })

  it('is N/A when no group has a known cost', () => {
    const unknown = primaryGroups(result({ failures: [failure()] }))
    expect(spendAverages(0.5, 1, unknown, []).costPerUniqueGroup).toBeNull()
  })

  it('averages peer cost only over debates with a known cost', () => {
    const peer = peerGroups(result({ failures: [failure({ peer_debate: {
      consensus_reached: true, rounds_used: 1, max_rounds: 1, ai_configs: [],
      rounds: [{ round: 1, ai_provider: 'g', ai_model: 'p', role: 'orchestrator', classification: '', pattern: '', details: '', agrees_with_orchestrator: true, token_usage: call({ cost_usd: 0.4 }) }],
    } })] }))
    const averages = spendAverages(0.5, 1, groups, peer)
    expect(averages.debatedGroups).toBe(1)
    expect(averages.peerCostPerDebatedGroup).toBeCloseTo(0.4)
    expect(averages.debatedGroups).toBe(1)
    expect(averages.peersWithKnownCost).toBe(1)
  })

  it('reports the peer denominator as debates with a known cost', () => {
    const known = peerGroups(result({ failures: [failure({ peer_debate: {
      consensus_reached: true, rounds_used: 1, max_rounds: 1, ai_configs: [],
      rounds: [{ round: 1, ai_provider: 'g', ai_model: 'p', role: 'orchestrator', classification: '', pattern: '', details: '', agrees_with_orchestrator: true, token_usage: call({ cost_usd: 0.4 }) }],
    } })] }))
    const unavailable = peerGroups(result({ failures: [failure({ test_name: 'test_b', error_signature: 'sig-b', peer_debate: {
      consensus_reached: false, rounds_used: 2, max_rounds: 2, ai_configs: [],
      rounds: [
        { round: 1, ai_provider: 'g', ai_model: 'p', role: 'orchestrator', classification: '', pattern: '', details: '', agrees_with_orchestrator: true, token_usage: null },
        { round: 1, ai_provider: 'g', ai_model: 'p', role: 'peer', classification: '', pattern: '', details: '', agrees_with_orchestrator: null, token_usage: null },
      ],
    } })] }))
    const averages = spendAverages(0.9, 2, groups, [...known, ...unavailable])
    expect(averages.debatedGroups).toBe(2)
    expect(averages.peersWithKnownCost).toBe(1)
    expect(averages.peerCostPerDebatedGroup).toBeCloseTo(0.4)
  })
})

describe('analyzedFailingTests', () => {
  it('counts only failures carrying an analysis, across the child tree', () => {
    const analyzed = result({
      failures: [failure(), failure({ test_name: 'test_b' })],
      child_job_analyses: [child({ failures: [failure({ test_name: 'test_c', analysis: null as never })] })],
    })
    expect(analyzedFailingTests(analyzed)).toBe(2)
  })
})

describe('failed-cost partialness is independent of stage partialness', () => {
  it('does not flag a failed-spend figure because a SUCCEEDED call was unpriced', () => {
    // The failed call is fully priced, so `failedCostUsd` is a complete figure.
    // Stage-wide partialness comes from the successful unpriced call and must not
    // leak into the wasted-cost disclosure.
    const [stage] = stageBreakdown(usageSummary([
      call({ success: false, cost_usd: 0.4 }),
      call({ success: true, cost_usd: null }),
    ]))
    expect(stage.failedCostUsd).toBeCloseTo(0.4)
    expect(stage.partial).toBe(true)
    expect(stage.failedPartial).toBe(false)
  })

  it('flags the failed figure when a FAILED call is itself unpriced', () => {
    const [stage] = stageBreakdown(usageSummary([
      call({ success: false, cost_usd: null }),
      call({ success: true, cost_usd: 0.2 }),
    ]))
    expect(stage.failedPartial).toBe(true)
  })

  it('flags a failed call whose recorded cost covers only some turns', () => {
    const [stage] = stageBreakdown(usageSummary([
      call({ success: false, cost_usd: 0.4, cost_partial: true }),
    ]))
    expect(stage.failedPartial).toBe(true)
  })
})

describe('spendAverages excludes floor-only costs from known-cost averages', () => {
  const floorGroup = primaryGroups(result({
    failures: [failure({
      error_signature: 'floor',
      token_usage: summary({
        total_cost_usd: 0.5,
        cost_partial: true,
        calls: [call({ cost_usd: 0.5, cost_partial: true })],
      }),
    })],
  }))
  const exactGroup = primaryGroups(result({
    failures: [failure({ error_signature: 'exact', token_usage: summary({ total_cost_usd: 0.2 }) })],
  }))

  it('excludes a partial group rather than averaging its floor as known spend', () => {
    const averages = spendAverages(0.7, 2, [...exactGroup, ...floorGroup], [])
    // Only the exact group counts, so the mean is 0.2 — not (0.2 + 0.5) / 2.
    expect(averages.costPerUniqueGroup).toBeCloseTo(0.2)
    expect(averages.groupsWithKnownCost).toBe(1)
    expect(averages.groupsWithFloorCost).toBe(1)
    expect(averages.totalGroups).toBe(2)
  })

  it('excludes a group whose own calls carry an unpriced entry', () => {
    const unpricedCallGroup = primaryGroups(result({
      failures: [failure({
        error_signature: 'unpriced',
        token_usage: summary({ total_cost_usd: 0.9, cost_partial: false, calls: [call({ cost_usd: null })] }),
      })],
    }))
    const averages = spendAverages(0.9, 1, unpricedCallGroup, [])
    expect(averages.costPerUniqueGroup).toBeNull()
    expect(averages.groupsWithFloorCost).toBe(1)
  })

  it('reports N/A when every priced group is a floor', () => {
    expect(spendAverages(0.5, 1, floorGroup, []).costPerUniqueGroup).toBeNull()
  })
})
