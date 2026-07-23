import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { CodeFixLiteralBlock } from '../FailureCard'
import { TooltipProvider } from '@/components/ui/tooltip'

function renderBlock(props: { title: string; content: string; className?: string }) {
  return render(
    <TooltipProvider>
      <CodeFixLiteralBlock className="text-green" {...props} />
    </TooltipProvider>,
  )
}

describe('CodeFixLiteralBlock', () => {
  beforeEach(() => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    })
  })

  it('renders actual newlines from literal \\n sequences', () => {
    renderBlock({
      title: 'Test Code',
      content: 'line1\\nline2\\nline3',
    })
    const pre = screen.getByText(/line1/)
    expect(pre.textContent).toBe('line1\nline2\nline3')
  })

  it('renders actual tabs from literal \\t sequences', () => {
    renderBlock({
      title: 'Test Code',
      content: 'if x:\\n\\treturn y',
    })
    const pre = screen.getByText(/if x:/)
    expect(pre.textContent).toBe('if x:\n\treturn y')
  })

  it('renders content unchanged when no escape sequences', () => {
    const content = 'normal code content'
    renderBlock({ title: 'Test Code', content })
    expect(screen.getByText(content)).toBeTruthy()
  })

  it('renders section title', () => {
    renderBlock({ title: 'Original Code', content: 'some code' })
    expect(screen.getByText('Original Code')).toBeTruthy()
  })

  it('copies unescaped suggested code via the copy button', async () => {
    renderBlock({
      title: 'Suggested Code',
      content: 'def fix():\\n    return 1',
    })
    fireEvent.click(screen.getByRole('button', { name: /Copy Suggested Code to clipboard/i }))
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('def fix():\n    return 1')
    })
  })
})
