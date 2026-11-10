import { Badge } from '@/components/ui/badge'

export interface IssueMatchItem {
  label: string
  url: string
  status?: string
}

/**
 * A match URL that can open a ticket ('' = render the label unlinked).
 *
 * A stored match may carry no URL (a Jira gateway connection with no site
 * URL) or the scoped api.atlassian.com API gateway itself; browsing either
 * lands on nothing, so the match renders as plain text instead (#496).
 */
function matchHref(url: string): string {
  try {
    const parsed = new URL(url) // throws for '' and for a relative URL
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return ''
    return parsed.hostname.toLowerCase() === 'api.atlassian.com' ? '' : url
  } catch {
    return ''
  }
}

/**
 * One tracker's duplicate matches, rendered the one way this app does it:
 * optional heading, a linked label, a status badge. Used by the bug report
 * (Jira and GitHub duplicates) and by the bug-creation dialog's similar issues.
 */
export function IssueMatchList({ heading, items, testId }: {
  heading?: string
  items: IssueMatchItem[]
  testId?: string
}) {
  if (items.length === 0) return null
  return (
    <div className="mt-2" data-testid={testId}>
      {heading && (
        <p className="text-xs font-display uppercase tracking-widest text-text-tertiary mb-1">{heading}</p>
      )}
      <ul className="space-y-1">
        {items.map((item) => {
          const href = matchHref(item.url)
          return (
            <li key={item.url || item.label} className="flex items-center gap-2 text-xs">
              {href ? (
                <a href={href} target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">
                  {item.label}
                </a>
              ) : (
                <span>{item.label}</span>
              )}
              {item.status && <Badge variant="outline" className="text-[10px]">{item.status}</Badge>}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
