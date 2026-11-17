import { beforeEach, describe, expect, it } from 'vitest'
import { clearUrlFragment, stripQueryParams } from '@/lib/navigation'

/**
 * The state React Router keeps on the entry — `from` is how a protected page is
 * remembered, so a cleanup that drops it loses the return destination.
 */
const ROUTER_STATE = { usr: { from: { pathname: '/report/1' } }, key: 'abc123', idx: 2 }

function goTo(url: string, state: unknown = ROUTER_STATE) {
  window.history.replaceState(state, '', url)
}

describe('navigation URL cleanup', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('drops the named query params and keeps the rest', () => {
    goTo('/login?sso_error=denied&keep=1')
    stripQueryParams('sso_error')
    expect(window.location.pathname).toBe('/login')
    expect(window.location.search).toBe('?keep=1')
  })

  it('keeps the history entry state when dropping query params', () => {
    goTo('/login?sso_error=denied')
    stripQueryParams('sso_error')
    expect(window.location.search).toBe('')
    expect(window.history.state).toEqual(ROUTER_STATE)
  })

  it('leaves the URL and its state alone when no param matched', () => {
    goTo('/login?keep=1')
    stripQueryParams('sso_error')
    expect(window.location.search).toBe('?keep=1')
    expect(window.history.state).toEqual(ROUTER_STATE)
  })

  it('drops the fragment and keeps the path and query', () => {
    goTo('/login?keep=1#sso_ticket=tkt-1') // pragma: allowlist secret
    clearUrlFragment()
    expect(window.location.pathname).toBe('/login')
    expect(window.location.search).toBe('?keep=1')
    expect(window.location.hash).toBe('')
  })

  it('keeps the history entry state when dropping the fragment', () => {
    goTo('/login#sso_ticket=tkt-1') // pragma: allowlist secret
    clearUrlFragment()
    expect(window.history.state).toEqual(ROUTER_STATE)
  })

  it('leaves the history entry state alone when there is no fragment', () => {
    goTo('/login')
    clearUrlFragment()
    expect(window.location.href).toBe(`${window.location.origin}/login`)
    expect(window.history.state).toEqual(ROUTER_STATE)
  })
})
