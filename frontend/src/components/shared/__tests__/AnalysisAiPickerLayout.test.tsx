import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import {
  AnalysisModelNotice,
  AnalysisModelSelect,
} from '@/components/shared/AnalysisAiPicker'
import { UNVERIFIED_MODELS_HELP } from '@/components/shared/CredentialSourceLabel'
import { TooltipProvider } from '@/components/ui/tooltip'

// A provider whose key-scoped listing is unavailable: the sidecar refuses to
// send the key, so the model list is a stale suggestion set, not a verified one.
const providerStatus = { openrouter: { has_api_key: true, modelListingSupported: false } }

vi.mock('@/lib/useProviderOptions', () => ({
  useProviderCatalog: () => ({
    providers: [
      {
        provider: 'openrouter',
        name: 'OpenRouter',
        models: [{ id: 'stealth/space-bunny-alpha', name: 'Space Bunny Alpha', verified: true }],
      },
    ],
    providerStatus,
  }),
}))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ canUseServerProviders: false }),
}))

const renderPicker = (ui: React.ReactElement) =>
  render(<TooltipProvider>{ui}</TooltipProvider>)

describe('AnalysisModelSelect layout', () => {
  it('renders a single root so the caller lays out one item, not two', () => {
    // A fragment here became an extra flex/grid cell, pushing the notice beside
    // the picker and into the next grid column.
    const { container } = renderPicker(
      <AnalysisModelSelect provider="openrouter" value="" onChange={() => {}} forceServer={false} />,
    )
    const roots = Array.from(container.children)
    expect(roots).toHaveLength(1)
    expect(roots[0].tagName).toBe('DIV')
  })

  it('keeps the unverified notice inside that root, under the picker', () => {
    const { container } = renderPicker(
      <AnalysisModelSelect provider="openrouter" value="" onChange={() => {}} forceServer={false} />,
    )
    const root = container.children[0]
    expect(root.textContent).toContain(UNVERIFIED_MODELS_HELP)
    expect(screen.getByPlaceholderText('Enter model ID')).toBeInTheDocument()
  })

  it('omits the notice when the caller hoists it below a control row', () => {
    renderPicker(
      <AnalysisModelSelect provider="openrouter" value="" onChange={() => {}} forceServer={false} hideNotice />,
    )
    expect(screen.queryByText(UNVERIFIED_MODELS_HELP)).not.toBeInTheDocument()
  })

  it('offers the same notice separately for row layouts', () => {
    renderPicker(<AnalysisModelNotice provider="openrouter" forceServer={false} />)
    expect(screen.getByText(UNVERIFIED_MODELS_HELP)).toBeInTheDocument()
  })

  it('shows no notice for a provider whose key is verified', () => {
    providerStatus.openrouter.modelListingSupported = true
    renderPicker(<AnalysisModelNotice provider="openrouter" forceServer={false} />)
    expect(screen.queryByText(UNVERIFIED_MODELS_HELP)).not.toBeInTheDocument()
    providerStatus.openrouter.modelListingSupported = false
  })
})
describe('unlistable provider wording', () => {
  it('names the bundled snapshot rather than only saying "unverified"', () => {
    // modelListingSupported is false, so these IDs come from the frozen
    // pi_model_suggestions.json fallback — the wording has to say so.
    renderPicker(<AnalysisModelNotice provider="openrouter" forceServer={false} />)
    expect(screen.getByText(UNVERIFIED_MODELS_HELP)).toBeInTheDocument()
    expect(UNVERIFIED_MODELS_HELP).toMatch(/bundled suggestions/)
    expect(UNVERIFIED_MODELS_HELP).toMatch(/Newer models are missing/)
  })
})
