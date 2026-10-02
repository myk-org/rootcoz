import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TokenUsageBadge } from '../TokenUsageBadge'
import { CostCell } from '@/pages/TokenUsagePage'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { TokenUsageSummary } from '@/types'

const usage = (over: Partial<TokenUsageSummary> = {}): TokenUsageSummary => ({
  total_input_tokens: 100,
  total_output_tokens: 50,
  total_cache_read_tokens: 0,
  total_cache_write_tokens: 0,
  total_tokens: 150,
  total_cost_usd: 0.25,
  total_duration_ms: 1000,
  total_calls: 1,
  calls: [],
  ...over,
})

const renderBadge = (summary: TokenUsageSummary) =>
  render(<TooltipProvider><TokenUsageBadge usage={summary} /></TooltipProvider>)

describe('TokenUsageBadge partial cost', () => {
  it('marks a numeric cost that is only a lower bound', () => {
    renderBadge(usage({ cost_partial: true }))
    expect(screen.getByText(/100 in \/ 50 out/)).toHaveTextContent('$0.25+')
  })

  it('never renders a partial marker on an unavailable cost', () => {
    // "Unavailable+" and "Unavailable (partial)" would both mean nothing
    renderBadge(usage({ total_cost_usd: null, cost_partial: true }))
    const badge = screen.getByText(/in \/ .* out/)
    expect(badge).toHaveTextContent('Unavailable')
    expect(badge.textContent).not.toContain('+')
    expect(badge.textContent).not.toContain('partial')
  })

  it('shows no marker for a complete cost', () => {
    renderBadge(usage({ cost_partial: false }))
    expect(screen.getByText(/100 in \/ 50 out/)).toHaveTextContent('$0.25')
  })
})

describe('CostCell lower-bound marker', () => {
  const renderCell = (cost: number | null, partial: boolean | number) =>
    render(
      <TooltipProvider>
        <CostCell cost={cost} partial={partial} />
      </TooltipProvider>,
    )

  it('marks a numeric partial cost as a lower bound', () => {
    renderCell(0.10, true)
    expect(screen.getByRole('button', { name: /lower bound/i })).toBeInTheDocument()
    expect(screen.getByText('$0.10')).toBeInTheDocument()
  })

  it('accepts the integer flag SQLite returns', () => {
    renderCell(0.10, 1)
    expect(screen.getByRole('button', { name: /lower bound/i })).toBeInTheDocument()
  })

  it('never marks an unavailable cost as a lower bound', () => {
    renderCell(null, true)
    expect(screen.queryByRole('button', { name: /lower bound/i })).toBeNull()
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
  })

  it('shows no marker for a complete numeric cost', () => {
    renderCell(0.10, false)
    expect(screen.queryByRole('button', { name: /lower bound/i })).toBeNull()
  })
})
