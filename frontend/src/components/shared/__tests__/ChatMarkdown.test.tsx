import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ChatMarkdown } from '../ChatMarkdown'

describe('ChatMarkdown', () => {
  it('renders plain text', () => {
    render(<ChatMarkdown content="Hello world" />)
    expect(screen.getByText('Hello world')).toBeDefined()
  })

  it('renders regular links with target _blank', () => {
    render(<ChatMarkdown content="See [docs](https://example.com) for details" />)
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('https://example.com')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
  })

  it('renders artifact URLs as download buttons', () => {
    render(
      <ChatMarkdown content="Here is your report: [Download Report](/api/admin-chat/artifacts/abc-123)" />
    )
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('/api/admin-chat/artifacts/abc-123')
    expect(link.hasAttribute('download')).toBe(true)
    expect(link.textContent).toContain('Download Report')
    // Should have the download button styling
    expect(link.className).toContain('bg-accent-blue')
  })

  it('does not render download button for non-artifact URLs', () => {
    render(<ChatMarkdown content="[Link](https://example.com)" />)
    const link = screen.getByRole('link')
    expect(link.hasAttribute('download')).toBe(false)
    expect(link.className).not.toContain('bg-accent-blue')
  })

  it('renders markdown tables', () => {
    const table = '| Name | Count |\n|------|-------|\n| test | 5 |'
    render(<ChatMarkdown content={table} />)
    expect(screen.getByRole('table')).toBeDefined()
  })

  it('strips unsafe href links', () => {
    render(<ChatMarkdown content="[evil](javascript:alert(1))" />)
    const span = screen.getByText('evil')
    expect(span.tagName).toBe('SPAN')
  })
})
