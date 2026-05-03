import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LinkedText } from '../LinkedText'
import type { RepoUrl } from '@/lib/autoLink'

describe('LinkedText', () => {
  it('renders plain text without links', () => {
    render(<LinkedText text="no links here" repoUrls={[]} />)
    expect(screen.getByText('no links here')).toBeDefined()
  })

  it('renders URLs as anchor tags with target _blank', () => {
    render(<LinkedText text="see https://example.com for details" repoUrls={[]} />)
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('https://example.com')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
  })

  it('renders file paths as links when repoUrls provided', () => {
    const repos: RepoUrl[] = [{ name: 'repo', url: 'https://github.com/org/repo', ref: 'main' }]
    render(<LinkedText text="edit conftest.py now" repoUrls={repos} />)
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('https://github.com/org/repo/blob/main/conftest.py')
  })

  it('uses renderLink callback when provided', () => {
    render(
      <LinkedText
        text="see https://example.com here"
        repoUrls={[]}
        renderLink={(seg, i) => <span key={i} data-testid="custom-link">{seg.text}</span>}
      />
    )
    expect(screen.getByTestId('custom-link')).toBeDefined()
  })
})
