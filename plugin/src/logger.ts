/**
 * Shared error-formatting helpers.
 * Returns the message string for use in responses / result objects.
 */

/** Extract a human-readable message from an unknown throw value (no logging). */
export function formatError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** Log at warn level (default for non-critical failures). */
export function logError(context: string, err: unknown): string {
  const msg = formatError(err);
  console.warn(`[memclaw] ${context}:`, msg);
  return msg;
}

/** Log at error level (smoke-test failures, education failures, etc.). */
export function logErrorCritical(context: string, err: unknown): string {
  const msg = formatError(err);
  console.error(`[memclaw] ${context}:`, msg);
  return msg;
}
