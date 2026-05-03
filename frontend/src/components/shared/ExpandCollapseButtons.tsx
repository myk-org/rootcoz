interface ExpandCollapseButtonsProps {
  onExpandAll: () => void
  onCollapseAll: () => void
}

export function ExpandCollapseButtons({ onExpandAll, onCollapseAll }: ExpandCollapseButtonsProps) {
  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        onClick={onExpandAll}
        className="text-xs text-text-tertiary hover:text-text-primary transition-colors"
      >
        Expand All
      </button>
      <span className="text-text-tertiary text-xs">|</span>
      <button
        type="button"
        onClick={onCollapseAll}
        className="text-xs text-text-tertiary hover:text-text-primary transition-colors"
      >
        Collapse All
      </button>
    </div>
  )
}
