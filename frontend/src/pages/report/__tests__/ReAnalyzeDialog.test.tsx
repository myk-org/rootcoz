import { describe, it, expect, vi, beforeEach } from 'vitest'
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

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ canUseServerProviders: true, canUseFreeProviders: false }),
}))
vi.mock('@/lib/useProviderOptions', () => ({
  useProviderCatalog: () => ({
    providers: {
      claude: [{ id: 'claude-sonnet-4', name: 'Sonnet 4', provider: 'claude', credential_sources: ['user'], verified: true }],
      openai: [{ id: 'gpt-5', name: 'GPT-5', provider: 'openai', credential_sources: ['user'], verified: true }],
    },
    providerStatus: {},
  }),
}))
// Real availability logic so deferPrimary behavior is exercised.
vi.mock('@/components/shared/AnalysisAiPicker', () => ({
  AnalysisProviderSelect: ({ value, label }: { value: string; label?: string }) => (
    <div data-testid={`provider-${label ?? 'primary'}`}>{value}</div>
  ),
  AnalysisModelSelect: ({ value, label }: { value: string; label?: string }) => (
    <div data-testid={`model-${label ?? 'primary'}`}>{value}</div>
  ),
}))

const DETAIL = 'Cannot re-analyze: this job has no stored Jenkins job name and build number'

const result = {
  job_id: 'job-1',
  job_name: 'old-job',
  build_number: 7,
  request_params: { ai_provider: 'claude', ai_model: 'claude-sonnet-4' },
} as unknown as AnalysisResult

function submittedBody() {
  const [, body] = mockPost.mock.calls[0]
  return body as Record<string, unknown>
}

describe('ReAnalyzeDialog', () => {
  beforeEach(() => {
    mockPost.mockReset()
  })

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

  it('prefills from the resolved result when request_params is empty — no claude fallback', () => {
    const resolved = {
      ...result,
      ai_provider: 'openai',
      ai_model: 'gpt-5',
      request_params: {},
    } as unknown as AnalysisResult
    render(<ReAnalyzeDialog open onOpenChange={() => {}} result={resolved} jobId="job-1" />)
    expect(screen.getByTestId('provider-primary')).toHaveTextContent('openai')
    expect(screen.getByTestId('model-primary')).toHaveTextContent('gpt-5')
    expect(screen.queryByText(/claude/i)).not.toBeInTheDocument()
  })

  it('defers to server resolution when no provider/model is known: submit allowed, body omits them', async () => {
    mockPost.mockResolvedValue({ job_id: 'job-2' })
    const empty = {
      ...result,
      ai_provider: '',
      ai_model: '',
      request_params: {},
    } as unknown as AnalysisResult
    render(<ReAnalyzeDialog open onOpenChange={() => {}} result={empty} jobId="job-1" />)
    fireEvent.click(screen.getByRole('button', { name: /re-analyze/i }))
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('Select an available AI provider and model.')).not.toBeInTheDocument()
    const body = submittedBody()
    expect(body).not.toHaveProperty('ai_provider')
    expect(body).not.toHaveProperty('ai_model')
  })

  it('keeps explicit request_params as the prefill source', () => {
    render(<ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job-1" />)
    expect(screen.getByTestId('provider-primary')).toHaveTextContent('claude')
    expect(screen.getByTestId('model-primary')).toHaveTextContent('claude-sonnet-4')
  })

  it('drops empty peer rows and keeps them empty — no claude fallback', async () => {
    mockPost.mockResolvedValue({ job_id: 'job-2' })
    const withPeers = {
      ...result,
      ai_provider: 'claude',
      ai_model: 'claude-sonnet-4',
      request_params: {
        ai_provider: 'claude',
        ai_model: 'claude-sonnet-4',
        peer_ai_configs: [
          { ai_provider: '', ai_model: '' },
          { ai_provider: 'openai', ai_model: 'gpt-5' },
        ],
      },
    } as unknown as AnalysisResult
    render(<ReAnalyzeDialog open onOpenChange={() => {}} result={withPeers} jobId="job-1" />)
    expect(screen.getByTestId('provider-Peer 1 provider')).toHaveTextContent('')
    expect(screen.getByTestId('provider-Peer 2 provider')).toHaveTextContent('openai')
    fireEvent.click(screen.getByRole('button', { name: /re-analyze/i }))
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1))
    expect(submittedBody().peer_ai_configs).toEqual([
      { ai_provider: 'openai', ai_model: 'gpt-5' },
    ])
  })

  it('blocks submit when peer review is enabled but every peer row is empty', () => {
    const emptyPeers = {
      ...result,
      ai_provider: 'claude',
      ai_model: 'claude-sonnet-4',
      request_params: {
        ai_provider: 'claude',
        ai_model: 'claude-sonnet-4',
        peer_ai_configs: [{ ai_provider: '', ai_model: '' }],
      },
    } as unknown as AnalysisResult
    render(<ReAnalyzeDialog open onOpenChange={() => {}} result={emptyPeers} jobId="job-1" />)
    expect(screen.getByRole('button', { name: /re-analyze/i })).toBeDisabled()
    expect(mockPost).not.toHaveBeenCalled()
  })
})
