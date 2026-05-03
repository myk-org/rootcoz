import { useId, useState } from 'react'
import { ChevronRight } from 'lucide-react'

export function Section({
  title,
  dotColor,
  defaultOpen = false,
  children,
}: {
  title: string
  dotColor: string
  defaultOpen?: boolean
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  const id = useId()
  const contentId = `${id}-content`
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={contentId}
        className="w-full flex items-center gap-2 py-2 text-left group"
      >
        <ChevronRight
          className={`h-3 w-3 text-text-tertiary transition-transform duration-200 ${open ? 'rotate-90' : ''}`}
        />
        <span className={`w-1.5 h-1.5 rounded-full ${dotColor} flex-shrink-0`} />
        <span className="font-display text-[11px] font-semibold tracking-widest text-text-secondary uppercase">
          {title}
        </span>
      </button>
      {open && <div id={contentId} className="pl-5 space-y-4 pb-4">{children}</div>}
    </div>
  )
}
