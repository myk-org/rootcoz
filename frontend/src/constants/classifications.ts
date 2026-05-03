export const CLASSIFICATIONS = [
  'CODE ISSUE',
  'PRODUCT BUG',
  'FLAKY',
  'REGRESSION',
  'INFRASTRUCTURE',
  'KNOWN_BUG',
  'INTERMITTENT',
] as const

export type Classification = (typeof CLASSIFICATIONS)[number]

export const OVERRIDE_CLASSIFICATIONS = ['CODE ISSUE', 'PRODUCT BUG', 'INFRASTRUCTURE'] as const

export type OverrideClassification = (typeof OVERRIDE_CLASSIFICATIONS)[number]
