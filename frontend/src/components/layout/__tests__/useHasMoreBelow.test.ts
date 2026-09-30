import { describe, it, expect } from 'vitest'
import { act, fireEvent, renderHook } from '@testing-library/react'
import { useHasMoreBelow } from '../useHasMoreBelow'

/** JSDOM does no layout, so the scroll metrics are stubbed per element. */
function stubScroll(el: HTMLElement, metrics: { scrollHeight: number; clientHeight: number; scrollTop?: number }) {
  Object.defineProperty(el, 'scrollHeight', { value: metrics.scrollHeight, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: metrics.clientHeight, configurable: true })
  el.scrollTop = metrics.scrollTop ?? 0
}

describe('useHasMoreBelow', () => {
  it('reports content below the fold and clears once scrolled to the end', () => {
    const { result } = renderHook(() => useHasMoreBelow<HTMLElement>())
    const el = document.createElement('div')
    stubScroll(el, { scrollHeight: 1200, clientHeight: 600 })
    act(() => { result.current.ref(el) })

    expect(result.current.hasMore).toBe(true)
    stubScroll(el, { scrollHeight: 1200, clientHeight: 600, scrollTop: 600 })
    act(() => { el.dispatchEvent(new Event('scroll')) })
    expect(result.current.hasMore).toBe(false)
  })

  it('reports no more content when the content fits the viewport', () => {
    const { result } = renderHook(() => useHasMoreBelow<HTMLElement>())
    const el = document.createElement('div')
    stubScroll(el, { scrollHeight: 400, clientHeight: 600 })
    act(() => { result.current.ref(el) })
    expect(result.current.hasMore).toBe(false)
  })

  it('re-measures on scroll events attached to the container', () => {
    const { result } = renderHook(() => useHasMoreBelow<HTMLElement>())
    const el = document.createElement('div')
    stubScroll(el, { scrollHeight: 900, clientHeight: 300 })
    act(() => { result.current.ref(el) })
    expect(result.current.hasMore).toBe(true)
    stubScroll(el, { scrollHeight: 900, clientHeight: 300, scrollTop: 600 })
    act(() => { fireEvent.scroll(el) })
    expect(result.current.hasMore).toBe(false)
  })
})
