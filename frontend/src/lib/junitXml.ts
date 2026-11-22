export const MAX_FILE_JOB_NAME_LENGTH = 500
// Above this size, avoid building a full multi-MB DOM on the browser main thread;
// scan opening <testsuite>/<testsuites> start tags in the leading prefix instead.
export const FAST_XML_PREFIX_BYTES = 65_536

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

function parseStartTagName(tagName: string, rawAttrs: string): string | null {
  const attrs = rawAttrs.replace(/\/\s*$/, '').trim()
  if (!attrs) return null
  try {
    const doc = new DOMParser().parseFromString(`<${tagName} ${attrs}/>`, 'text/xml')
    const root = doc.documentElement
    if (
      !root ||
      root.localName === 'parsererror' ||
      doc.getElementsByTagName('parsererror').length > 0
    ) {
      return null
    }
    return validSuiteName(root)
  } catch {
    return null
  }
}

function isTagNameChar(code: number): boolean {
  return (
    (code >= 0x41 && code <= 0x5a) || // A-Z
    (code >= 0x61 && code <= 0x7a) || // a-z
    (code >= 0x30 && code <= 0x39) || // 0-9
    code === 0x5f || // _
    code === 0x2d || // -
    code === 0x2e || // .
    code === 0x3a // :
  )
}

function findTagClose(text: string, start: number): number {
  let quote = 0
  for (let i = start; i < text.length; i++) {
    const code = text.charCodeAt(i)
    if (quote !== 0) {
      if (code === quote) quote = 0
    } else if (code === 0x22 || code === 0x27) {
      quote = code
    } else if (code === 0x3e) {
      return i
    }
  }
  return -1
}

function extractSuiteNameFromLargeXmlPrefix(rawXml: string): string | null {
  const head = rawXml.slice(0, FAST_XML_PREFIX_BYTES)
  const len = head.length
  let i = 0
  let isRoot = true
  let fallbackName: string | null = null

  while (i < len) {
    const lt = head.indexOf('<', i)
    if (lt === -1 || lt + 1 >= len) break

    const next = head.charCodeAt(lt + 1)
    // Comment: <!-- ... -->
    if (head.startsWith('<!--', lt)) {
      const end = head.indexOf('-->', lt + 4)
      if (end === -1) break
      i = end + 3
      continue
    }
    // CDATA: <![CDATA[ ... ]]>
    if (head.startsWith('<![CDATA[', lt)) {
      const end = head.indexOf(']]>', lt + 9)
      if (end === -1) break
      i = end + 3
      continue
    }
    // Processing instruction: <? ... ?>
    if (next === 0x3f) {
      const end = head.indexOf('?>', lt + 2)
      if (end === -1) break
      i = end + 2
      continue
    }
    // Declaration (<!DOCTYPE ...>, etc.) or closing tag (</...>)
    if (next === 0x21 || next === 0x2f) {
      const end = findTagClose(head, lt + 2)
      if (end === -1) break
      i = end + 1
      continue
    }

    // Opening tag: <tagName ...>
    let nameEnd = lt + 1
    while (nameEnd < len && isTagNameChar(head.charCodeAt(nameEnd))) {
      nameEnd++
    }
    if (nameEnd === lt + 1) {
      i = lt + 1
      continue
    }

    const tagClose = findTagClose(head, nameEnd)
    if (tagClose === -1) break

    const fullTag = head.slice(lt + 1, nameEnd)
    const colon = fullTag.lastIndexOf(':')
    const localName = colon >= 0 ? fullTag.slice(colon + 1) : fullTag
    const rawAttrs = head.slice(nameEnd, tagClose)

    if (localName === 'testsuite') {
      const name = parseStartTagName('testsuite', rawAttrs)
      if (name) return name
      if (isRoot) return null
    } else if (isRoot && localName === 'testsuites') {
      fallbackName = parseStartTagName('testsuites', rawAttrs)
    }
    isRoot = false
    i = tagClose + 1
  }
  return fallbackName
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

  if (rawXml.length > FAST_XML_PREFIX_BYTES) {
    return extractSuiteNameFromLargeXmlPrefix(rawXml)
  }

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
