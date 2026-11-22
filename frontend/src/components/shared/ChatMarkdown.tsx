import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { isSafeHref } from '@/lib/autoLink'
import { cn } from '@/lib/utils'
import { Download } from 'lucide-react'

interface ChatMarkdownProps {
  content: string
  /** Allow /api/admin-chat/artifacts/ links to render as download buttons (default: true). */
  allowArtifactLinks?: boolean
  /** Render Markdown images as <img> tags; false turns them into safe external links to prevent silent third-party requests (default: true). */
  allowImages?: boolean
  className?: string
}

const ARTIFACT_URL_PATTERN = '/api/admin-chat/artifacts/'

export function ChatMarkdown({ content, allowArtifactLinks = true, allowImages = true, className }: ChatMarkdownProps) {
  return (
    <div className={cn(
      'prose prose-sm max-w-none break-words',
      'prose-p:my-1 prose-ul:my-1 prose-ol:my-1 prose-li:my-0.5',
      'prose-headings:my-2 prose-headings:text-text-primary',
      'prose-a:text-text-link prose-a:hover:underline',
      'prose-code:text-text-secondary prose-code:bg-surface-elevated prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:before:content-none prose-code:after:content-none',
      'prose-pre:bg-surface-elevated prose-pre:rounded-md prose-pre:p-3 prose-pre:text-xs',
      'prose-table:text-xs prose-th:text-left prose-th:p-2 prose-td:p-2',
      'prose-strong:text-text-primary',
      className,
    )}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            if (!href || !isSafeHref(href)) {
              return <span>{children}</span>
            }
            if (allowArtifactLinks && href.startsWith(ARTIFACT_URL_PATTERN)) {
              return (
                <a
                  href={href}
                  download
                  className="no-underline inline-flex items-center gap-2 px-3 py-1.5 rounded-md
                    bg-accent-blue/15 text-accent-blue hover:bg-accent-blue/25
                    text-xs font-medium transition-colors"
                >
                  <Download className="h-3.5 w-3.5" />
                  {children}
                </a>
              )
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            )
          },
          img: ({ src, alt }) => {
            if (!src || !isSafeHref(src)) {
              return <span>{alt}</span>
            }
            if (!allowImages) {
              return (
                <a href={src} target="_blank" rel="noopener noreferrer">
                  {alt || src}
                </a>
              )
            }
            return <img src={src} alt={alt ?? ''} />
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
