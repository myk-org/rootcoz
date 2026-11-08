import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { it, expect, vi } from 'vitest'
import { useState } from 'react'
import { ModelCombobox } from './ModelCombobox'
import { AnalysisModelSelect } from './AnalysisAiPicker'

HTMLElement.prototype.scrollIntoView = () => {}

/** The picker reason the backend attaches to an unsessionable pair. */
const REASON =
  "AI session refused: this model is listed by the provider but is not declared in that provider's models[] and the listing reports no token limits. Declare it in the agent-dir models.json models[] — limits are optional there, pi's defaults apply — or pick a model that is declared or lists both limits."

const catalog = {
  providers: { enmaas: [{
    id: 'gemini-3.1-pro-preview', name: 'Gemini 3.1 Pro', provider: 'enmaas', credential_sources: ['server'],
    session_supported: false, session_unsupported_reason: REASON,
  }] },
  provider_status: {},
}
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: {
    get: async (path: string) => path.startsWith('/api/ai-models') ? catalog : {},
    post: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ username: 'alice', isAdmin: false, authenticated: true, canUseServerProviders: true, role: 'reviewer' }),
}))

it('keeps a denied server model disabled without a locked label', async () => {
  render(<ModelCombobox value="" onChange={() => {}} options={[{
    id: 'sonnet', name: 'Sonnet', credential_sources: ['server'], serverLocked: true,
  }]} forceServer canUseServer={false} />)
  const user = userEvent.setup()
  await user.keyboard('{Tab}{ArrowDown}{ArrowDown}')
  expect(screen.getByRole('combobox')).toHaveFocus()
  expect(screen.getByRole('combobox')).toHaveAccessibleDescription('Organization access is unavailable. Ask an admin for access.')
  const option = screen.getByRole('option', { name: /sonnet.*Server/i })
  expect(option).toHaveAttribute('aria-disabled', 'true')
  expect(option).not.toHaveTextContent('locked')
})

it('mutes only Server on mixed models and explains access on hover and focus', async () => {
  const user = userEvent.setup()
  render(<ModelCombobox value="" onChange={() => {}} options={[{
    id: 'shared', name: 'Shared', credential_sources: ['user', 'server'],
  }]} canUseServer={false} />)
  const input = screen.getByRole('combobox')
  await user.keyboard('{Tab}{ArrowDown}{ArrowDown}')
  expect(input).toHaveFocus()
  expect(input).toHaveAccessibleDescription('Organization access is unavailable. Ask an admin for access.')
  const option = screen.getByRole('option', { name: /shared.*User \+ Server/ })
  expect(option).not.toHaveAttribute('aria-disabled')
  expect(within(option).getByText('Server')).toHaveClass('text-text-tertiary')
  await user.hover(option)
  expect(await screen.findByRole('tooltip')).toHaveTextContent('Organization access is unavailable. Ask an admin for access.')
  await user.unhover(option)
  await user.keyboard('{ArrowUp}{Tab}')
  expect(input).not.toHaveFocus()
})

// Regression: the model box used to render read-only whenever the provider's
// models were "verified" (strict={!unverified}), so users could only pick from
// the list and could not type a model ID. It must always be a real combobox.
it('always accepts typed model IDs, including ones not in the list', async () => {
  const user = userEvent.setup()
  const committed = vi.fn()
  function Harness() {
    const [value, setValue] = useState('')
    return <ModelCombobox value={value} onChange={setValue} onCommit={committed} options={[{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' }]} />
  }
  render(<Harness />)
  const input = screen.getByRole('combobox')
  expect(input).not.toHaveAttribute('readonly')

  await user.type(input, 'gpt-5.6-luna')
  expect(input).toHaveValue('gpt-5.6-luna')
  // Enter with no highlighted option must commit the free-typed id, not just close.
  await user.keyboard('{Enter}')
  expect(committed).toHaveBeenLastCalledWith('gpt-5.6-luna')
})

it('keeps the typed value verbatim instead of decorating it as unavailable', () => {
  render(<ModelCombobox value="not-in-catalog" onChange={() => {}} options={[{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' }]} />)
  expect(screen.getByRole('combobox')).toHaveValue('not-in-catalog')
})

it('still filters the dropdown while typing', async () => {
  const user = userEvent.setup()
  render(<ModelCombobox value="2.5" onChange={() => {}} options={[
    { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' },
    { id: 'claude-opus-4-6-1m', name: 'Claude Opus' },
  ]} />)
  await user.click(screen.getByRole('combobox'))
  const options = screen.getAllByRole('option')
  expect(options).toHaveLength(1)
  expect(options[0]).toHaveTextContent('gemini-2.5-pro')
})

// Regression guard for the dead-end the fix above could otherwise introduce: a
// stale stored model must not empty the dropdown, or the user can neither type
// a new id nor pick a replacement.
it('still offers the full list when the stored value matches nothing', async () => {
  const user = userEvent.setup()
  render(<ModelCombobox value="missing-model" onChange={() => {}} options={[
    { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' },
    { id: 'claude-opus-4-6-1m', name: 'Claude Opus' },
  ]} />)
  await user.click(screen.getByRole('combobox'))
  expect(screen.getAllByRole('option')).toHaveLength(2)
})

it('shows a Free badge on admin-declared free models only', async () => {
  const user = userEvent.setup()
  render(<ModelCombobox value="" onChange={() => {}} options={[{
    id: 'glm-5-3', name: 'GLM 5.3', credential_sources: ['server'], free: true,
  }, {
    id: 'paid', name: 'Paid', credential_sources: ['server'],
  }]} canUseServer />)
  await user.keyboard('{Tab}{ArrowDown}{ArrowDown}')
  const freeOption = screen.getByRole('option', { name: /glm-5-3/ })
  expect(within(freeOption).getByText('Free')).toBeInTheDocument()
  const paidOption = screen.getByRole('option', { name: /^paid/i })
  expect(within(paidOption).queryByText('Free')).not.toBeInTheDocument()
})

// #423 (test 15): AnalysisModelSelect passes the flag explicitly, because the
// combobox never defaults it on — the test below it is that negative twin. A
// model the sidecar would refuse cannot be picked, and the row carries the why.
it('refuses to select a model without session support', async () => {
  const user = userEvent.setup()
  function Harness() {
    const [value, setValue] = useState('')
    return <AnalysisModelSelect provider="enmaas" value={value} onChange={setValue} forceServer={false} />
  }
  render(<Harness />)
  const input = await screen.findByRole('combobox', { name: 'AI Model' })
  await user.click(input)
  const blocked = await screen.findByRole('option', { name: /gemini-3\.1-pro-preview/ })
  expect(blocked).toHaveAttribute('aria-disabled', 'true')

  // The reason, not a bare "unavailable": it names the manifest fix.
  await user.hover(blocked)
  expect(await screen.findByRole('tooltip')).toHaveTextContent(REASON)
  await user.unhover(blocked)

  await user.click(blocked)
  expect(input).toHaveValue('')
})

// #423: the lock covers the commit points too — a typed id of a NO SESSION row
// is the same pair the list refuses, so it must not become the committed value.
it('refuses to commit a typed id that matches a session-refused row', async () => {
  const user = userEvent.setup()
  const committed = vi.fn()
  const locked = {
    id: 'gemini-3.1-pro-preview', name: 'Gemini 3.1 Pro',
    session_supported: false, session_unsupported_reason: REASON,
  }
  function Harness() {
    const [value, setValue] = useState('')
    return <ModelCombobox value={value} onChange={setValue} onCommit={committed} disableUnsessionable options={[locked]} />
  }
  render(<Harness />)
  const input = screen.getByRole('combobox')
  await user.type(input, 'gemini-3.1-pro-preview')
  // The draft is typeable — a refused id may be the prefix of a usable one — but
  // a blur or Enter cannot commit it, so the pair can never be started.
  expect(input).toHaveValue('gemini-3.1-pro-preview')
  await user.keyboard('{Enter}')
  await user.tab()
  expect(committed).not.toHaveBeenCalled()
})

// #423: a refused id that prefixes a usable one must not lock the keystrokes of
// the usable one — the lock lives on the commit, not on the draft.
it('lets a usable id that extends a refused id be typed and committed', async () => {
  const user = userEvent.setup()
  const committed = vi.fn()
  const options = [
    { id: 'gpt-5', name: 'GPT-5', session_supported: false, session_unsupported_reason: REASON },
    { id: 'gpt-5-mini', name: 'GPT-5 mini' },
  ]
  function Harness() {
    const [value, setValue] = useState('')
    return <ModelCombobox value={value} onChange={setValue} onCommit={committed} disableUnsessionable options={options} />
  }
  render(<Harness />)
  const input = screen.getByRole('combobox')
  await user.type(input, 'gpt-5-mini')
  expect(input).toHaveValue('gpt-5-mini')
  await user.keyboard('{Enter}')
  expect(committed).toHaveBeenLastCalledWith('gpt-5-mini')
})

// #423: the flag is opt-in at the call site (AnalysisModelSelect passes it), so
// the admin key-verification pickers keep offering a listed-but-undeclared id —
// they exist to verify such a key. Defaulting it on here would fail this.
it('keeps an unsessionable option selectable when the prop is absent', async () => {
  const user = userEvent.setup()
  const onChange = vi.fn()
  render(<ModelCombobox value="" onChange={onChange} options={[{
    id: 'listed-only', name: 'Listed only', session_supported: false,
    session_unsupported_reason: 'no declared token limits',
  }]} />)
  await user.click(screen.getByRole('combobox'))
  const option = screen.getByRole('option', { name: /listed-only/ })
  expect(option).not.toHaveAttribute('aria-disabled')
  await user.click(option)
  expect(onChange).toHaveBeenCalledWith('listed-only')
  expect(screen.queryByText('NO SESSION')).not.toBeInTheDocument()
})
