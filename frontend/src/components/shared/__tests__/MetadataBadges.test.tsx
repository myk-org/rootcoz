import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import {
  MetadataBadges,
  JobTagBadges,
  JobMetadataAndTags,
} from '@/components/shared/MetadataBadges'

describe('MetadataBadges', () => {
  it('renders nothing for null, undefined, or empty metadata', () => {
    const { container: c1 } = render(<MetadataBadges metadata={null} />)
    expect(c1).toBeEmptyDOMElement()

    const { container: c2 } = render(<MetadataBadges metadata={undefined} />)
    expect(c2).toBeEmptyDOMElement()

    const { container: c3 } = render(
      <MetadataBadges metadata={{ job_name: 'j', team: null, tier: null, version: null, labels: [] }} />,
    )
    expect(c3).toBeEmptyDOMElement()
  })

  it('renders team, tier, version, and labels with theme tokens', () => {
    const metadata = {
      job_name: 'mtv-2.12-gate',
      team: 'mtv',
      tier: 'critical',
      version: '2.12',
      labels: ['gating', 'vsphere8'],
    }

    render(<MetadataBadges metadata={metadata} />)

    const team = screen.getByText('mtv')
    expect(team.className).toContain('text-signal-blue')
    expect(team.className).toContain('border-signal-blue/30')

    const tier = screen.getByText('critical')
    expect(tier.className).toContain('text-signal-red')

    expect(screen.getByText('2.12')).toBeInTheDocument()

    const gating = screen.getByText('gating')
    expect(gating.className).toContain('text-signal-green')
    expect(screen.getByText('vsphere8')).toBeInTheDocument()
  })
})

describe('JobTagBadges', () => {
  it('renders nothing when tags are empty or blank', () => {
    const { container: c1 } = render(<JobTagBadges tags={undefined} />)
    expect(c1).toBeEmptyDOMElement()

    const { container: c2 } = render(<JobTagBadges tags={[]} />)
    expect(c2).toBeEmptyDOMElement()

    const { container: c3 } = render(<JobTagBadges tags={['']} />)
    expect(c3).toBeEmptyDOMElement()
  })

  it('renders tags with distinct styling for re-analyze and deduplicates entries', () => {
    render(<JobTagBadges tags={['re-analyze', 'myakove', 'regression', 'myakove']} />)

    const reAnalyze = screen.getByText('re-analyze')
    expect(reAnalyze.className).toContain('text-signal-orange')
    expect(reAnalyze.className).toContain('bg-signal-orange/10')

    const userTags = screen.getAllByText('myakove')
    expect(userTags).toHaveLength(1)
    expect(userTags[0].className).toContain('text-signal-blue')
    expect(userTags[0].className).toContain('bg-signal-blue/10')

    expect(screen.getByText('regression')).toBeInTheDocument()
  })
})

describe('JobMetadataAndTags', () => {
  it('renders nothing when neither metadata badges nor tags are present', () => {
    const { container } = render(
      <JobMetadataAndTags
        metadata={{ job_name: 'j', team: null, tier: null, version: null, labels: [] }}
        tags={[]}
        testId="job-metadata-tags"
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders combined metadata badges and tag pills when either is present', () => {
    render(
      <JobMetadataAndTags
        metadata={{ job_name: 'j', team: 'mtv', tier: null, version: '2.12', labels: ['gating'] }}
        tags={['re-analyze']}
        testId="job-metadata-tags"
      />,
    )
    expect(screen.getByTestId('job-metadata-tags')).toBeInTheDocument()
    expect(screen.getByText('mtv')).toBeInTheDocument()
    expect(screen.getByText('2.12')).toBeInTheDocument()
    expect(screen.getByText('gating')).toBeInTheDocument()
    expect(screen.getByText('re-analyze')).toBeInTheDocument()
  })
})
