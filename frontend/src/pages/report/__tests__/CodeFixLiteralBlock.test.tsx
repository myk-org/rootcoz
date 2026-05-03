import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CodeFixLiteralBlock } from '../FailureCard'

describe('CodeFixLiteralBlock', () => {
  it('renders actual newlines from literal \\n sequences', () => {
    render(
      <CodeFixLiteralBlock
        title="Test Code"
        content={'line1\\nline2\\nline3'}
        className="text-green"
      />,
    )
    const pre = screen.getByText(/line1/)
    expect(pre.textContent).toBe('line1\nline2\nline3')
  })

  it('renders actual tabs from literal \\t sequences', () => {
    render(
      <CodeFixLiteralBlock
        title="Test Code"
        content={'if x:\\n\\treturn y'}
        className="text-green"
      />,
    )
    const pre = screen.getByText(/if x:/)
    expect(pre.textContent).toBe('if x:\n\treturn y')
  })

  it('renders content unchanged when no escape sequences', () => {
    const content = 'normal code content'
    render(
      <CodeFixLiteralBlock
        title="Test Code"
        content={content}
        className="text-green"
      />,
    )
    expect(screen.getByText(content)).toBeTruthy()
  })

  it('renders section title', () => {
    render(
      <CodeFixLiteralBlock
        title="Original Code"
        content="some code"
        className="text-green"
      />,
    )
    expect(screen.getByText('Original Code')).toBeTruthy()
  })
})
