export function SectionDivider({ title }: { title: string }) {
  return (
    <div className="flex items-center gap-3 py-1">
      <div className="h-px flex-1 bg-border-muted" />
      <span className="font-display text-[10px] uppercase tracking-widest text-text-tertiary">{title}</span>
      <div className="h-px flex-1 bg-border-muted" />
    </div>
  )
}
