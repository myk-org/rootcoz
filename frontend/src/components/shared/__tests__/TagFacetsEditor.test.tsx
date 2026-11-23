import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { TagFacetsEditor } from '../TagFacetsEditor'
import { api } from '@/lib/api'
import type { TagFacet } from '@/types'

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}))

const mockGet = vi.mocked(api.get)

describe('TagFacetsEditor (#658)', () => {
  beforeEach(() => {
    mockGet.mockReset()
    mockGet.mockResolvedValue([
      { job_name: 'job-1', team: null, tier: null, version: null, labels: ['aws', 'bare-metal', 'io2-csi'] },
    ] as never)
  })

  it('adds a facet, auto-slugifies ID, and picks tags strictly from live metadata labels', async () => {
    const onChange = vi.fn()
    const { rerender } = render(<TagFacetsEditor facets={[]} onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: /Add facet/i }))
    expect(onChange).toHaveBeenCalledWith([
      { id: '', name: '', options: [{ label: '', tags: [] }] },
    ])

    const added: TagFacet[] = [{ id: '', name: '', options: [{ label: '', tags: [] }] }]
    rerender(<TagFacetsEditor facets={added} onChange={onChange} />)

    fireEvent.change(screen.getByLabelText('Facet name'), { target: { value: 'Storage class' } })
    expect(onChange).toHaveBeenLastCalledWith([
      { id: 'storage-class', name: 'Storage class', options: [{ label: '', tags: [] }] },
    ])

    // Open tag picker — only live tags appear, no free-text custom add option
    const picker = screen.getByRole('combobox', { name: /Add tags for option 1/i })
    fireEvent.click(picker)
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'aws' })).toBeInTheDocument()
    })
    fireEvent.change(screen.getByRole('combobox', { name: /Search tags for option 1/i }), {
      target: { value: 'nonexistent-custom-tag' },
    })
    expect(screen.queryByRole('option', { name: /Add "nonexistent-custom-tag"/ })).toBeNull()
  })

  it('shows a missing-tag warning naming disappeared tags without rewriting the config', async () => {
    const facets: TagFacet[] = [
      {
        id: 'platform',
        name: 'Platform',
        options: [
          { label: 'AWS', tags: ['aws'] },
          { label: 'Legacy', tags: ['dropped-tag'] },
        ],
      },
    ]
    const onChange = vi.fn()
    render(<TagFacetsEditor facets={facets} onChange={onChange} />)

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/Missing tag:\s*dropped-tag/i)
    })
    // Config was never silently mutated
    expect(onChange).not.toHaveBeenCalled()
  })

  it('supports reordering facets and options', async () => {
    const facets: TagFacet[] = [
      {
        id: 'platform',
        name: 'Platform',
        options: [
          { label: 'AWS', tags: ['aws'] },
          { label: 'Bare metal', tags: ['bare-metal'] },
        ],
      },
      {
        id: 'storage',
        name: 'Storage',
        options: [{ label: 'io2', tags: ['io2-csi'] }],
      },
    ]
    const onChange = vi.fn()
    render(<TagFacetsEditor facets={facets} onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: 'Move facet Platform down' }))
    expect(onChange).toHaveBeenLastCalledWith([facets[1], facets[0]])

    fireEvent.click(screen.getByRole('button', { name: 'Move option AWS down' }))
    expect(onChange).toHaveBeenLastCalledWith([
      {
        ...facets[0],
        options: [facets[0].options[1], facets[0].options[0]],
      },
      facets[1],
    ])
  })
})
