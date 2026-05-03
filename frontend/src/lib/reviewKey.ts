/** Build the review lookup key matching the backend format. */
export function reviewKey(testName: string, childJobName?: string, childBuildNumber?: number): string {
  if (childJobName) return `${childJobName}#${childBuildNumber ?? 0}::${testName}`
  return testName
}
