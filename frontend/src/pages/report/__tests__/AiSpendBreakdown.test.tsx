import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { AiSpendBreakdown } from '../AiSpendBreakdown'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { AnalysisResult, TokenUsageEntry, TokenUsageSummary } from '@/types'

const call = (over: Partial<TokenUsageEntry> = {}): TokenUsageEntry => ({
  provider: 'gemini', model: 'pro', call_type: 'primary',
  input_tokens: 10, output_tokens: 5, cache_read_tokens: 4, cache_write_tokens: 2,
  total_tokens: 15, cost_usd: 0.02, duration_ms: 100, success: true, ...over,
})

const summary = (calls: TokenUsageEntry[], over: Partial<TokenUsageSummary> = {}): TokenUsageSummary => ({
  total_input_tokens: 10, total_output_tokens: 5, total_cache_read_tokens: 4,
  total_cache_write_tokens: 2, total_tokens: 15, total_cost_usd: 0.02,
  total_duration_ms: 100, total_calls: calls.length, calls, ...over,
})

const result = (over: Partial<AnalysisResult> = {}): AnalysisResult => ({
  job_id: 'j1', job_name: 'job', build_number: 1, jenkins_url: null, status: 'completed',
  summary: '', ai_provider: 'gemini', ai_model: 'pro', failures: [], child_job_analyses: [],
  ...over,
})

const expand = () => fireEvent.click(screen.getByRole('button', { name: /ai spend/i }))

const renderSection = (r: AnalysisResult) =>
  render(<TooltipProvider><AiSpendBreakdown result={r} /></TooltipProvider>)

describe('AiSpendBreakdown', () => {
  it('breaks usage down by stage and reports failed-call waste', () => {
    renderSection(result({
      token_usage: summary([
        call(),
        call({ call_type: 'agent_routing', cost_usd: 0.01 }),
        call({ call_type: 'primary', success: false, cost_usd: 0.08 }),
      ], { total_cost_usd: 0.11, total_duration_ms: 300 }),
    }))
    expand()

    const stages = screen.getByRole('table', { name: '' }).closest('section')!
    expect(within(stages).getByText('agent_routing')).toBeInTheDocument()
    // cache read/write reported apart from billed input/output
    expect(within(stages).getByText(/4 \+2w/)).toBeInTheDocument()

    expect(screen.getByText(/1 failed call/)).toBeInTheDocument()
    expect(screen.getByText(/\$0\.08/)).toBeInTheDocument()
    expect(screen.getByText(/not wall-clock/)).toBeInTheDocument()
  })

  it('shows N/A averages for a zero-failure job and unavailable costs without inventing zeroes', () => {
    renderSection(result({ token_usage: summary([call({ cost_usd: null, success: null })], { total_cost_usd: null }) }))
    expand()
    expect(screen.getAllByText('N/A').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThan(0)
    // legacy rows have no recorded outcome, so zero failures is not proof of success
    expect(screen.getByText(/Some calls predate outcome tracking/)).toBeInTheDocument()
  })

  it('marks the failed-call count as a lower bound when any stage has unknown outcomes', () => {
    renderSection(result({
      token_usage: summary([
        call({ call_type: 'primary', success: false, cost_usd: 0.08 }),
        call({ call_type: 'agent_routing', success: null }),
      ], { total_cost_usd: 0.1 }),
    }))
    expand()
    expect(screen.getByText(/1 failed call \(lower bound/)).toBeInTheDocument()
  })

  it('exposes every spend hint as a keyboard-focusable control', () => {
    renderSection(result({ token_usage: summary([call()]) }))
    expand()
    expect(screen.getByRole('button', { name: /cache read and write tokens/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /billed tokens include/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /what counts in cost per failing test/i })).toBeInTheDocument()
  })

  it('reports the billed token total separately from cache tokens', () => {
    renderSection(result({ token_usage: summary([call()]) }))
    expand()
    // 10 in + 5 out billed, cache 4 read / 2 write listed apart
    expect(screen.getByText('15')).toBeInTheDocument()
  })

  it('renders nothing for a legacy result with no usage and no failures', () => {
    const { container } = renderSection(result())
    expect(container).toBeEmptyDOMElement()
  })

  it('lists primary groups with shared-failure counts and peer rounds', () => {
    const peerRound = { round: 1, ai_provider: 'gemini', ai_model: 'pro', role: 'peer' as const, classification: '', pattern: '', details: '', agrees_with_orchestrator: true, token_usage: call({ call_type: 'peer', cost_usd: 0.05 }) }
    renderSection(result({
      failures: [
        { id: 'f1', test_name: 'test_a', error: '', analysis: {} as never, error_signature: 'sig', token_usage: summary([call()]) },
        { id: 'f2', test_name: 'test_b', error: '', analysis: {} as never, error_signature: 'sig',
          peer_debate: { consensus_reached: true, rounds_used: 1, max_rounds: 1, ai_configs: [], rounds: [peerRound] } },
      ],
    }))
    expand()
    expect(screen.getByText(/shared by 2 tests/)).toBeInTheDocument()
    expect(screen.getByText('R1 Peer 1 · gemini/pro: 15 tokens · $0.05')).toBeInTheDocument()
  })
})
