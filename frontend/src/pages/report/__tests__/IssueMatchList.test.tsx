import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { IssueMatchList } from '../IssueMatchList'

const CLOUD_ID = '2b9e35e3-6bd3-4cec-b838-f4249ee02432'

// No anchor may exist for any of these: an href that is empty, relative or an
// API gateway URL opens no ticket, and the label is all the reader gets (#496).
const BROKEN: [string, string][] = [
  ['no URL', ''],
  ['a scoped API gateway URL', `https://api.atlassian.com/ex/jira/${CLOUD_ID}/browse/CNV-1`],
  ['a dotted API gateway URL', `https://api.atlassian.com./ex/jira/${CLOUD_ID}/browse/CNV-1`],
  ['a relative URL', '/browse/CNV-1'],
  ['a non-http scheme', 'javascript:alert(1)'],
]

describe('IssueMatchList links', () => {
  it('links a site browse URL', () => {
    render(
      <IssueMatchList items={[{ label: 'CNV-1: dup', url: 'https://redhat.atlassian.net/browse/CNV-1' }]} />,
    )
    expect(screen.getByRole('link', { name: 'CNV-1: dup' }).getAttribute('href')).toBe(
      'https://redhat.atlassian.net/browse/CNV-1',
    )
  })

  it.each(BROKEN)('renders the label unlinked for %s (#496)', (_name, url) => {
    const { container } = render(<IssueMatchList items={[{ label: 'CNV-1: dup', url }]} />)
    expect(container.querySelector('a')).toBeNull()
    expect(screen.getByText('CNV-1: dup')).toBeTruthy()
  })
})
