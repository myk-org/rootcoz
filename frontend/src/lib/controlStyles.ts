/**
 * The one source of truth for single-line control styling.
 *
 * Every text input, select trigger, search field and page-local filter control
 * renders through these classes; `components/ui/*` primitives consume them, and
 * pages consume the primitives (never raw class strings). The guard test
 * `__tests__/controlStyles.test.ts` fails the build when a component hand-rolls
 * a control shape — that is what keeps "control style comes from one place"
 * true for future code instead of being a rule someone has to remember.
 *
 * Controls are square to match buttons; badges and status dots stay round, and
 * are not controls.
 */

/** Shared height for every single-line control and the buttons beside it. */
export const CONTROL_HEIGHT = 'h-9'

/** Square corners, matching Button. */
export const CONTROL_RADIUS = 'rounded-md'

/** Surface, border and hover treatment shared by every control. */
export const CONTROL_SURFACE =
  'border border-border-default bg-surface-elevated hover:bg-surface-hover'

/** The full single-line control class string. */
export const CONTROL_CLASS = `${CONTROL_HEIGHT} ${CONTROL_RADIUS} ${CONTROL_SURFACE}`

/**
 * Header/text button: an icon + label with no box of its own, used by Guide,
 * Feedback and the theme toggle. It sits beside a bordered control, so it must
 * NOT borrow CONTROL_CLASS — a lone bordered pill among plain text buttons is
 * the inconsistency this constant prevents.
 */
export const TEXT_BUTTON_CLASS =
  'flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium text-text-tertiary transition-colors duration-150 hover:bg-surface-hover hover:text-text-secondary'
