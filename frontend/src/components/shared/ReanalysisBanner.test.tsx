import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { OriginJobBanner, ReanalyzedForwardBanner } from './ReanalysisBanner'

function renderBanner(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

describe('ReanalyzedForwardBanner', () => {
  it('links to the latest re-analysis', () => {
    renderBanner(<ReanalyzedForwardBanner reanalysisJobId="new-job" />)
    expect(screen.getByText(/This job was re-analyzed/)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'view' })
    expect(link).toHaveAttribute('href', '/results/new-job')
  })
})

describe('OriginJobBanner', () => {
  it('links back to the original job', () => {
    renderBanner(
      <OriginJobBanner originJobId="old-job" originJobName="My Job" />,
    )
    expect(screen.getByText(/Re-analysis of My Job/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'view' })).toHaveAttribute(
      'href',
      '/results/old-job',
    )
  })
})
