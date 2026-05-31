import ReactMarkdown from 'react-markdown'

interface ChatMarkdownProps {
  content: string
}

export function ChatMarkdown({ content }: ChatMarkdownProps) {
  return (
    <div className="prose prose-sm prose-invert max-w-none break-words
      prose-p:my-1 prose-ul:my-1 prose-ol:my-1 prose-li:my-0.5
      prose-headings:my-2 prose-headings:text-text-primary
      prose-a:text-text-link prose-a:hover:underline
      prose-code:text-text-secondary prose-code:bg-surface-elevated prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-xs
      prose-pre:bg-surface-elevated prose-pre:rounded-md prose-pre:p-3 prose-pre:text-xs
      prose-table:text-xs prose-th:text-left prose-th:p-2 prose-td:p-2
      prose-strong:text-text-primary">
      <ReactMarkdown>{content}</ReactMarkdown>
    </div>
  )
}
