import { Fragment, useMemo, type ReactNode } from 'react'
import { autoLinkAnalysis, isSafeHref, type RepoUrl, type LinkSegment } from '@/lib/autoLink'

interface LinkedTextProps {
  text: string
  repoUrls: RepoUrl[]
  /** Custom renderer for link segments. */
  renderLink?: (seg: LinkSegment, index: number) => ReactNode
  /** Custom renderer for plain-text segments (e.g. @mention highlighting). */
  renderText?: (text: string, index: number) => ReactNode
}

export function LinkedText({ text, repoUrls, renderLink, renderText }: LinkedTextProps) {
  const segments = useMemo<LinkSegment[]>(() => autoLinkAnalysis(text, repoUrls), [text, repoUrls])

  return (
    <>
      {segments.map((seg, i) =>
        seg.type === 'link' && seg.href && isSafeHref(seg.href) ? (
          renderLink ? <Fragment key={i}>{renderLink(seg, i)}</Fragment> : (
            <a key={i} href={seg.href} target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">
              {seg.text}
            </a>
          )
        ) : (
          renderText ? <Fragment key={i}>{renderText(seg.text, i)}</Fragment> : <span key={i}>{seg.text}</span>
        )
      )}
    </>
  )
}
