/**
 * Returns a Date set to the start of the given date string in UTC (00:00:00.000Z).
 */
export function utcStartOfDateInput(value: string): Date {
  return new Date(`${value}T00:00:00.000Z`)
}

/**
 * Returns a Date set to the end of the given date string in UTC (23:59:59.999Z).
 */
export function utcEndOfDateInput(value: string): Date {
  return new Date(`${value}T23:59:59.999Z`)
}
