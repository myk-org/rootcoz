import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { TokenUsageBadge } from '../TokenUsageBadge'
import { CostCell } from '@/components/shared/TokenUsageCostCell'
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

  it('shows an unavailable cost as unavailable, not as a free $0', () => {
    // A legacy summary has no recorded total. It is not free, and there is no
    // floor to disclose — rendering `$0.00` here would reintroduce the exact
    // "unavailable reads as free" bug this reporting removed.
    renderBadge(usage({ total_cost_usd: null, cost_partial: true }))
    const badge = screen.getByText(/in \/ .* out/)
    expect(badge).toHaveTextContent('Unavailable')
    expect(badge.textContent).not.toContain('$0.00')
  })

  it('derives a floor from a legacy null total when the calls support one', () => {
    // Same legacy null, but the per-call records survive — the spend is provable.
    const call = (cost_usd: number | null) => ({
      call_type: 'primary', provider: 'openrouter', model: 'm',
      input_tokens: 0, output_tokens: 0, total_tokens: 0,
      cache_read_tokens: 0, cache_write_tokens: 0,
      cost_usd, cost_partial: false, success: true, duration_ms: 1,
      credential_source: 'user',
    })
    renderBadge(usage({
      total_cost_usd: null,
      cost_partial: false,
      calls: [call(0.4), call(null)] as TokenUsageSummary['calls'],
    }))
    const badge = screen.getByText(/in \/ .* out/)
    expect(badge).toHaveTextContent('$0.40')
    expect(badge).toHaveTextContent('+')
  })

  it('shows no marker for a complete cost', () => {
    renderBadge(usage({ cost_partial: false }))
    expect(screen.getByText(/100 in \/ 50 out/)).toHaveTextContent('$0.25')
    expect(screen.getByText(/100 in \/ 50 out/).textContent).not.toContain('+')
  })

  it('shows a genuine free-model zero as a complete $0.00', () => {
    // pi-sidecar reports cost_usd 0 with cost_partial false for a free model
    renderBadge(usage({ total_cost_usd: 0, cost_partial: false }))
    const badge = screen.getByText(/100 in \/ 50 out/)
    expect(badge).toHaveTextContent('$0.00')
    expect(badge.textContent).not.toContain('+')
  })
})

describe('CostCell lower-bound marker', () => {
  const renderCell = (cost: number | null, partial: boolean | number, priced?: number, total?: number) =>
    render(
      <TooltipProvider>
        <CostCell cost={cost} partial={partial} pricedCalls={priced} totalCalls={total} />
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

  it('renders an unavailable cost as unavailable, with no floor to mark', () => {
    renderCell(null, true, 1, 3)
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /lower bound/i })).toBeNull()
  })

  it('explains how many calls were excluded from the floor', async () => {
    renderCell(0.25, true, 2, 3)
    fireEvent.focus(screen.getByRole('button', { name: /lower bound/i }))
    // 3 calls, 2 priced — the ONE unpriced call is what was excluded.
    expect((await screen.findAllByText(/1 of 3 calls had no recorded price/)).length).toBeGreaterThan(0)
  })

  it('reports the excluded count from a large mixed range', async () => {
    // The real shape of the regression: 339 of 351 calls priced.
    renderCell(2.06, true, 339, 351)
    fireEvent.focus(screen.getByRole('button', { name: /lower bound/i }))
    expect((await screen.findAllByText(/12 of 351 calls had no recorded price/)).length).toBeGreaterThan(0)
    expect(screen.queryByText(/339 of 351 calls had no recorded price/)).toBeNull()
  })

  it('shows no marker for a complete numeric cost', () => {
    renderCell(0.10, false)
    expect(screen.queryByRole('button', { name: /lower bound/i })).toBeNull()
  })
})

describe('CostCell inside a clickable job row', () => {
  it('does not toggle the surrounding row when the explanation is clicked', () => {
    const onRowClick = vi.fn()
    render(
      <TooltipProvider>
        <table><tbody>
          <tr onClick={onRowClick}>
            <td><CostCell cost={0.10} partial /></td>
          </tr>
        </tbody></table>
      </TooltipProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: /lower bound/i }))
    expect(onRowClick).not.toHaveBeenCalled()
  })
})
