/**
 * Frozen identifiers used to exercise compatibility with existing installs.
 *
 * Declares no tests. It was named `*.test.ts` and so looked like a suite
 * that CI had simply forgotten to register — `*.fixture.ts` says what it is
 * and keeps `test-registration.test.ts` from demanding it be run.
 */
export const FROZEN_PLUGIN_ID = "memclaw"; // legacy-name-ok: permanent on-disk plugin identifier
export const LEGACY_DISPLAY_NAME = "MemClaw"; // legacy-name-ok: historical on-disk prose fixture
