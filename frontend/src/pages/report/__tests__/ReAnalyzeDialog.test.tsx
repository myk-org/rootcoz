import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ReAnalyzeDialog } from '@/pages/report/ReAnalyzeDialog'
import { ApiError } from '@/lib/api'
import type { AnalysisResult } from '@/types'

const mockPost = vi.fn()

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return { ...actual, api: { post: (...args: unknown[]) => mockPost(...args) } }
})

vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))

vi.mock('@/lib/auth', () => ({ useAuth: () => ({ canUseServerProviders: true }) }))
vi.mock('@/lib/usePeerModels', () => ({ usePeerModels: () => [] }))
vi.mock('@/lib/useProviderOptions', () => ({
  useProviderCatalog: () => ({ providers: [], providerStatus: {} }),
}))
vi.mock('@/lib/analysisAi', () => ({ isAnalysisFormAiUnavailable: () => false }))
vi.mock('@/components/shared/AnalysisAiPicker', () => ({
  AnalysisProviderSelect: () => <div />,
  AnalysisModelSelect: () => <div />,
}))

const DETAIL = 'Cannot re-analyze: this job has no stored Jenkins job name and build number'

const result = {
  job_id: 'job-1',
  job_name: 'old-job',
  build_number: 7,
  request_params: { ai_provider: 'claude', ai_model: 'claude-sonnet-4' },
} as unknown as AnalysisResult

describe('ReAnalyzeDialog', () => {
  it('shows the backend detail when re-analysis is rejected', async () => {
    mockPost.mockRejectedValue(
      new ApiError(400, 'Bad Request', {
        detail: DETAIL,
      }),
    )
    render(
      <ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job-1" />,
    )
    fireEvent.click(screen.getByRole('button', { name: /re-analyze/i }))
    await waitFor(() => expect(screen.getByText(DETAIL)).toBeInTheDocument())
  })
})
