import { Copy, Check } from 'lucide-react'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'

interface UuidCopyButtonProps {
  uuid: string
  sectionKey: string
  copiedSection: string | null
  onCopy: (text: string, section: string) => void
  showTruncated?: boolean
}

export function UuidCopyButton({ uuid, sectionKey, copiedSection, onCopy, showTruncated = true }: UuidCopyButtonProps) {
  const isCopied = copiedSection === sectionKey
  return (
    <>
      {showTruncated && <span className="font-mono text-[10px] text-text-tertiary">{uuid.slice(0, 8)}</span>}
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="text-text-tertiary hover:text-text-primary transition-colors shrink-0"
            onClick={(e) => { e.stopPropagation(); onCopy(uuid, sectionKey) }}
            aria-label={isCopied ? 'Copied UUID' : 'Copy UUID to clipboard'}
          >
            {isCopied ? <Check className="h-3 w-3 text-signal-green" /> : <Copy className="h-3 w-3" />}
          </button>
        </TooltipTrigger>
        <TooltipContent>{isCopied ? 'Copied UUID' : 'Copy UUID to clipboard'}</TooltipContent>
      </Tooltip>
    </>
  )
}
