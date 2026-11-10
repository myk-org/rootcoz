/**
 * The one URL contract for job filters (#546).
 *
 * Both the dashboard and the reports page read and write their filter keys
 * through this module, so a new dimension is added once. The canonical key list
 * lives **here** and `lib/filterPresets.ts` imports it — never the other way
 * round, because a mutual import throws
 * `Cannot access 'JOB_FILTER_KEYS' before initialization` when this module is the
 * entry point.
 */

export type FilterValues = Record<string, string | string[]>

/** A page that lists or aggregates jobs. */
export type FilterPage = 'dashboard' | 'reports'

/** The four match modes of the test-name filter; the text stays literal. */
export const TEST_NAME_MODES = [
  { value: 'contains', label: 'contains' },
  { value: 'exact', label: 'exact' },
  { value: 'starts_with', label: 'starts with' },
  { value: 'ends_with', label: 'ends with' },
] as const

export type TestNameMode = (typeof TEST_NAME_MODES)[number]['value']

export const DEFAULT_TEST_NAME_MODE: TestNameMode = 'contains'

/** The eleven canonical dimensions, in bar order. Both pages use this list. */
export const JOB_FILTER_KEYS = [
  'team',
  'tier',
  'version',
  'label',
  'exclude_label',
  'status',
  'review_status',
  'analysis_state',
  'submitted_by',
  'date_from',
  'date_to',
] as const

/** Dashboard-only: filtering *jobs* by the name of a failing test (#546 B). */
export const TEST_NAME_FILTER_KEYS = ['test_name', 'test_name_mode'] as const

/** Read-only legacy date keys: accepted, never written. */
export const JOB_FILTER_READ_ALIASES = ['from', 'to'] as const

export interface JobFilterValues {
  team: string[]
  tier: string[]
  version: string[]
  label: string[]
  exclude_label: string[]
  status: string[]
  review_status: string
  analysis_state: string
  submitted_by: string[]
  date_from: string
  date_to: string
  test_name: string
  test_name_mode: TestNameMode
}

export const EMPTY_JOB_FILTERS: JobFilterValues = {
  team: [],
  tier: [],
  version: [],
  label: [],
  exclude_label: [],
  status: [],
  review_status: 'all',
  analysis_state: 'all',
  submitted_by: [],
  date_from: '',
  date_to: '',
  test_name: '',
  test_name_mode: DEFAULT_TEST_NAME_MODE,
}

// ─── Generic param <-> filters helpers (moved from filterPresets.ts) ─

/** Extract allowlisted filter values from URL search params. */
export function searchParamsToFilters(
  params: URLSearchParams,
  keys: readonly string[],
): FilterValues {
  const out: FilterValues = {}
  for (const key of keys) {
    const all = params.getAll(key).filter((v) => v !== '')
    if (all.length === 0) continue
    out[key] = all.length > 1 ? all : all[0]
  }
  return out
}

/** Replace allowlisted filter params in a URLSearchParams copy, leaving
 * everything else (e.g. `search`) untouched. */
export function applyFiltersToParams(
  params: URLSearchParams,
  filters: FilterValues,
  keys: readonly string[],
): URLSearchParams {
  const next = new URLSearchParams(params)
  for (const key of keys) next.delete(key)
  for (const [key, value] of Object.entries(filters)) {
    if (!keys.includes(key)) continue
    for (const v of Array.isArray(value) ? value : [value]) next.append(key, v)
  }
  return next
}

/** Build a query string from filter values (sidebar navigation). */
export function filtersToQueryString(filters: FilterValues): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    for (const v of Array.isArray(value) ? value : [value]) params.append(key, v)
  }
  return params.toString()
}

const asSortedArray = (value: string | string[]): string[] =>
  (Array.isArray(value) ? [...value] : [value]).sort()

/** Deep-compare two filter value maps (order-insensitive for lists). */
export function filtersEqual(a: FilterValues, b: FilterValues): boolean {
  const ka = Object.keys(a).sort()
  const kb = Object.keys(b).sort()
  if (ka.length !== kb.length || ka.some((k, i) => k !== kb[i])) return false
  return ka.every((k) => {
    const va = asSortedArray(a[k])
    const vb = asSortedArray(b[k])
    return va.length === vb.length && va.every((v, i) => v === vb[i])
  })
}

// ─── URL <-> typed values <-> preset filters ────────────────────────

// Caps the filtered endpoint enforces on submitted_by (src/rootcoz/main.py).
const MAX_SUBMITTERS = 50
const MAX_SUBMITTER_LENGTH = 64

/**
 * The submitter selection held in the URL.
 *
 * Names may contain commas, so the values stay a list instead of being joined
 * and re-split. Each value is folded onto the enumerated spelling when one
 * exists — the server matches case-insensitively, so `ALICE` and `alice` are one
 * selection, not two chips that toggle against each other. Values the server
 * would reject (blank, too long, past the cap) are dropped here so a shared link
 * cannot turn a page into a 400 error.
 */
export function normalizeSubmitters(values: string[], options: string[]): string[] {
  const spelling = new Map(options.map((name) => [name.toLowerCase(), name]))
  const seen = new Set<string>()
  const picked: string[] = []
  for (const raw of values) {
    const value = spelling.get(raw.trim().toLowerCase()) ?? raw.trim()
    const key = value.toLowerCase()
    // Code points, not UTF-16 units: the server counts characters, so an emoji
    // name must not be dropped for a length the endpoint would have accepted.
    const length = [...value].length
    if (!value || length > MAX_SUBMITTER_LENGTH || seen.has(key)) continue
    if (picked.length === MAX_SUBMITTERS) break
    seen.add(key)
    picked.push(value)
  }
  return picked
}

const asList = (value: string | string[] | undefined): string[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value]

const asText = (value: string | string[] | undefined): string =>
  value === undefined ? '' : Array.isArray(value) ? (value[0] ?? '') : value

function asMode(value: string | string[] | undefined): TestNameMode {
  const text = asText(value)
  return (TEST_NAME_MODES.some((mode) => mode.value === text)
    ? text
    : DEFAULT_TEST_NAME_MODE) as TestNameMode
}

/**
 * Read a page's filters from the URL.
 *
 * `keys` is the page's saved key set (dashboard adds the test-name pair); the
 * legacy `from`/`to` aliases are always read in addition, so a bookmark made
 * before the unified contract still opens the same view.
 */
export function jobFiltersFromParams(
  params: URLSearchParams,
  keys: readonly string[],
  options: { submitters?: string[] } = {},
): JobFilterValues {
  const filters = searchParamsToFilters(params, [...keys, ...JOB_FILTER_READ_ALIASES])
  return jobFiltersFromFilters(filters, options)
}

/** Normalise preset filters or URL filters into typed values. */
export function jobFiltersFromFilters(
  filters: FilterValues,
  options: { submitters?: string[] } = {},
): JobFilterValues {
  return {
    team: asList(filters.team),
    tier: asList(filters.tier),
    version: asList(filters.version),
    label: asList(filters.label),
    exclude_label: asList(filters.exclude_label),
    status: asList(filters.status),
    review_status: asText(filters.review_status) || 'all',
    analysis_state: asText(filters.analysis_state) || 'all',
    submitted_by: normalizeSubmitters(asList(filters.submitted_by), options.submitters ?? []),
    date_from: asText(filters.date_from) || asText(filters.from),
    date_to: asText(filters.date_to) || asText(filters.to),
    test_name: asText(filters.test_name),
    // A mode without a name is not a filter: normalise it to the default so the
    // pair is written as one value (a clean URL never carries `test_name_mode`).
    test_name_mode: asText(filters.test_name) ? asMode(filters.test_name_mode) : DEFAULT_TEST_NAME_MODE,
  }
}

/**
 * Collapse typed values to the preset filter shape: only real filters survive
 * (empties, `[]` and `all` omitted), and the test-name pair is written together
 * — a mode without a committed name is dropped.
 */
export function jobFiltersToFilters(values: JobFilterValues): FilterValues {
  const out: FilterValues = {}
  for (const key of ['team', 'tier', 'version', 'label', 'exclude_label', 'status', 'submitted_by'] as const) {
    const list = values[key]
    if (list.length > 0) out[key] = list
  }
  if (values.review_status !== 'all') out.review_status = values.review_status
  if (values.analysis_state !== 'all') out.analysis_state = values.analysis_state
  if (values.date_from) out.date_from = values.date_from
  if (values.date_to) out.date_to = values.date_to
  if (values.test_name) {
    out.test_name = values.test_name
    if (values.test_name_mode !== DEFAULT_TEST_NAME_MODE) out.test_name_mode = values.test_name_mode
  }
  return out
}

/** Write typed values into the URL, dropping the legacy date aliases. */
export function applyJobFiltersToParams(
  params: URLSearchParams,
  values: JobFilterValues,
  keys: readonly string[],
): URLSearchParams {
  const next = applyFiltersToParams(params, jobFiltersToFilters(values), keys)
  for (const alias of JOB_FILTER_READ_ALIASES) next.delete(alias)
  return next
}

/**
 * The URL→API wire shape, per page per dimension.
 *
 * The two APIs disagree (FastAPI keeps the last of a repeated scalar, and a CSV
 * sent to a repeatable list yields one bogus value), so the shape is stated in
 * one table instead of at each call site.
 */
export function jobFiltersToApiQuery(page: FilterPage, values: JobFilterValues): URLSearchParams {
  const params = new URLSearchParams()
  const repeated = (key: string, list: string[]) => {
    for (const value of list) params.append(key, value)
  }
  const csv = (key: string, list: string[]) => {
    if (list.length > 0) params.set(key, list.join(','))
  }

  if (page === 'dashboard') {
    repeated('team', values.team)
    repeated('tier', values.tier)
    repeated('version', values.version)
    repeated('label', values.label)
    repeated('exclude_label', values.exclude_label)
    repeated('status', values.status)
    repeated('submitted_by', values.submitted_by)
  } else {
    csv('team', values.team)
    csv('tier', values.tier)
    csv('version', values.version)
    csv('tags', values.label)
    csv('exclude_tags', values.exclude_label)
    csv('status', values.status)
    repeated('submitted_by', values.submitted_by)
  }
  if (values.review_status !== 'all') params.set('review_status', values.review_status)
  if (values.analysis_state !== 'all') params.set('analysis_state', values.analysis_state)
  if (values.date_from) params.set('date_from', values.date_from)
  if (values.date_to) params.set('date_to', values.date_to)
  if (page === 'dashboard' && values.test_name) {
    params.set('test_name', values.test_name)
    if (values.test_name_mode !== DEFAULT_TEST_NAME_MODE) {
      params.set('test_name_mode', values.test_name_mode)
    }
  }
  return params
}
