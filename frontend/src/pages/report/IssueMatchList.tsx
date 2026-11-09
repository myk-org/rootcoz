import { Badge } from '@/components/ui/badge'

export interface IssueMatchItem {
  label: string
  url: string
  status?: string
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
        {items.map((item) => (
          <li key={item.url || item.label} className="flex items-center gap-2 text-xs">
            <a href={item.url} target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">
              {item.label}
            </a>
            {item.status && <Badge variant="outline" className="text-[10px]">{item.status}</Badge>}
          </li>
        ))}
      </ul>
    </div>
  )
}
