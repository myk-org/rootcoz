import { useReportDispatch, useReportState } from './ReportContext'

/**
 * One AI pair per report page, shared by every AI-assisted surface (issue creation,
 * comment intent). The first surface to set a choice decides it for the page; until
 * then each surface shows its own default. Reopening a dialog on another failure
 * keeps the choice instead of silently reverting to a state initialiser.
 */
export function useAiSelection(defaultProvider = '', defaultModel = '') {
  const { aiSelection } = useReportState()
  const dispatch = useReportDispatch()
  const provider = aiSelection.ai_provider || defaultProvider
  const model = aiSelection.ai_provider ? aiSelection.ai_model : defaultModel
  return {
    aiProvider: provider,
    aiModel: model,
    setAiPair: (nextProvider: string, nextModel: string) =>
      dispatch({ type: 'SET_AI_SELECTION', payload: { ai_provider: nextProvider, ai_model: nextModel } }),
  }
}
