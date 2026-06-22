export const CLASSIFICATIONS = [
  'CODE ISSUE',
  'PRODUCT BUG',
  'INFRASTRUCTURE',
] as const

export type Classification = (typeof CLASSIFICATIONS)[number]

export const OVERRIDE_CLASSIFICATIONS = ['CODE ISSUE', 'PRODUCT BUG', 'INFRASTRUCTURE'] as const

export type OverrideClassification = (typeof OVERRIDE_CLASSIFICATIONS)[number]

export const PATTERNS = [
  'NEW',
  'REGRESSION',
  'FLAKY',
  'INTERMITTENT',
  'KNOWN_BUG',
  'PERSISTENT',
] as const

export type Pattern = (typeof PATTERNS)[number]

/** All known badge labels (root cause + pattern). */
export const ALL_BADGE_LABELS = [
  ...CLASSIFICATIONS,
  ...PATTERNS,
] as const

export type BadgeLabel = (typeof ALL_BADGE_LABELS)[number]
