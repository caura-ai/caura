/**
 * Runtime discovery bridge for ``openclaw/plugin-sdk`` exports.
 *
 * # Why this file exists
 *
 * Our plugin ships as compiled ``dist/index.js``. OpenClaw's plugin loader
 * (``loader-CBUR8YGF.js`` → ``createPluginModuleLoader``) takes one of two
 * paths per ``shouldPreferNativeModuleLoad``: jiti for ``.ts`` / ``.mts``
 * / ``.cts`` sources, native Node import for ``.js`` / ``.mjs`` / ``.cjs``.
 * The jiti path consults a ``PluginLoaderAliasMap`` that aliases the
 * bare-spec ``openclaw/plugin-sdk`` to OpenClaw's bundled SDK location.
 * Native-loaded ``.js`` plugins do NOT see that alias map — bare-spec
 * resolution falls back to standard Node resolution, which from
 * ``~/.openclaw/plugins/memclaw/dist/index.js`` walks the directory tree
 * upward and never reaches OpenClaw's global install. Result:
 *
 *     import("openclaw/plugin-sdk")       // ERR_MODULE_NOT_FOUND
 *     import("@openclaw/plugin-sdk")      // ERR_MODULE_NOT_FOUND
 *     createRequire(argv[1]).resolve("openclaw/plugin-sdk")  // MODULE_NOT_FOUND
 *
 * (Wet-tested 2026-05-26 on openclaw-test-ran with OpenClaw 2026.5.4.)
 *
 * # What this bridge does
 *
 * Locate the OpenClaw install at runtime by realpath-resolving
 * ``process.argv[1]`` (the launcher script, e.g. ``/usr/bin/openclaw``
 * which is a symlink to ``/usr/lib/.../openclaw.mjs``), walking up until
 * we find a ``package.json`` with ``name: "openclaw"``, then importing
 * ``{pkgRoot}/dist/plugin-sdk/index.js`` via absolute path. The absolute
 * path import bypasses the alias-map dependency entirely.
 *
 * Verified to work across global npm install. The same shape (launcher
 * → symlink → real script inside the package) is used by brew, nvm,
 * asdf, and source-checkout installs, so the discovery is portable.
 *
 * # What this bridge does NOT do
 *
 * - Does not throw. Every error becomes a ``null`` return so callers
 *   can fall back gracefully (degraded behavior is better than a
 *   crashed turn).
 * - Does not attempt the bare-spec import first. That path is known
 *   to fail in our deployment and would just add latency.
 * - Does not assume a TypeScript dep on the ``openclaw`` package.
 *   Returned values are typed structurally so we never need
 *   ``import type`` from the SDK at compile time.
 *
 * # Why we keep ``ownsCompaction: true``
 *
 * With this bridge in place, our ``compact()`` delegates to
 * ``delegateCompactionToRuntime`` from the SDK — which is exactly what
 * OpenClaw's legacy engine does internally. Declaring
 * ``ownsCompaction: true`` is therefore truthful: we own the contract
 * with OpenClaw, and we fulfill it by handing the actual work to the
 * shared runtime helper. Setting it to ``false`` would NOT auto-fall
 * back to legacy behavior — per OpenClaw docs
 * (``docs/concepts/context-engine.md:241``), ``false`` without a real
 * implementation means no compaction at all.
 */

import { realpath, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";

/**
 * Structural shape of the bits of ``openclaw/plugin-sdk`` we use.
 * Kept minimal so we never grow a build-time dependency on the SDK
 * package.
 */
export interface OpenClawSdkSurface {
  /**
   * Delegate a context-engine compaction request to OpenClaw's
   * built-in runtime compaction path. Identical to the helper used by
   * the legacy engine; returns a properly-shaped ``CompactResult``.
   */
  delegateCompactionToRuntime?: (params: unknown) => Promise<unknown>;
  /**
   * Build the memory-plugin prompt section as a single string suitable
   * for ``systemPromptAddition``. Currently unused by our engine
   * (assemble already composes its own section), but kept in the
   * surface for future callers.
   */
  buildMemorySystemPromptAddition?: (params: {
    availableTools: Set<string>;
    citationsMode?: string;
  }) => string | undefined;
}

/**
 * Discovery outcome. Three distinct states matter for diagnostics:
 *
 *   * ``{sdk: object, pkgRoot: string}`` — happy path. SDK exports
 *     at least one of the helpers we care about.
 *   * ``{sdk: null,   pkgRoot: string}`` — discovered openclaw on
 *     disk but its plugin-sdk did not export any of our target
 *     functions, OR the import failed. Common cause: openclaw older
 *     than 2026.5.x; possible cause: corrupted install. Operators
 *     can ``ls -la $pkgRoot`` to verify.
 *   * ``{sdk: null,   pkgRoot: null}``   — no openclaw discovered on
 *     the launcher's parent path. Most likely cause: install layout
 *     we don't recognize (custom symlink farm, vendored tree). The
 *     operator's next step is to check where openclaw lives.
 *
 * Callers (currently just ``MemClawContextEngine.compact()``) branch
 * on ``pkgRoot`` to emit a remediation-specific log message instead
 * of a generic "not discoverable" hint that sends operators hunting
 * for a missing install when the real cause is a present-but-broken
 * one.
 */
export interface OpenClawSdkResolution {
  sdk: OpenClawSdkSurface | null;
  pkgRoot: string | null;
}

// Single in-flight promise per process lifetime. Storing the PROMISE
// (rather than a pair of cachedSdk + triedAndFailed flags) means
// concurrent callers all await the same resolution — no
// stampede, no race window where caller B sees a half-set negative
// flag while caller A's resolve is still in flight and would have
// succeeded. The OpenClaw install location does not change during a
// process lifetime, so a single promise is correct for both the
// positive and negative outcomes.
let sdkPromise: Promise<OpenClawSdkResolution> | null = null;

/**
 * Reset the resolver cache. Test-only — exported with a leading
 * underscore so it never reads like a production API. The
 * ``NODE_ENV !== "test"`` guard enforces test-only semantics
 * explicitly: only when ``NODE_ENV`` is the literal string
 * ``"test"`` will this function actually clear the cache. Any
 * other value — ``"production"``, ``"development"``, ``"staging"``,
 * or unset — turns this into a no-op. ``npm test`` sets
 * ``NODE_ENV=test`` via ``package.json`` scripts, so the test
 * suite is unaffected; everything else gets defense-in-depth
 * against accidental cache invalidation.
 */
export function _resetSdkBridgeCache(): void {
  if (process.env.NODE_ENV !== "test") return;
  sdkPromise = null;
}

/**
 * Resolve the OpenClaw SDK surface at runtime.
 *
 * Always resolves to an ``OpenClawSdkResolution`` — never throws.
 * Callers branch on the combination of ``sdk`` and ``pkgRoot``:
 *
 *   * ``sdk`` truthy → use it.
 *   * ``sdk`` null and ``pkgRoot`` truthy → openclaw is installed at
 *     ``pkgRoot`` but its plugin-sdk did not expose any helper we
 *     can use (likely too old, possibly corrupt). Including
 *     ``pkgRoot`` in the operator-facing log message lets them
 *     verify the file on disk without guessing the install path.
 *   * Both null → openclaw was not discoverable from
 *     ``process.argv[1]``.
 *
 * Note: this function is NOT marked ``async`` — it returns the
 * cached promise directly so two concurrent callers receive the
 * same in-flight resolution. Making it ``async`` would wrap each
 * call in a NEW promise, defeating the cache.
 */
export function getOpenClawSdk(): Promise<OpenClawSdkResolution> {
  if (!sdkPromise) sdkPromise = _resolveSdk();
  return sdkPromise;
}

/**
 * Resolve the SDK module subpath from a parsed ``openclaw``
 * ``package.json``. Reads the standard Node ``exports`` field if
 * present so we follow whatever output layout the installed
 * openclaw version declares — robust to future moves of
 * ``dist/plugin-sdk/index.js``.
 *
 * Returns ``null`` (so the caller falls back to the conventional
 * layout) when ``exports`` is missing or doesn't declare
 * ``./plugin-sdk``.
 *
 * Conditional-exports resolution preference, matching Node's own
 * resolver as documented at
 * https://nodejs.org/api/packages.html#conditional-exports:
 * ``import`` → ``default`` → ``node``. ``default`` is the universal
 * fallback. We don't honor ``require`` because our load uses
 * ``import()`` which is ESM.
 */
function _resolveSdkSubpath(pkg: Record<string, unknown>): string | null {
  const exports = pkg.exports;
  if (!exports || typeof exports !== "object") return null;
  const entry = (exports as Record<string, unknown>)["./plugin-sdk"];
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object") {
    const conditional = entry as Record<string, unknown>;
    const candidate =
      conditional.import ?? conditional.default ?? conditional.node;
    if (typeof candidate === "string") return candidate;
  }
  return null;
}

async function _resolveSdk(): Promise<OpenClawSdkResolution> {
  // Stop walking after this many parent directories. Eight is plenty
  // for ``/usr/lib/node_modules/openclaw`` (5 levels above ``dist/``)
  // and short enough that a misconfigured environment fails quickly.
  const MAX_WALK_DEPTH = 8;

  // Phase 1 — find the openclaw package root by walking up from the
  // launcher script. Anything that goes wrong in here means we
  // never even located openclaw on disk, so both fields stay null.
  // We retain the PARSED ``package.json`` (not just the path) so
  // Phase 2 can consult the ``exports`` field for SDK resolution
  // without re-reading the file.
  let pkgRoot: string | null = null;
  let pkg: Record<string, unknown> | null = null;
  try {
    const launcher = process.argv?.[1];
    if (!launcher || typeof launcher !== "string") {
      return { sdk: null, pkgRoot: null };
    }

    let realLauncher: string;
    try {
      realLauncher = await realpath(launcher);
    } catch {
      return { sdk: null, pkgRoot: null };
    }

    let dir = dirname(realLauncher);
    for (let depth = 0; depth < MAX_WALK_DEPTH; depth++) {
      const pj = join(dir, "package.json");
      // ``readFile`` throws ENOENT for missing files and EACCES for
      // unreadable ones — both mean "no package.json here, walk up."
      // A pre-check via ``access`` would only add a TOCTOU window and
      // an extra await without changing the signal.
      let pkgText: string;
      try {
        pkgText = await readFile(pj, "utf8");
      } catch {
        const parent = dirname(dir);
        if (parent === dir) break;
        dir = parent;
        continue;
      }
      try {
        const parsed = JSON.parse(pkgText) as Record<string, unknown>;
        // Strict name match. ``openclaw`` is the canonical npm name
        // for the runtime; any other name (``@openclaw/sdk``, a
        // fork, etc.) is intentionally NOT accepted — the SDK
        // surface contract is tied to ``openclaw`` versioning.
        if (parsed.name === "openclaw") {
          pkgRoot = dir;
          pkg = parsed;
          break;
        }
      } catch {
        // Malformed package.json — keep walking.
      }
      const parent = dirname(dir);
      // Hit filesystem root or symlink loop — stop walking.
      if (parent === dir) break;
      dir = parent;
    }
  } catch {
    return { sdk: null, pkgRoot: null };
  }

  if (!pkgRoot || !pkg) return { sdk: null, pkgRoot: null };

  // Phase 2 — load the SDK module from the discovered package. Any
  // failure here KEEPS ``pkgRoot`` set in the return: we know
  // openclaw is on disk, just can't use it. Caller's diagnostic
  // distinguishes this from the "never found" case.
  //
  // Prefer the path openclaw declares via package.json ``exports``;
  // only fall back to the conventional ``dist/plugin-sdk/index.js``
  // when the package doesn't tell us where to look. This makes the
  // bridge robust to future openclaw releases that move the build
  // output (e.g., to a different top-level dir).
  const subpath = _resolveSdkSubpath(pkg) ?? "dist/plugin-sdk/index.js";
  const sdkPath = join(pkgRoot, subpath);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let mod: Record<string, any>;
  try {
    // No pre-check on sdkPath — ``import()`` already rejects with a
    // clear error if the file is missing or unreadable. A separate
    // ``access`` would add a TOCTOU window and another await for no
    // signal beyond what the import already provides.
    mod = (await import(sdkPath)) as Record<string, any>;
  } catch {
    return { sdk: null, pkgRoot };
  }

  // Structural extraction. Anything missing is fine — callers
  // guard on the specific field they need.
  //
  // Surface is returned unconditionally when Phase 2 loads cleanly,
  // even if the extracted object is empty. We deliberately do NOT
  // collapse "no recognized exports" to ``sdk: null`` here because
  // the SDK resolution is shared across all callers via a single
  // process-lifetime promise cache: gating on one caller's required
  // field would make any other field permanently unreachable for
  // the rest of the process. Each caller is responsible for
  // checking ``sdk?.<field>`` and falling back via the ``pkgRoot``
  // diagnostic when their specific field is absent.
  const surface: OpenClawSdkSurface = {};
  if (typeof mod.delegateCompactionToRuntime === "function") {
    surface.delegateCompactionToRuntime = mod.delegateCompactionToRuntime;
  }
  if (typeof mod.buildMemorySystemPromptAddition === "function") {
    surface.buildMemorySystemPromptAddition =
      mod.buildMemorySystemPromptAddition;
  }
  return { sdk: surface, pkgRoot };
}
