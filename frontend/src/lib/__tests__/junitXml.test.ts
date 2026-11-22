import { describe, expect, it } from 'vitest'
import {
  extractSuiteNameFromXml,
  FAST_XML_PREFIX_BYTES,
  MAX_FILE_JOB_NAME_LENGTH,
  validateFileJobName,
} from '@/lib/junitXml'

describe('validateFileJobName', () => {
  it('allows empty and whitespace-only values (omitted on submit)', () => {
    expect(validateFileJobName('')).toBeNull()
    expect(validateFileJobName('   \t  ')).toBeNull()
  })

  it('accepts valid names with allowed characters up to max length', () => {
    expect(validateFileJobName('qe-ui-check-submit')).toBeNull()
    expect(validateFileJobName('Suite 1: tier_2 (v4.18) + extra.run')).toBeNull()
    expect(validateFileJobName('a'.repeat(MAX_FILE_JOB_NAME_LENGTH))).toBeNull()
  })

  it('rejects names exceeding max length', () => {
    expect(validateFileJobName('a'.repeat(MAX_FILE_JOB_NAME_LENGTH + 1))).toMatch(/at most 500/)
  })

  it('rejects path separators, traversal, leading dots, control chars, and disallowed chars', () => {
    expect(validateFileJobName('../etc/passwd')).toMatch(/path separators/)
    expect(validateFileJobName('a/b')).toMatch(/path separators/)
    expect(validateFileJobName('a\\b')).toMatch(/path separators/)
    expect(validateFileJobName('foo..bar')).toMatch(/\.\./)
    expect(validateFileJobName('.hidden')).toMatch(/start with '\.'/)
    expect(validateFileJobName('-leading-dash')).toMatch(/must start with a letter or digit/)
    expect(validateFileJobName('suite<script>')).toMatch(/must start with a letter or digit/)
    expect(validateFileJobName('suite\u0007bell')).toMatch(/control characters/)
  })
})

describe('extractSuiteNameFromXml', () => {
  it('extracts root <testsuite name> and trims whitespace', () => {
    expect(extractSuiteNameFromXml('<testsuite name="qe-ui-check-submit"/>')).toBe(
      'qe-ui-check-submit',
    )
    expect(extractSuiteNameFromXml('<testsuite name="  spaced-suite  "></testsuite>')).toBe(
      'spaced-suite',
    )
  })

  it('extracts first valid <testsuite name> inside <testsuites> before falling back to <testsuites name>', () => {
    expect(
      extractSuiteNameFromXml('<testsuites><testsuite name="nested-suite"/></testsuites>'),
    ).toBe('nested-suite')
    expect(
      extractSuiteNameFromXml(
        '<testsuites name="wrapper-suite"><testsuite name="nested-suite"/></testsuites>',
      ),
    ).toBe('nested-suite')
    expect(
      extractSuiteNameFromXml(
        '<testsuites name="wrapper-suite"><testsuite name="../bad"/><testsuite name="second-good"/></testsuites>',
      ),
    ).toBe('second-good')
    expect(
      extractSuiteNameFromXml('<testsuites name="wrapper-suite"><testsuite/></testsuites>'),
    ).toBe('wrapper-suite')
    expect(
      extractSuiteNameFromXml(
        '<testsuites name="wrapper-suite"><testsuite name="a/b"/></testsuites>',
      ),
    ).toBe('wrapper-suite')
  })

  it('handles XML namespaces', () => {
    expect(
      extractSuiteNameFromXml(
        '<ns:testsuite xmlns:ns="http://example.com/junit" name="ns-suite"/>',
      ),
    ).toBe('ns-suite')
    expect(
      extractSuiteNameFromXml(
        '<testsuites xmlns="http://example.com/junit"><testsuite name="default-ns-suite"/></testsuites>',
      ),
    ).toBe('default-ns-suite')
  })

  it('returns null when root <testsuite> has no valid name', () => {
    expect(extractSuiteNameFromXml('<testsuite/>')).toBeNull()
    expect(extractSuiteNameFromXml('<testsuite name="   "/>')).toBeNull()
    expect(
      extractSuiteNameFromXml('<testsuite name="../bad"><testsuite name="inner"/></testsuite>'),
    ).toBeNull()
  })

  it('returns null for hostile suite names', () => {
    expect(extractSuiteNameFromXml('<testsuite name="../etc/passwd"/>')).toBeNull()
    expect(extractSuiteNameFromXml('<testsuite name="a/b"/>')).toBeNull()
    expect(extractSuiteNameFromXml('<testsuite name="a\\b"/>')).toBeNull()
    expect(extractSuiteNameFromXml('<testsuite name=".hidden"/>')).toBeNull()
    expect(extractSuiteNameFromXml('<testsuites name="../etc/passwd"><testsuite/></testsuites>')).toBeNull()
  })

  it('returns null cleanly on empty or malformed XML without throwing', () => {
    expect(extractSuiteNameFromXml('')).toBeNull()
    expect(extractSuiteNameFromXml('   ')).toBeNull()
    expect(extractSuiteNameFromXml('<testsuite name="unclosed"')).toBeNull()
    expect(extractSuiteNameFromXml('not xml at all')).toBeNull()
  })

  it('extracts suite names from large XML via bounded prefix scan without building a full DOM', () => {
    const padding = 'x'.repeat(FAST_XML_PREFIX_BYTES + 1024)
    expect(
      extractSuiteNameFromXml(
        `<?xml version="1.0"?><!-- comment --><testsuite note="a > b" name="large-root-suite"><testcase name="t1">${padding}</testcase></testsuite>`,
      ),
    ).toBe('large-root-suite')
    expect(
      extractSuiteNameFromXml(
        `<testsuites name="outer-large"><testsuite name="inner-large"><testcase>${padding}</testcase></testsuite></testsuites>`,
      ),
    ).toBe('inner-large')
    expect(
      extractSuiteNameFromXml(
        `<testsuites name="outer-fallback"><testsuite><testcase>${padding}</testcase></testsuite></testsuites>`,
      ),
    ).toBe('outer-fallback')
    expect(
      extractSuiteNameFromXml(
        `<testsuite name="../etc/passwd"><testcase>${padding}</testcase></testsuite>`,
      ),
    ).toBeNull()
  })

  it('handles large XML with repeated tag-like text inside CDATA in linear time', () => {
    const hostileCdata = '<a '.repeat(25_000)
    const xmlWithCdataBeforeSuite =
      `<testsuites name="outer-cdata-suite"><system-out><![CDATA[${hostileCdata}]]></system-out>` +
      `<testsuite name="late-suite"/></testsuites>`
    expect(xmlWithCdataBeforeSuite.length).toBeGreaterThan(FAST_XML_PREFIX_BYTES)
    const start = performance.now()
    expect(extractSuiteNameFromXml(xmlWithCdataBeforeSuite)).toBe('outer-cdata-suite')
    expect(performance.now() - start).toBeLessThan(200)

    const xmlWithClosedCdata =
      `<testsuites><system-out><![CDATA[<testsuite name="fake-inside-cdata"/>${'<a '.repeat(100)}]]></system-out>` +
      `<testsuite name="real-after-cdata"><testcase>${'x'.repeat(FAST_XML_PREFIX_BYTES)}</testcase></testsuite></testsuites>`
    expect(extractSuiteNameFromXml(xmlWithClosedCdata)).toBe('real-after-cdata')
  })
})
