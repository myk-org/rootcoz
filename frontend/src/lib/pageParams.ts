/**
 * A page size read from the URL: one of the sizes the control offers, else the
 * default. `?limit=40` on a 25/50/100 control is not a size the admin can get
 * back to, so it must not be honoured.
 */
export function parsePageSize<T extends number>(
  raw: string | null,
  options: readonly T[],
  fallback: T,
): T {
  const value = Number(raw)
  return options.includes(value as T) ? (value as T) : fallback
}
