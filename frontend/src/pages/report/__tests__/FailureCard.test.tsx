import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ReportProvider } from '../ReportContext'
import { FailureCard } from '../FailureCard'
import type { FailureAnalysis, GroupedFailure, PeerDebate, TokenUsageSummary } from '@/types'

const auth = { role: 'reviewer', isOperator: false, isAdmin: false, username: 'tester' }
vi.mock('@/lib/auth', () => ({ useAuth: () => auth }))

const usage: TokenUsageSummary = {
  total_input_tokens: 1500,
  total_output_tokens: 200,
  total_cache_read_tokens: 100,
  total_cache_write_tokens: 50,
  total_tokens: 1700,
  total_cost_usd: 0.005,
  total_duration_ms: 300,
  total_calls: 1,
  calls: [{ provider: 'openai', model: 'gpt-4', call_type: 'primary', input_tokens: 1500, output_tokens: 200, cache_read_tokens: 100, cache_write_tokens: 50, total_tokens: 1700, cost_usd: 0.005, duration_ms: 300 }],
}

type FailureWithUsage = FailureAnalysis & { token_usage?: TokenUsageSummary | null }

function showCard(count: number, tokenUsage?: TokenUsageSummary | null, peerDebate?: PeerDebate) {
  const tests: FailureWithUsage[] = Array.from({ length: count }, (_, i) => ({
    id: `id-${i}`, test_name: `test-${i}`, error: 'boom', error_signature: 'shared',
    analysis: { classification: 'CODE ISSUE', pattern: '', affected_tests: [], details: '', artifacts_evidence: '' },
    token_usage: tokenUsage,
    peer_debate: peerDebate,
  }))
  const group: GroupedFailure = { id: 'group-id-0', signature: 'shared', tests, count }
  render(
    <MemoryRouter>
      <TooltipProvider delayDuration={0}>
        <ReportProvider>
          <FailureCard group={group} jobId="job-1" index={0} />
        </ReportProvider>
      </TooltipProvider>
    </MemoryRouter>,
  )
  fireEvent.click(screen.getByRole('button', { name: /test-0/i }))
}

describe('FailureCard primary usage', () => {
  beforeEach(() => { sessionStorage.clear(); auth.role = 'reviewer' })
  it('shows attributed tokens and cost for a single failure', async () => {
    showCard(1, usage)
    expect(screen.getByText('Primary AI usage')).toBeTruthy()
    expect(screen.getByText(/1.5K in \/ 200 out/)).toBeTruthy()
    expect(screen.getByText(/\$0.0050/)).toBeTruthy()
    await userEvent.hover(screen.getByText(/1.5K in \/ 200 out/))
    expect((await screen.findAllByText(/Cache read: 100/)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Cache write: 50/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Total tokens: 1,700/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/primary · openai\/gpt-4/).length).toBeGreaterThan(0)
  })

  it('labels usage shared by failures with the same signature', () => {
    showCard(2, usage)
    expect(screen.getByText('Shared group usage')).toBeTruthy()
    expect(screen.getByText(/Job total includes other AI calls/)).toBeTruthy()
    expect(screen.getAllByText(/1.5K in \/ 200 out/)).toHaveLength(1)
  })

  it.each([null, undefined])('does not invent usage for old jobs without attribution (%s)', (missing) => {
    showCard(1, missing)
    expect(screen.queryByText(/usage/i)).toBeNull()
    expect(screen.queryByText(/\$0.00/)).toBeNull()
  })

  it('shows the group primary usage in the first orchestrator round', () => {
    showCard(1, usage, {
      consensus_reached: true, rounds_used: 1, max_rounds: 1, ai_configs: [],
      rounds: [{ round: 1, role: 'orchestrator', ai_provider: 'openai', ai_model: 'gpt-4', classification: 'CODE ISSUE', pattern: '', details: 'initial assessment', agrees_with_orchestrator: true, token_usage: null }],
    })
    fireEvent.click(screen.getByRole('button', { name: /peer analysis/i }))
    expect(screen.getByText('Primary (shared)')).toBeInTheDocument()
    expect(screen.getAllByText(/1.5K in \/ 200 out/)).toHaveLength(2)
    expect(screen.queryByText('Usage unavailable')).not.toBeInTheDocument()
  })

  it('shows a $0.00 floor, marked as a lower bound, when cost is unpriced', () => {
    showCard(1, { ...usage, total_cost_usd: null, cost_partial: true })
    expect(screen.queryByText(/Unavailable/)).toBeNull()
    expect(screen.getAllByText(/\$0.00/).length).toBeGreaterThan(0)
  })

  it('no longer renders the bare provider/model inputs on the card', () => {
    showCard(1)
    expect(screen.queryByPlaceholderText('provider')).toBeNull()
    expect(screen.queryByPlaceholderText('model')).toBeNull()
    expect(screen.queryByText(/AI for issue generation/i)).toBeNull()
  })
})

describe('FailureCard review controls', () => {
  beforeEach(() => { sessionStorage.clear(); auth.role = 'reviewer' })

  it('hides every review control from viewers', () => {
    auth.role = 'viewer'
    showCard(3)
    // Header bulk control and the expanded "Review All" button both stay hidden.
    expect(screen.queryByRole('button', { name: /Review All|Review \d+\/3/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /reviewed/i })).toBeNull()
  })

  it('shows the bulk review control to reviewers', () => {
    showCard(3)
    expect(screen.getByRole('button', { name: /Review All \(0\/3\)/ })).toBeInTheDocument()
  })
})
