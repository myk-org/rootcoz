import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { isSafeHref } from '@/lib/autoLink'

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
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            if (!href || !isSafeHref(href)) {
              return <span>{children}</span>
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            )
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
