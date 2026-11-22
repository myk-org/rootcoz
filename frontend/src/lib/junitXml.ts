export const MAX_FILE_JOB_NAME_LENGTH = 500

// Mirrors backend FILE_JOB_NAME_PATTERN in src/rootcoz/models.py.
export const FILE_JOB_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._:()+-]*$/

function hasControlChars(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i)
    if (code < 0x20 || code === 0x7f) return true
  }
  return false
}

/**
 * Validate a file-analysis job name against the backend contract.
 * Returns a user-facing error message when invalid, or null when valid
 * (including blank/whitespace-only, since job_name is optional and omitted when blank).
 */
export function validateFileJobName(value: string): string | null {
  const name = value.trim()
  if (!name) return null
  if (name.length > MAX_FILE_JOB_NAME_LENGTH) {
    return `Job name must be at most ${MAX_FILE_JOB_NAME_LENGTH} characters.`
  }
  if (hasControlChars(name)) {
    return 'Job name must not contain control characters.'
  }
  if (name.includes('/') || name.includes('\\')) {
    return "Job name must not contain path separators ('/' or '\\')."
  }
  if (name.includes('..')) {
    return "Job name must not contain '..'."
  }
  if (name.startsWith('.')) {
    return "Job name must not start with '.'."
  }
  if (!FILE_JOB_NAME_RE.test(name)) {
    return 'Job name must start with a letter or digit and use only letters, digits, spaces, and . _ - : ( ) +'
  }
  return null
}

function validSuiteName(el: Element): string | null {
  const raw = (el.getAttribute('name') ?? '').trim()
  if (!raw) return null
  return validateFileJobName(raw) === null ? raw : null
}

/**
 * Extract and validate a JUnit suite name from raw XML in the browser.
 *
 * Matches backend extract_suite_name_from_xml precedence:
 *   1. Root <testsuite name="...">
 *   2. First valid <testsuite name="..."> inside <testsuites> (or wrapper)
 *   3. Root <testsuites name="..."> when no <testsuite> carries a valid name
 *
 * Returns null on malformed XML, missing names, or invalid/hostile suite names.
 */
export function extractSuiteNameFromXml(rawXml: string): string | null {
  if (!rawXml || !rawXml.trim()) return null

  let doc: Document
  try {
    doc = new DOMParser().parseFromString(rawXml, 'text/xml')
  } catch {
    return null
  }

  const root = doc.documentElement
  if (
    !root ||
    root.localName === 'parsererror' ||
    doc.getElementsByTagName('parsererror').length > 0
  ) {
    return null
  }

  if (root.localName === 'testsuite') {
    return validSuiteName(root)
  }

  const suites = doc.getElementsByTagNameNS('*', 'testsuite')
  for (let i = 0; i < suites.length; i++) {
    const name = validSuiteName(suites[i])
    if (name) return name
  }

  if (root.localName === 'testsuites') {
    return validSuiteName(root)
  }

  return null
}
