import { Copy, Check } from 'lucide-react'

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
      <button
        type="button"
        className="text-text-tertiary hover:text-text-primary transition-colors shrink-0"
        onClick={(e) => { e.stopPropagation(); onCopy(uuid, sectionKey) }}
        title={isCopied ? 'Copied UUID' : 'Copy UUID to clipboard'}
        aria-label={isCopied ? 'Copied UUID' : 'Copy UUID to clipboard'}
      >
        {isCopied ? <Check className="h-3 w-3 text-signal-green" /> : <Copy className="h-3 w-3" />}
      </button>
    </>
  )
}
