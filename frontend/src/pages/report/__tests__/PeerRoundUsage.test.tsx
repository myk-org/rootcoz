import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { PeerAnalysisSummary } from '../PeerAnalysisSummary'
import { PeerDebateSection } from '../PeerDebateSection'
import type { FailureAnalysis, PeerDebate, PeerRound, TokenUsageEntry } from '@/types'
import { TooltipProvider } from '@/components/ui/tooltip'

const usage = (tokens: number): TokenUsageEntry => ({
  provider: 'gemini', model: 'pro', call_type: 'peer',
  input_tokens: tokens, output_tokens: 2, cache_read_tokens: 1,
  cache_write_tokens: 3, total_tokens: tokens + 2,
  cost_usd: tokens / 1000, duration_ms: 100,
})
const round = (num: number, role: PeerRound['role'], tokens?: number): PeerRound => ({
  round: num, role, ai_provider: 'gemini', ai_model: 'pro',
  classification: 'CODE ISSUE', pattern: '', details: `result-${num}-${role}-${tokens}`,
  agrees_with_orchestrator: role === 'peer' ? (tokens === 22 || tokens === undefined ? null : true) : true,
  token_usage: tokens === undefined ? null : usage(tokens),
})
const debate: PeerDebate = {
  consensus_reached: true, rounds_used: 2, max_rounds: 2,
  ai_configs: [{ ai_provider: 'gemini', ai_model: 'pro' }],
  rounds: [round(1, 'orchestrator'), round(1, 'peer', 11), round(1, 'peer', 22), round(2, 'orchestrator', 33), round(2, 'peer', 44), round(2, 'peer')],
}

describe('peer round usage', () => {
  it.each(['summary', 'section'])('shows per-result usage in %s without guessing missing usage', (view) => {
    if (view === 'summary') {
      const primary = usage(99)
      const failure = {
        test_name: 'test_one', error_signature: 'sig', peer_debate: debate,
        token_usage: {
          total_input_tokens: 99, total_output_tokens: 2, total_cache_read_tokens: 1,
          total_cache_write_tokens: 3, total_tokens: 101, total_cost_usd: 0.10,
          total_duration_ms: 100, total_calls: 1, calls: [primary],
        },
      } as FailureAnalysis
      render(<TooltipProvider><PeerAnalysisSummary failures={[failure]} repoUrls={[]} /></TooltipProvider>)
      fireEvent.click(screen.getByRole('button', { name: /peer analysis/i }))
      fireEvent.click(screen.getByRole('button', { name: /test_one/i }))
    } else {
      render(<TooltipProvider><PeerDebateSection debate={debate} repoUrls={[]} /></TooltipProvider>)
      fireEvent.click(screen.getByRole('button', { name: /peer analysis/i }))
    }
    for (const tokens of [11, 22, 33, 44]) {
      const entry = screen.getByText(`result-${tokens === 33 || tokens === 44 ? 2 : 1}-${tokens === 33 ? 'orchestrator' : 'peer'}-${tokens}`).closest('.rounded-md')!
      expect(within(entry as HTMLElement).getByText(new RegExp(`${tokens} in / 2 out`))).toHaveTextContent(`$${(tokens / 1000).toFixed(2)}`)
    }
    expect(screen.getAllByText('Usage unavailable')).toHaveLength(view === 'summary' ? 1 : 2)
    if (view === 'summary') {
      expect(screen.getByText(/99 in \/ 2 out/)).toHaveTextContent('$0.10')
      expect(screen.getByText('Primary (shared)')).toBeInTheDocument()
    }
  })
})

const diagnosedRound = (overrides: Partial<PeerRound>): PeerRound => ({
  round: 1, role: 'peer', ai_provider: 'gemini', ai_model: 'pro',
  classification: '', pattern: '', details: '',
  agrees_with_orchestrator: null, token_usage: null,
  ...overrides,
})

const diagnosedDebate: PeerDebate = {
  consensus_reached: false, rounds_used: 1, max_rounds: 1,
  ai_configs: [{ ai_provider: 'gemini', ai_model: 'pro' }],
  rounds: [
    diagnosedRound({ role: 'orchestrator', classification: 'CODE ISSUE', agrees_with_orchestrator: true, details: 'orch' }),
    diagnosedRound({ error: 'HTTP 503: upstream refused', details: '' }),
    diagnosedRound({
      classification: 'CODE ISSUE',
      agrees_with_orchestrator: true,
      warning: '503 Service Unavailable',
      details: 'peer reasoning stays visible',
    }),
  ],
}

describe('peer round diagnostics (#569)', () => {
  it.each(['section', 'summary'])('renders Failed, Turn warning and their callouts in %s', (view) => {
    if (view === 'section') {
      render(<TooltipProvider><PeerDebateSection debate={diagnosedDebate} repoUrls={[]} /></TooltipProvider>)
      fireEvent.click(screen.getByRole('button', { name: /peer analysis/i }))
    } else {
      const failure = {
        test_name: 'test_one', error_signature: 'sig', peer_debate: diagnosedDebate,
      } as FailureAnalysis
      render(<TooltipProvider><PeerAnalysisSummary failures={[failure]} repoUrls={[]} /></TooltipProvider>)
      fireEvent.click(screen.getByRole('button', { name: /peer analysis/i }))
      fireEvent.click(screen.getByRole('button', { name: /test_one/i }))
    }
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.getByText('HTTP 503: upstream refused')).toBeInTheDocument()
    expect(screen.getByText('Turn warning')).toBeInTheDocument()
    expect(screen.getByText('503 Service Unavailable')).toBeInTheDocument()
    expect(screen.getByText('Agrees')).toBeInTheDocument()
    expect(screen.getByText(/peer reasoning stays visible/)).toBeInTheDocument()
    // The failed peer has no classification, so its card carries no empty pill:
    // only the role badge and the Failed badge.
    const failedCard = screen.getByText('HTTP 503: upstream refused').closest('.rounded-md') as HTMLElement
    expect(failedCard.querySelectorAll('.rounded-full')).toHaveLength(2)
    expect(within(failedCard).queryByText('CODE ISSUE')).toBeNull()
    expect(screen.getAllByText('CODE ISSUE')).toHaveLength(2)
  })
})
