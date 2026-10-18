import * as React from "react"
import { cn } from "@/lib/utils"

// Lightweight Tabs (no Radix dependency) — controlled via value/onValueChange.

interface TabsContextValue {
  value: string
  setValue: (value: string) => void
}

const TabsContext = React.createContext<TabsContextValue | null>(null)

function useTabs() {
  const ctx = React.useContext(TabsContext)
  if (!ctx) throw new Error("Tabs components must be used within <Tabs>")
  return ctx
}

interface TabsProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string
  onValueChange: (value: string) => void
}

const Tabs = React.forwardRef<HTMLDivElement, TabsProps>(
  ({ className, value, onValueChange, children, ...props }, ref) => (
    <TabsContext.Provider value={{ value, setValue: onValueChange }}>
      <div ref={ref} className={className} {...props}>
        {children}
      </div>
    </TabsContext.Provider>
  ),
)
Tabs.displayName = "Tabs"

const TabsList = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      role="tablist"
      className={cn(
        "inline-flex flex-wrap items-center gap-1 rounded-lg border border-border-default bg-surface-card p-1",
        className,
      )}
      {...props}
    />
  ),
)
TabsList.displayName = "TabsList"

interface TabsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string
}

const TabsTrigger = React.forwardRef<HTMLButtonElement, TabsTriggerProps>(
  ({ className, value, onClick, ...props }, ref) => {
    const { value: active, setValue } = useTabs()
    const isActive = active === value
    return (
      <button
        ref={ref}
        type="button"
        role="tab"
        aria-selected={isActive}
        data-state={isActive ? "active" : "inactive"}
        onClick={(e) => {
          onClick?.(e)
          if (!e.defaultPrevented) setValue(value)
        }}
        className={cn(
          "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
          isActive
            ? "bg-surface-elevated text-text-primary shadow-sm"
            : "text-text-tertiary hover:bg-surface-hover hover:text-text-secondary",
          className,
        )}
        {...props}
      />
    )
  },
)
TabsTrigger.displayName = "TabsTrigger"

interface TabsContentProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string
}

const TabsContent = React.forwardRef<HTMLDivElement, TabsContentProps>(
  ({ className, value, children, ...props }, ref) => {
    const { value: active } = useTabs()
    if (active !== value) return null
    return (
      <div
        ref={ref}
        role="tabpanel"
        data-state={active === value ? "active" : "inactive"}
        className={cn("mt-4 focus-visible:outline-none", className)}
        {...props}
      >
        {children}
      </div>
    )
  },
)
TabsContent.displayName = "TabsContent"

export { Tabs, TabsList, TabsTrigger, TabsContent }
