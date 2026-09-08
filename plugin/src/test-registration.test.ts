/**
 * A test file this package does not list is a test file CI never runs.
 *
 * `npm test` enumerates its inputs explicitly — `node --test dist/a.test.js
 * dist/b.test.js …` — and the CI job is just `cd plugin && npm test`. So an
 * unlisted `src/*.test.ts` compiles, ships, and is silently never executed.
 * Nothing failed when that happened, because the suite that would have
 * complained is the one that did not run.
 *
 * The list is kept explicit rather than replaced with a `dist/*.test.js`
 * glob on purpose: there is no clean step before `tsc`, so a glob would also
 * run stale artifacts left behind by deleted or renamed sources. This guard
 * gets the completeness a glob would give without inheriting that problem.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// Compiled to dist/, so the package root is one level up either way.
const PLUGIN_ROOT = fileURLToPath(new URL("..", import.meta.url));

const testScript: string = (
  JSON.parse(readFileSync(join(PLUGIN_ROOT, "package.json"), "utf8")) as {
    scripts: { test: string };
  }
).scripts.test;

/**
 * The dist entries the script names, as exact tokens.
 *
 * Tokenised rather than substring-matched because this package contains both
 * `identity.test.ts` and `tool-identity.test.ts`: asking whether the script
 * "includes" `dist/identity.test.js` answers yes when only the tool- one is
 * listed, and the guard would pass while the file went unrun.
 */
const listed = new Set(
  testScript.split(/\s+/).filter((tok) => tok.startsWith("dist/") && tok.endsWith(".test.js")),
);

const sources = readdirSync(join(PLUGIN_ROOT, "src")).filter((f) => f.endsWith(".test.ts"));

describe("every test file is registered with the runner", () => {
  test("no src/*.test.ts is missing from the npm test script", () => {
    const unlisted = sources
      .map((f) => `dist/${f.replace(/\.ts$/, ".js")}`)
      .filter((entry) => !listed.has(entry));
    assert.deepEqual(
      unlisted,
      [],
      `${unlisted.length} test file(s) exist but are never run by CI. Add them ` +
        `to the "test" script in plugin/package.json, or rename the file to ` +
        `*.fixture.ts if it declares no tests.`,
    );
  });

  test("the script names no entry whose source is gone", () => {
    // The other direction. A rename or deletion leaves a stale entry that
    // `node --test` fails on outright — but only once dist is clean, which it
    // never is here, so it can sit unnoticed until someone else's CI breaks.
    const expected = new Set(sources.map((f) => `dist/${f.replace(/\.ts$/, ".js")}`));
    const orphaned = [...listed].filter((entry) => !expected.has(entry));
    assert.deepEqual(orphaned, [], "stale entries in the test script");
  });

  test("the guard is reading a real list, not an empty one", () => {
    // Guards the guard: if the script were reformatted so no token matched,
    // both assertions above would go quiet in the passing direction.
    assert.ok(listed.size >= 20, `only ${listed.size} entries parsed from the test script`);
    assert.ok(sources.length >= 20, `only ${sources.length} test sources found`);
  });
});
