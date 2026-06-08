/**
 * MemClaw ContextEngine — lifecycle hooks for OpenClaw memory integration.
 *
 * Provides: bootstrap (smoke test), ingest (message buffering +
 * persistence), assemble (token-budget-aware recall injection),
 * afterTurn (auto-write), compact (persist summary + delegate to
 * OpenClaw runtime via ``openclaw-sdk-bridge``).
 *
 * Security:
 * - afterTurn enabled by default; opt out with MEMCLAW_AUTO_WRITE_TURNS=false
 * - Recall timeout enforced via AbortController
 */

import { createHash } from "crypto";
import { apiCall, parseSearchItems } from "./transport.js";
import {
  MEMCLAW_FLEET_ID,
  MEMCLAW_TENANT_ID,
  MEMCLAW_AUTO_WRITE_TURNS,
  ensureTenantId,
  RECALL_CACHE_TTL_MS,
  RECALL_TIMEOUT_MS,
  MIN_TURN_CONTENT_LENGTH,
  MAX_TURN_SUMMARY_LENGTH,
  MAX_RECALL_CONTENT_LENGTH,
  RECALL_POLICY,
  RECALL_MIN_PROMPT_CHARS,
  RECALL_TRIGGER_KEYWORDS,
  RECALL_DENY_SESSIONS,
  type RecallPolicy,
} from "./env.js";
import { memclawPromptSectionText } from "./prompt-section.js";
import { MEMCLAW_TOOLS } from "./tools.js";
import { resolveAgentId, resolveAgentIdQuiet } from "./resolve-agent.js";
import { getTenantPrefix, getSessionKey } from "./context-engine.internal.js";
import { logError, logErrorCritical } from "./logger.js";
import { fetchKeystonesBlock } from "./keystones.js";
import { getOpenClawSdk } from "./openclaw-sdk-bridge.js";

// --- Typed interfaces for ContextEngine hooks ---

export interface IngestMessage {
  role: "user" | "assistant" | "system";
  content: string | unknown;
  sessionKey?: string;
}

export interface AssembleBudget {
  tokenBudget?: number;
}

export interface CompactContext {
  summary?: string;
  compactionSummary?: string;
  [key: string]: unknown;
}

export interface AfterTurnContext {
  messages?: Array<{ role: string; content: string | unknown }>;
  [key: string]: unknown;
}

// --- Session message buffer (LRU, per-session) ---

const SESSION_BUFFER_CAP = 50;
const MAX_SESSIONS = 100;
const MAX_INGEST_WRITES_PER_SESSION = 10;
const sessionBuffers = new Map<string, IngestMessage[]>();
const sessionIngestCounts = new Map<string, number>();

// --- Process-level bootstrap memoization (CAURA-000) ---
//
// OpenClaw's ``resolveContextEngine`` calls our factory every time any
// of ~7 subsystems (embedded-agent, cli-compaction, init, openclaw-tools,
// prepare.runtime, subagent-registry, context-engine-host-compat) needs
// a ContextEngine — there's NO caching at the OpenClaw side
// (``registry-CMq-i5MO.js: engine = await entry.factory(factoryCtx)``).
// Customer log on goodclaw shows 390 ``ContextEngine bootstrap`` events
// in 31.4 hours (~12/hour) → 388 smoke tests passed, 0 actionable
// warnings. Each smoke = 1 write + 1-3 searches + 1 delete = 3-5 backend
// ops + 2 embedding computations + ~500-1500ms wall-clock.
//
// The bootstrap state was previously instance-level (``private
// _bootstrapped`` + ``_bootstrapPromise``), so each freshly-constructed
// engine ran its own smoke test. Hoisting to module-level memoizes
// across instances within the same Node process: smoke runs ONCE per
// process lifetime, all subsequent engines share the resolved promise
// and short-circuit instantly.
//
// On failure, the catch resets the promise to ``null`` so the next
// engine retries — the "first-deploy validation" property is preserved.
let _processBootstrapPromise: Promise<void> | null = null;

/**
 * Test-only: reset the process-level bootstrap memo so each test
 * exercises the smoke path fresh. NOT for production use.
 */
export function _resetBootstrapForTests(): void {
  _processBootstrapPromise = null;
}

/**
 * True iff the error is the content-hash dedup rejection from
 * ``POST /memories`` (the backend's idempotency guard against re-writing
 * identical content under the same ``(tenant_id, fleet_id, agent_id)``
 * triple — see ``CheckExactDuplicate`` step in the write pipeline).
 *
 * Used by ``afterTurn`` to swallow this specific shape silently: the same
 * agent emitting an identical short reply on consecutive turns ("Got it.",
 * "ok", "thanks") is the common case, and the existing memory is what
 * ``afterTurn`` would have written — so a 409 here is a soft no-op, not a
 * failure. Pre-fix it produced ~5 warn/hr in the customer's goodclaw
 * gateway, drowning real signal in the operator log. Anything that's NOT a
 * 409 (5xx, network, auth) still routes to the normal error logger.
 *
 * Matches the error message format from ``transport.ts:82``:
 *   ``new Error("MemClaw API " + status + ": " + body)``
 */
export function isDuplicateMemoryError(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  return /\bMemClaw API 409\b/.test(e.message);
}

function pushToBuffer(sessionKey: string, message: IngestMessage): void {
  let buffer = sessionBuffers.get(sessionKey);
  if (!buffer) {
    // LRU eviction: if we have too many sessions, drop the oldest
    if (sessionBuffers.size >= MAX_SESSIONS) {
      const oldest = sessionBuffers.keys().next().value!;
      sessionBuffers.delete(oldest);
      sessionIngestCounts.delete(oldest);
    }
    buffer = [];
    sessionBuffers.set(sessionKey, buffer);
  }
  buffer.push(message);
  // Cap per-session buffer
  if (buffer.length > SESSION_BUFFER_CAP) {
    buffer.splice(0, buffer.length - SESSION_BUFFER_CAP);
  }
}

// --- Build search query from recent user messages ---

function buildQueryFromMessages(
  sessionKey: string,
  fallbackPrompt?: string,
): string {
  const buffer = sessionBuffers.get(sessionKey);
  if (buffer && buffer.length > 0) {
    // Use last 3 user messages to build a contextual query
    const userMessages = buffer
      .filter((m) => m.role === "user")
      .slice(-3);
    if (userMessages.length > 0) {
      const combined = userMessages
        .map((m) =>
          typeof m.content === "string" ? m.content : JSON.stringify(m.content),
        )
        .join(" ");
      // Truncate to a reasonable query length
      return combined.length > 500 ? combined.slice(-500) : combined;
    }
  }
  return fallbackPrompt && fallbackPrompt.length > 5 ? fallbackPrompt : "";
}

// --- Recall-policy predicate ---
//
// Decides whether `assemble()` should issue a `/search` call this turn or
// emit only the static education + identity blocks. Inlined here (rather
// than a dedicated module) so the v2.3.0→v2.4.0 first-hop deploy doesn't
// have to know about a new file in its hardcoded srcFiles list.
//
// The OpenClaw runtime calls assemble() on every prompt assembly with no
// triviality signal of its own (verified against
// github.com/openclaw/openclaw/src/context-engine/types.ts:258-272). All
// gating must happen here.

export type ShouldRecallReason =
  | "policy-always"
  | "policy-never"
  | "policy-keywords-no-trigger"
  | "explicit-recall-trigger"
  | "below-threshold"
  | "trivial-ping"
  | "slash-command"
  | "session-denied"
  | "default-substantive";

export interface ShouldRecallInput {
  policy: RecallPolicy;
  prompt: string | undefined;
  messages: Array<{ role: string; content: unknown }>;
  minPromptChars: number;
  triggerKeywords: readonly string[];
  sessionKey?: string;
  denySessions: readonly string[];
}

export interface ShouldRecallResult {
  recall: boolean;
  reason: ShouldRecallReason;
}

// Greetings / acks / single-emoji turns. Anchored on the FULL effective
// prompt (after trim + lowercase). The list is conservative; if none of
// these patterns matches we let recall through. Keep the source-of-truth
// here and exercise it via context-engine.test.ts.
const TRIVIAL_PING_LITERALS: ReadonlySet<string> = new Set([
  // greetings
  "hi", "hello", "hey", "yo", "yo!", "hi there", "hey there", "hello there",
  // acks
  "ok", "okay", "k", "kk", "yes", "yep", "yeah", "no", "nope", "nah",
  "thanks", "thank you", "thx", "ty", "cheers", "got it", "noted",
  "cool", "nice", "great", "sure", "alright", "right", "ack",
  // emoji-only / sticker-style
  "👍", "🙏", "💯", "🦞", "👋", "😀", "😄", "🙂", "✅", "❤️", "🔥",
]);

function _effectivePrompt(
  prompt: string | undefined,
  messages: ShouldRecallInput["messages"],
): string {
  const direct = (prompt || "").trim();
  if (direct) return direct;
  // Fall back to the most recent user message in the buffer.
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === "user" && typeof m.content === "string") {
      const t = m.content.trim();
      if (t) return t;
    }
  }
  return "";
}

function _hasTriggerKeyword(
  text: string,
  keywords: readonly string[],
): boolean {
  if (!text) return false;
  const lc = text.toLowerCase();
  return keywords.some((kw) => {
    const k = kw.toLowerCase();
    if (!k) return false;
    // Substring with **lenient one-sided** word boundary. The keyword
    // matches if AT LEAST ONE side is a non-letter (or end-of-string).
    // The match fails only when the keyword is embedded INSIDE another
    // word (letters on both sides).
    //
    // Examples:
    //   "remember the deadline"   ✓ (both sides whitespace)
    //   "remembered yesterday"    ✓ (after='e' is letter, before=' ' is not — one-sided OK)
    //   "preremember"             ✓ (before='r' is letter, after=end — one-sided OK)
    //   "preremembered"           ✗ (both sides letters — embedded)
    //   "memorylane"              ✓ (one-sided — known minor false-positive)
    //
    // This is INTENTIONAL: for a recall-gate heuristic, catching
    // morphological variants ("remembered", "remembering", "recalling")
    // matters more than excluding rare embedded-substring false
    // positives. The cost of a false positive is one extra /search;
    // the cost of a false negative is a missed-context turn.
    // See context-engine.test.ts "_hasTriggerKeyword boundary"
    // for the pinned cases. Do NOT switch to strict word-boundary
    // (`return !(isLetterBefore || isLetterAfter)`) without updating
    // those tests and reconsidering the trade-off.
    //
    // Walk ALL occurrences, not just the first. Without the loop, a
    // prompt like `"preremembering: remember the deadline"` returns
    // false: the first "remember" hit (inside "preremembering") is
    // embedded both-sides → rejected → and we'd never check the
    // second clean occurrence at the start of "remember the deadline".
    let idx = lc.indexOf(k);
    while (idx >= 0) {
      const before = idx === 0 ? "" : lc[idx - 1];
      const after = idx + k.length >= lc.length ? "" : lc[idx + k.length];
      const isLetterBefore = /[a-z0-9]/.test(before);
      const isLetterAfter = /[a-z0-9]/.test(after);
      if (!(isLetterBefore && isLetterAfter)) return true;
      idx = lc.indexOf(k, idx + 1);
    }
    return false;
  });
}

function _isTrivialPing(text: string): boolean {
  // Empty input is "no content", not a pleasantry — let the threshold
  // check handle it so the reason is `below-threshold` rather than
  // `trivial-ping`.
  if (!text) return false;
  const lc = text.trim().toLowerCase();
  if (!lc) return false;
  if (TRIVIAL_PING_LITERALS.has(lc)) return true;
  // Pure-emoji / pure-symbol / pure-punctuation turns.
  if (/^[\p{Emoji_Presentation}\p{Extended_Pictographic}\s\p{P}]+$/u.test(text))
    return true;
  return false;
}

export function shouldRecall(input: ShouldRecallInput): ShouldRecallResult {
  // Per-session denylist applies regardless of policy.
  if (
    input.sessionKey &&
    input.denySessions.some((s) => s && input.sessionKey!.includes(s))
  ) {
    return { recall: false, reason: "session-denied" };
  }

  switch (input.policy) {
    case "always":
      return { recall: true, reason: "policy-always" };
    case "never":
      return { recall: false, reason: "policy-never" };
    case "keywords": {
      const eff = _effectivePrompt(input.prompt, input.messages);
      return _hasTriggerKeyword(eff, input.triggerKeywords)
        ? { recall: true, reason: "explicit-recall-trigger" }
        : { recall: false, reason: "policy-keywords-no-trigger" };
    }
    case "auto":
    default: {
      const eff = _effectivePrompt(input.prompt, input.messages);
      // Explicit recall keywords always win — even on short / trivial /
      // slash-command prompts. ("hi remember the deadline" → recall.)
      if (_hasTriggerKeyword(eff, input.triggerKeywords)) {
        return { recall: true, reason: "explicit-recall-trigger" };
      }
      // Specific-reason checks BEFORE the generic length threshold so
      // operators see the precise skip reason in metrics + logs (a 5-char
      // "/help" should report `slash-command`, not `below-threshold`).
      if (_isTrivialPing(eff)) {
        return { recall: false, reason: "trivial-ping" };
      }
      if (eff.startsWith("/") && eff.length < 60) {
        return { recall: false, reason: "slash-command" };
      }
      if (eff.length < input.minPromptChars) {
        return { recall: false, reason: "below-threshold" };
      }
      return { recall: true, reason: "default-substantive" };
    }
  }
}

// --- Skip-decision logging (rate-limited, in-memory, never blocks) ---

interface SkipMetrics {
  calls_total: number;
  skipped_total: number;
  skipped_by_reason: Record<string, number>;
}

const recallMetrics: SkipMetrics = {
  calls_total: 0,
  skipped_total: 0,
  skipped_by_reason: {},
};

const _lastLoggedAt = new Map<string, number>();
const _SKIP_LOG_INTERVAL_MS = 60_000;

function _recordDecision(decision: ShouldRecallResult, sessionHash: string): void {
  recallMetrics.calls_total += 1;
  if (!decision.recall) {
    recallMetrics.skipped_total += 1;
    recallMetrics.skipped_by_reason[decision.reason] =
      (recallMetrics.skipped_by_reason[decision.reason] || 0) + 1;
    const k = `${decision.reason}:${sessionHash}`;
    const last = _lastLoggedAt.get(k) || 0;
    if (Date.now() - last > _SKIP_LOG_INTERVAL_MS) {
      console.log(
        `[memclaw] recall skipped: reason=${decision.reason} ` +
          `policy=${RECALL_POLICY} session=${sessionHash}`,
      );
      _lastLoggedAt.set(k, Date.now());
      // Bounded cleanup so the map doesn't grow indefinitely across
      // sessions. Triggered only after we'd exceed 1000 distinct
      // (reason, session-hash) pairs — orders of magnitude above any
      // real fleet's per-process churn. Cutoff at 10× the log
      // interval (10 min) so entries that haven't been touched in
      // that long are pruned; entries within the rate-limit window
      // stay.
      if (_lastLoggedAt.size > 1000) {
        const cutoff = Date.now() - _SKIP_LOG_INTERVAL_MS * 10;
        for (const [key, ts] of _lastLoggedAt) {
          if (ts < cutoff) _lastLoggedAt.delete(key);
        }
      }
    }
  }
}

function createHashShort(s: string): string {
  return createHash("sha256").update(s).digest("hex").slice(0, 8);
}

/**
 * Snapshot of the current rolling counters for the heartbeat payload.
 * Counters reset on plugin restart — this is intentional for v1.
 */
export function getRecallMetrics(): SkipMetrics {
  return {
    calls_total: recallMetrics.calls_total,
    skipped_total: recallMetrics.skipped_total,
    skipped_by_reason: { ...recallMetrics.skipped_by_reason },
  };
}

// --- Token budget helpers ---

const CHARS_PER_TOKEN_ESTIMATE = 4;

function estimateTokens(text: string): number {
  return Math.ceil(text.length / CHARS_PER_TOKEN_ESTIMATE);
}

function trimToTokenBudget(text: string, maxTokens: number): string {
  const maxChars = maxTokens * CHARS_PER_TOKEN_ESTIMATE;
  if (text.length <= maxChars) return text;
  // Trim from the end, keeping complete lines where possible
  const trimmed = text.slice(0, maxChars);
  const lastNewline = trimmed.lastIndexOf("\n");
  return lastNewline > maxChars * 0.5
    ? trimmed.slice(0, lastNewline)
    : trimmed;
}

// --- Recall cache ---

const RECALL_CACHE_MAX_ENTRIES = 200;
const recallCache = new Map<string, { text: string; ts: number }>();

// --- ContextEngine class ---

export class MemClawContextEngine {
  private config: Record<string, unknown>;

  /**
   * Engine metadata — declares to OpenClaw that we own compaction.
   *
   * "Own" means we are the receiver of ``compact()`` calls and the
   * caller has no fallback path if we don't compact. As of v2.6.4 we
   * fulfill that contract by delegating to OpenClaw's runtime helper
   * ``delegateCompactionToRuntime`` (same bridge the legacy engine
   * uses internally — see ``compact()`` below). Setting this to
   * ``false`` would NOT auto-fall-back to legacy behavior per the
   * SDK docs (``docs/concepts/context-engine.md:241``); it would
   * just disable compaction entirely. So we stay truthful and keep
   * it ``true``.
   */
  readonly info = {
    id: "memclaw",
    name: "MemClaw Context Engine",
    ownsCompaction: true,
  };

  constructor(config: Record<string, unknown> | undefined | null) {
    // Defensive coercion — callers may omit config (CAURA-000). Inject
    // a synthetic, per-instance ``sessionId`` so multiple legacy-compat
    // engines on the same process don't collide on the module-level
    // ``sessionBuffers`` Map (without this, both would compute the same
    // ``getSessionKey({}) = "default:main-<installId>:default"`` key
    // and share an ingest buffer, cross-polluting recall queries).
    if (config == null) {
      logError(
        "ContextEngine constructed without config — using env-var/install defaults (CAURA-000)",
        undefined,
      );
      this.config = {
        sessionId: `legacycompat-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      };
    } else {
      this.config = config;
    }
  }

  async bootstrap(): Promise<void> {
    // Process-level memoization: smoke test runs once per Node process,
    // shared across every engine instance OpenClaw constructs. See
    // ``_processBootstrapPromise`` docstring at top of file for the why.
    // Concurrent callers all await the same in-flight promise (Node is
    // single-threaded so the ``if (!_x) _x = ...`` assignment is safe).
    if (!_processBootstrapPromise) {
      _processBootstrapPromise = this._doBootstrap().catch((e) => {
        // Reset on failure so the next engine instance can retry —
        // matches the pre-change behavior of clearing the cache on a
        // failed bootstrap, just at process scope instead of instance.
        _processBootstrapPromise = null;
        throw e;
      });
    }
    return _processBootstrapPromise;
  }

  private async _doBootstrap(): Promise<void> {
    // ``Quiet`` variant: the factoryCtx wrapper that OpenClaw passes at
    // construction has no per-call session identity, so the install-default
    // fallback IS the bootstrap design — the warn was pure noise. Per-turn
    // call sites (assemble/ingest/afterTurn/prepareSubagentSpawn) still use
    // the loud ``resolveAgentId`` so a real per-turn regression remains
    // visible. (CAURA-000 — customer's 18h goodclaw 2.8.1 window shows
    // 100% of residual warn noise comes from this exact bootstrap path.)
    const bootAgentId = resolveAgentIdQuiet(this.config);
    console.log(
      `[memclaw] ContextEngine bootstrap: agent=${bootAgentId}, ` +
        `fleet=${MEMCLAW_FLEET_ID || "(unset)"}, ` +
        `config keys=${Object.keys(this.config || {}).join(",") || "(empty)"}`,
    );

    const testContent = `memclaw-smoke-${Date.now()}`;
    let writtenId: string | null = null;
    // Hoisted out of the try so the finally-block cleanup DELETE can pass
    // the tenant_id the backend requires (see below).
    let tid: string | null = null;
    try {
      tid = await ensureTenantId();
      const wr = (await apiCall("POST", "/memories", {
        tenant_id: tid,
        agent_id: "__health_check__",
        content: testContent,
        memory_type: "fact",
        tags: ["__smoke_test__"],
      })) as Record<string, unknown>;
      writtenId =
        (wr?.id as string) ||
        ((wr?.memory as Record<string, unknown>)?.id as string) ||
        ((wr?.data as Record<string, unknown>)?.id as string) ||
        null;
      if (!writtenId) {
        console.warn("[memclaw] bootstrap: could not extract memory ID — smoke test memory may not be cleaned up");
      }

      let top: Record<string, unknown> | undefined;
      let score = 0;
      for (let attempt = 0; attempt < 3; attempt++) {
        await new Promise((r) => setTimeout(r, 500));
        const sr = (await apiCall("POST", "/search", {
          tenant_id: tid,
          query: testContent,
          top_k: 1,
        })) as Record<string, unknown> | Record<string, unknown>[];
        const firstResult = parseSearchItems(sr)[0];
        top = firstResult as Record<string, unknown> | undefined;
        score = (top?.score as number) ?? (top?.similarity as number) ?? 0;
        if (top && score >= 0.7) break;
      }

      if (!top) {
        console.error(
          "[memclaw] SMOKE TEST FAILED: search returned no results — check EMBEDDING_PROVIDER",
        );
      } else if (score < 0.7) {
        console.error(
          `[memclaw] SMOKE TEST WARNING: score ${score.toFixed(3)} < 0.7 — embeddings may be degraded`,
        );
      } else {
        console.log(
          `[memclaw] Smoke test passed (score: ${score.toFixed(3)})`,
        );
      }
    } catch (e: unknown) {
      logErrorCritical("SMOKE TEST ERROR", e);
    } finally {
      // DELETE /memories/{id} requires tenant_id as a query param
      // (core-api ``tenant_id: str = Query(...)``); omitting it returns 422
      // and the smoke-test memory leaks into the tenant on every restart.
      if (writtenId && tid) {
        apiCall(
          "DELETE",
          `/memories/${encodeURIComponent(writtenId)}`,
          undefined,
          { tenant_id: tid },
        ).catch(() => {});
      }
    }
    // No need to set a flag here — the resolved ``_processBootstrapPromise``
    // serves as the "done" signal; subsequent ``await``s are instant.
  }

  /**
   * ingest — buffer messages per session and persist user messages as episodes.
   * Enables buildQueryFromMessages for richer recall in assemble().
   */
  async ingest(message: IngestMessage): Promise<void> {
    await this.bootstrap();
    if (!message || !message.content) return;

    const sessionKey = getSessionKey(
      this.config,
      message as unknown as Record<string, unknown>,
    );
    pushToBuffer(sessionKey, message);

    // Persist user messages as episode memories (async, non-blocking).
    // Capped at MAX_INGEST_WRITES_PER_SESSION to prevent memory spam in long sessions.
    // The in-memory buffer still receives all messages for buildQueryFromMessages.
    if (message.role === "user") {
      const content =
        typeof message.content === "string"
          ? message.content
          : JSON.stringify(message.content);
      if (content.length < MIN_TURN_CONTENT_LENGTH) return;

      const writeCount = sessionIngestCounts.get(sessionKey) || 0;
      if (writeCount >= MAX_INGEST_WRITES_PER_SESSION) return;

      try {
        const tid = await ensureTenantId();
        // Per-call ``message`` (or the wrapper OpenClaw passes around
        // it) carries the agent identity via ``sessionKey``; the
        // factory-time ``this.config`` doesn't. Thread ``message``
        // first so the resolver can extract agent name from
        // ``"agent:NAME:CHANNEL:..."`` instead of falling back to
        // the install-default. (CAURA-000)
        const agentId = resolveAgentId(
          message as unknown as Record<string, unknown>,
          this.config,
        );
        const truncated =
          content.length > MAX_TURN_SUMMARY_LENGTH
            ? content.slice(0, MAX_TURN_SUMMARY_LENGTH) + "..."
            : content;
        await apiCall("POST", "/memories", {
          tenant_id: tid,
          agent_id: agentId,
          fleet_id: MEMCLAW_FLEET_ID || undefined,
          content: truncated,
          memory_type: "episode",
          tags: ["auto-ingest", "user-message"],
        });
        sessionIngestCounts.set(sessionKey, writeCount + 1);
      } catch (e: unknown) {
        logError("Failed to persist ingested message", e);
      }
    }
  }

  /**
   * assemble — called before every LLM call.
   *
   * Returns the OpenClaw `AssembleResult` shape (`messages`,
   * `estimatedTokens`, optional `systemPromptAddition`) on every path.
   * Pre-CAURA-444 we returned `{}` on empty output, which the runtime
   * treats as a thrown error and falls back to pre-assembly state.
   *
   * Recall is gated by `shouldRecall()`. On skip we return the static
   * education + identity block only — no `/search` HTTP call. The
   * model can still call `memclaw_recall` explicitly when it judges it
   * needs LTM on a short turn.
   */
  async assemble(
    params: AssembleBudget & {
      sessionId?: string;
      sessionKey?: string;
      messages?: Array<{ role: string; content: unknown }>;
      availableTools?: Set<string>;
      citationsMode?: string;
      model?: string;
      prompt?: string;
    },
    legacyPrompt?: string,
  ): Promise<{
    system?: string;
    systemPromptAddition?: string;
    messages?: unknown[];
    tokenEstimate?: number;
    estimatedTokens?: number;
  }> {
    // Echo the (defensively coerced) input messages on every return path,
    // including the catch-all below. OpenClaw 2026.5.4's
    // selection-BfCSa_QL.js:7677 reads ``assembled.messages`` and
    // overwrites ``activeSession.agent.state.messages`` when the
    // reference differs from input — returning ``undefined`` there
    // produces a downstream ``Cannot read properties of undefined
    // (reading 'slice')`` that the runtime catches as a generic
    // ``context engine assemble failed`` warning with no stack
    // (``String(err)`` strips the trace), so a stray throw inside our
    // assemble surfaces in customer logs as a useless top-line and the
    // ``systemPromptAddition`` is silently dropped. The outer try/catch
    // below mirrors this: on ANY internal throw we log the full stack
    // ourselves and still return a well-shaped, no-injection result —
    // OpenClaw moves on with the original messages and the agent loses
    // one turn of memory injection instead of the entire turn breaking.
    const safeMessages: Array<{ role: string; content: unknown }> = Array.isArray(
      params?.messages,
    )
      ? (params!.messages as Array<{ role: string; content: unknown }>)
      : [];

    try {
      return await this._assembleInner(params, legacyPrompt, safeMessages);
    } catch (err: unknown) {
      // Log context + full stack in a single ``console.error`` so log
      // aggregators that filter or split by severity can't drop one
      // half. Two separate emissions (e.g. ``console.error`` for the
      // context line and ``console.warn`` for the stack) get
      // dropped-or-kept independently by level-based filters, which
      // costs us exactly the forensic signal this catch exists to
      // surface. OpenClaw's own catch logs only ``String(err)`` which
      // strips the stack — this is the only place a customer's
      // gateway log will carry the trace.
      const stack =
        err instanceof Error && err.stack ? err.stack : String(err);
      console.error(
        `[memclaw] assemble: unexpected error (returning safe fallback)\n${stack}`,
      );
      return { messages: safeMessages, systemPromptAddition: "", estimatedTokens: 0 };
    }
  }

  private async _assembleInner(
    params: AssembleBudget & {
      sessionId?: string;
      sessionKey?: string;
      messages?: Array<{ role: string; content: unknown }>;
      availableTools?: Set<string>;
      citationsMode?: string;
      model?: string;
      prompt?: string;
    },
    legacyPrompt: string | undefined,
    safeMessages: Array<{ role: string; content: unknown }>,
  ): Promise<{
    system?: string;
    systemPromptAddition?: string;
    messages?: unknown[];
    tokenEstimate?: number;
    estimatedTokens?: number;
  }> {
    await this.bootstrap();

    // Two call shapes supported:
    //   - Modern OpenClaw (>= v2026.4.5): assemble({sessionId, messages, prompt, ...})
    //   - Legacy:                          assemble(budget, prompt)
    // Both flow through the same optional-chaining reads below
    // (`params?.tokenBudget`, `params?.messages`, `params?.prompt`)
    // and the legacy second-arg `legacyPrompt` fallback; no explicit
    // branch is needed.
    const tokenBudget = params?.tokenBudget || 0;
    const incomingMessages = safeMessages;
    const prompt = (params?.prompt as string | undefined) ?? legacyPrompt;

    // OpenClaw 2026.5.4 invokes ``assemble({sessionId, sessionKey, ...})``
    // — ``sessionKey`` is the canonical agent identity source
    // (``"agent:NAME:CHANNEL:..."``). Thread ``params`` first so the
    // resolver extracts the real agent name instead of falling back to
    // the install-default. Without this, the identity block below
    // carries ``main-<installId>`` and every downstream LLM tool call
    // propagates the wrong agent_id. (CAURA-000)
    const agentId = resolveAgentId(
      params as unknown as Record<string, unknown>,
      this.config,
    );
    const fleetId = MEMCLAW_FLEET_ID || undefined;

    // --- Section 1: Education (always emitted; cheap, static) ---
    const educationText = memclawPromptSectionText(new Set(MEMCLAW_TOOLS));
    const identityBlock =
      `\n**Your identity**: agent_id=\`${agentId}\`` +
      (fleetId ? `, fleet_id=\`${fleetId}\`` : "") +
      (MEMCLAW_TENANT_ID ? `, tenant_id=\`${MEMCLAW_TENANT_ID}\`` : "") +
      "\n";
    const operatorPrompt = process.env.MEMCLAW_EDUCATION_PROMPT || "";
    const operatorBlock = operatorPrompt
      ? `\n## Operator Instructions\n${operatorPrompt}\n`
      : "";

    // --- Section 4: Keystone rules (mandatory policies, CAURA-000) ---
    //
    // Fetched + APPENDED unconditionally — they sit AFTER education,
    // identity, and the operator prompt so recency-sensitive models
    // treat them as the most-recent (and therefore highest-priority)
    // instruction in the system prompt. Most current LLMs weight
    // later-in-prompt content more heavily than earlier content, and
    // keystones are exactly what we want to override the preceding
    // sections when they conflict. (Recall content, when present, is
    // appended even later — that's fine; keystones describe POLICY
    // and recall describes FACTS, so they don't compete.)
    //
    // ``fetchKeystonesBlock`` is fail-open: it returns ``""`` on any
    // backend / auth / network error so a transient outage degrades to
    // "no rules injected" rather than blocking ``assemble``.
    //
    // Start the keystone fetch BEFORE the recall gate runs so its
    // network round-trip overlaps with the synchronous decision (~1 ms)
    // and any subsequent /search call (when the gate allows it). The
    // shouldRecall predicate is pure and microsecond-cheap, so all the
    // wall-clock time we save here is real keystone latency, not gate
    // CPU. Await happens only at the point where we need the value.
    const keystonePromise = fetchKeystonesBlock({
      agentId,
      fleetId,
    });

    const sessionKey = getSessionKey(
      this.config,
      params as unknown as Record<string, unknown>,
    );
    const sessionHash = createHashShort(sessionKey);

    // --- Recall gate ---
    const decision = shouldRecall({
      policy: RECALL_POLICY,
      prompt,
      messages: incomingMessages,
      minPromptChars: RECALL_MIN_PROMPT_CHARS,
      triggerKeywords: RECALL_TRIGGER_KEYWORDS,
      sessionKey,
      denySessions: RECALL_DENY_SESSIONS,
    });
    _recordDecision(decision, sessionHash);

    // Block on keystones now that the gate decision is in. In the skip
    // path below we return immediately; in the recall path further down
    // the /search call also overlaps with whatever keystone latency
    // remained.
    const keystoneBlock = await keystonePromise;
    const staticSection =
      educationText + identityBlock + operatorBlock + keystoneBlock;

    if (!decision.recall) {
      const tokens = estimateTokens(staticSection);
      // OpenClaw 2026.5.4 AssembleResult contract: must include
      // ``messages`` (echo of input is safe — reference equality means
      // the runtime won't overwrite activeSession.agent.state.messages)
      // and ``estimatedTokens``. The legacy ``system`` + ``tokenEstimate``
      // aliases stay for older runtimes that read them; modern OpenClaw
      // ignores extra fields.
      const out: {
        system?: string;
        systemPromptAddition?: string;
        messages?: unknown[];
        tokenEstimate?: number;
        estimatedTokens?: number;
      } = {
        system: staticSection,
        systemPromptAddition: staticSection,
        messages: incomingMessages,
        estimatedTokens: tokens,
      };
      if (tokenBudget > 0) {
        out.tokenEstimate = tokens;
      }
      return out;
    }

    // --- Token budget split: 20% education, 80% recall ---
    let recallBudgetTokens = 0;
    if (tokenBudget > 0) {
      const staticTokens = estimateTokens(staticSection);
      const educationBudget = Math.floor(tokenBudget * 0.2);
      const recallBudget = tokenBudget - educationBudget;
      const educationOverflow = Math.max(0, staticTokens - educationBudget);
      recallBudgetTokens = Math.max(0, recallBudget - educationOverflow);
    }

    // --- Section 2: Recalled memories (cached) ---
    const queryFromMessages = buildQueryFromMessages(sessionKey, prompt);
    const searchQuery = queryFromMessages || prompt || agentId;
    const tenantPrefix = getTenantPrefix(this.config);
    const cacheKey = `${tenantPrefix}:${agentId}:${searchQuery}`;
    const cached = recallCache.get(cacheKey);
    let recallBlock = "";

    if (cached && Date.now() - cached.ts < RECALL_CACHE_TTL_MS) {
      recallBlock = cached.text;
    } else {
      const now = Date.now();
      for (const [k, v] of recallCache) {
        if (now - v.ts > RECALL_CACHE_TTL_MS) recallCache.delete(k);
      }
      const controller = new AbortController();
      const timeout = setTimeout(
        () => controller.abort(),
        RECALL_TIMEOUT_MS,
      );
      try {
        const tid = await ensureTenantId();
        const searchBody: Record<string, unknown> = {
          tenant_id: tid,
          filter_agent_id: agentId,
          query: searchQuery,
          top_k: 5,
        };
        const sr = (await apiCall(
          "POST",
          "/search",
          searchBody,
          undefined,
          controller.signal,
        )) as Record<string, unknown> | Record<string, unknown>[];
        const results = parseSearchItems(sr);
        if (results.length > 0) {
          const lines = results.map(
            (m: Record<string, unknown>) =>
              `- [${(m.memory_type as string) || "memory"}] ${((m.content as string) || "").slice(0, MAX_RECALL_CONTENT_LENGTH)}`,
          );
          recallBlock =
            "\n## Recalled Memory Context\n" +
            "The following memories were retrieved from MemClaw for this session:\n" +
            lines.join("\n") +
            "\n";
        }
        if (recallBlock) {
          if (recallCache.size >= RECALL_CACHE_MAX_ENTRIES) {
            const oldest = recallCache.keys().next().value;
            if (oldest !== undefined) recallCache.delete(oldest);
          }
          recallCache.set(cacheKey, { text: recallBlock, ts: Date.now() });
        }
      } catch (e: unknown) {
        logError("recall failed", e);
      } finally {
        clearTimeout(timeout);
      }
    }

    if (tokenBudget > 0) {
      if (recallBudgetTokens <= 0) {
        recallBlock = "";
      } else if (recallBlock) {
        recallBlock = trimToTokenBudget(recallBlock, recallBudgetTokens);
      }
    }

    const systemPromptAddition = staticSection + recallBlock;
    const estimatedTokens = estimateTokens(systemPromptAddition);

    // Always return AssembleResult-shaped payload, even when only the
    // static block is non-empty. OpenClaw 2026.5.4's AssembleResult
    // (plugin-sdk/src/context-engine/types.d.ts) requires ``messages``
    // and ``estimatedTokens``; the runtime reads ``messages`` by
    // reference and overwrites ``activeSession.agent.state.messages``
    // if it differs from input — so echo the input array. The legacy
    // ``system`` + ``tokenEstimate`` aliases stay for older runtimes;
    // modern OpenClaw reads ``systemPromptAddition`` and ignores
    // extras.
    return tokenBudget > 0
      ? {
          system: systemPromptAddition,
          systemPromptAddition,
          messages: incomingMessages,
          tokenEstimate: estimatedTokens,
          estimatedTokens,
        }
      : {
          system: systemPromptAddition,
          systemPromptAddition,
          messages: incomingMessages,
          estimatedTokens,
        };

    // Note: we intentionally do NOT mutate `params.messages`. The OpenClaw
    // runtime replaces the session messages if our return's `messages`
    // differs from input, which we never want from `assemble()` —
    // compaction is `compact()`'s job.
  }

  /**
   * Compact an over-budget session.
   *
   * Two responsibilities, in order:
   *
   *   1. **Persist** any pre-computed summary (passed in by OpenClaw
   *      when its compaction pipeline ran the summarization step
   *      before invoking us) as a MemClaw episode memory tagged
   *      ``auto-compaction``. This is the long-standing
   *      observability side-effect — surviving summaries get
   *      recallable later via ``memclaw_recall``.
   *
   *   2. **Delegate** the actual transcript-reduction work to
   *      ``delegateCompactionToRuntime`` from
   *      ``openclaw/plugin-sdk``. This is the same bridge OpenClaw's
   *      own legacy engine uses internally (see
   *      ``delegate-DzhryVAO.js`` in the installed runtime). It
   *      invokes ``compactEmbeddedPiSessionDirect`` which performs
   *      summarization + transcript rotation + writes the result
   *      to the session file. Returns a properly-shaped
   *      ``CompactResult`` (``{ok, compacted, reason, result?}``) per
   *      ``plugin-sdk/src/context-engine/types.d.ts``.
   *
   * # Why this shape (CAURA-000-hotfix, v2.6.5)
   *
   * Pre-v2.6.4, ``compact()`` returned ``undefined`` — not even a
   * valid ``CompactResult``. With ``info.ownsCompaction: true`` (PR
   * #212), this told OpenClaw "we own compaction" and the runtime
   * delegated to us — but we returned nothing, so the over-budget
   * session never shrank. The first deployments to hit this were
   * WhatsApp group chats (transcript-per-participant pushes them
   * past the 272k token budget); direct chats stayed under budget
   * and never tripped the wedge. Agent looped on
   * ``memclaw_keystones`` calls trying to make any progress, and
   * eventual final replies were silently dropped (``Skipping
   * auto-reply: final-only`` runtime warning).
   *
   * # Why we don't just return a "declined" CompactResult
   *
   * Inspection of OpenClaw 2026.5.4's pi-embedded overflow recovery
   * loop (``pi-embedded-X0afS0ip.js:2466-2477``) shows that
   * ``{compacted: false}`` does NOT trigger a safeguard fallback —
   * it falls through to "normal handling" which leaves the session
   * over-budget. The `compactionSafeguardExtension` listener on
   * ``session_before_compact`` fires from a different code path
   * (the LLM-driven summarization pipeline), not from a declined
   * engine compact. The only reliable way for our engine to "let
   * OpenClaw handle compaction" while still owning the slot is to
   * call ``delegateCompactionToRuntime`` ourselves.
   *
   * # SDK resolution
   *
   * The ``openclaw/plugin-sdk`` package cannot be imported via
   * bare-spec from our native-loaded ``.js`` plugin — see
   * ``openclaw-sdk-bridge.ts`` for the runtime-discovery dance that
   * works around this. If the bridge returns ``null`` (e.g.,
   * exotic install layout, embedded test runner), we return a
   * structured "declined" ``CompactResult`` so OpenClaw at least
   * has a well-shaped response — same regression behavior as
   * v2.6.3, no worse.
   */
  async compact(
    context: CompactContext,
  ): Promise<{ ok: boolean; compacted: boolean; reason?: string; result?: unknown }> {
    // 1. Persist OpenClaw's summary into MemClaw as an episode
    // memory. This runs regardless of whether the delegation
    // succeeds below — even on a degraded environment the summary
    // is worth keeping if we can. Failure here is logged and
    // swallowed; never let it cascade into "compaction failed."
    const summary = context?.summary || context?.compactionSummary;
    if (summary && typeof summary === "string") {
      try {
        const tid = await ensureTenantId();
        const agentId = resolveAgentId(
          context as Record<string, unknown>,
          this.config,
        );
        await apiCall("POST", "/memories", {
          tenant_id: tid,
          agent_id: agentId,
          fleet_id: MEMCLAW_FLEET_ID || undefined,
          content: summary,
          memory_type: "episode",
          tags: ["auto-compaction"],
        });
      } catch (e: unknown) {
        logError("Failed to persist compaction summary", e);
      }
    }

    // 2. Defensive pre-check: only delegate when we have the
    // minimum params the runtime helper needs. The
    // ``compactEmbeddedPiSessionDirect`` path inside
    // ``delegateCompactionToRuntime`` requires a real session id
    // and file to acquire locks and read transcript bytes —
    // calling it with synthetic / empty input has been observed
    // to crash the gateway process in ways our outer ``try``
    // cannot catch (unhandled rejection inside the runtime
    // helper's own async chain). OpenClaw's production
    // compaction call site
    // (``pi-embedded-X0afS0ip.js:2447`` and friends) ALWAYS
    // passes both fields per ``ContextEngine.compact``'s
    // type signature, so this guard is invisible in the
    // happy path; it only blocks degenerate calls
    // (gateway-RPC test probes, admin tools, future
    // refactors).
    const ctx = context as Record<string, unknown> | undefined;
    const hasSessionId =
      typeof ctx?.sessionId === "string" && (ctx.sessionId as string).length > 0;
    const hasSessionFile =
      typeof ctx?.sessionFile === "string" && (ctx.sessionFile as string).length > 0;
    if (!hasSessionId || !hasSessionFile) {
      return {
        ok: false,
        compacted: false,
        reason:
          "compact called without sessionId/sessionFile — runtime " +
          "delegation skipped to avoid an unsafe call into " +
          "delegateCompactionToRuntime. Production callers always supply both " +
          "per the ContextEngine.compact contract.",
      };
    }

    // 3. Delegate the actual compaction work to OpenClaw's runtime
    // helper. ``getOpenClawSdk`` is cached, so this is a hot-path
    // dictionary lookup after the first call.
    //
    // Two distinct try/catch scopes — one for the bridge resolution,
    // one for the delegate call. Both ``getOpenClawSdk`` and the
    // helper are documented as never-throw, but defensive code is
    // wise here. Splitting the catches keeps the log label
    // accurate: an operator seeing "delegateCompactionToRuntime
    // threw" should know the throw came from inside the runtime
    // helper, not from the resolver walk.
    let sdk: Awaited<ReturnType<typeof getOpenClawSdk>>["sdk"];
    let pkgRoot: Awaited<ReturnType<typeof getOpenClawSdk>>["pkgRoot"];
    try {
      ({ sdk, pkgRoot } = await getOpenClawSdk());
    } catch (e: unknown) {
      logError("openclaw-sdk-bridge resolution failed", e);
      return {
        ok: false,
        compacted: false,
        reason: e instanceof Error ? e.message : String(e),
      };
    }

    if (sdk?.delegateCompactionToRuntime) {
      try {
        const raw = await sdk.delegateCompactionToRuntime(context as unknown);
        // Defensive shape check against SDK contract drift / version
        // skew. The two required ``CompactResult`` fields are
        // ``ok: boolean`` and ``compacted: boolean`` per
        // ``plugin-sdk/src/context-engine/types.d.ts``; if the helper
        // returns anything else (null, undefined, primitive, an
        // object with wrong-typed fields), downstream consumers
        // (OpenClaw runtime accessing ``result.ok`` /
        // ``result.compacted``) would NPE or branch on truthy strings
        // — the same class of bug v2.6.3's ``undefined`` return
        // caused. Surface contract violations with a named reason.
        if (
          !raw ||
          typeof raw !== "object" ||
          typeof (raw as Record<string, unknown>).ok !== "boolean" ||
          typeof (raw as Record<string, unknown>).compacted !== "boolean"
        ) {
          return {
            ok: false,
            compacted: false,
            reason:
              "delegateCompactionToRuntime returned invalid CompactResult shape — SDK contract violation",
          };
        }
        // Pass the runtime's verdict through verbatim. Touching
        // its fields would risk drift from the SDK contract.
        return raw as {
          ok: boolean;
          compacted: boolean;
          reason?: string;
          result?: unknown;
        };
      } catch (e: unknown) {
        logError("delegateCompactionToRuntime threw", e);
        return {
          ok: false,
          compacted: false,
          reason: e instanceof Error ? e.message : String(e),
        };
      }
    }

    // Bridge found the openclaw package on disk but the plugin-sdk
    // it exports did not include ``delegateCompactionToRuntime``.
    // Most likely cause is OpenClaw older than 2026.5.x — the
    // helper landed in CHANGELOG line 4940 ("expose
    // ``delegateCompactionToRuntime(...)`` on the public plugin
    // SDK"). Including the discovered ``pkgRoot`` in the log lets
    // operators run ``ls -la $pkgRoot`` and verify the actual
    // version on disk without guessing the install location.
    if (pkgRoot !== null) {
      // The discovered ``pkgRoot`` IS valuable to operators (they can
      // ``ls -la $pkgRoot`` to verify the install), so keep it in
      // the gateway-log line above via ``logError``. But the
      // returned ``CompactResult.reason`` flows downstream through
      // OpenClaw into runtime metrics, surface dashboards, and
      // possibly cross-system error reports — places where leaking
      // an absolute filesystem path is undesirable. Use a static
      // remediation hint there; operators with log access already
      // have the path.
      logError(
        "openclaw-sdk-bridge",
        new Error(
          `delegateCompactionToRuntime not exported by the openclaw plugin-sdk at "${pkgRoot}" — ` +
            "OpenClaw may be too old (or the install is corrupted). Update openclaw to 2026.5.x or later.",
        ),
      );
      return {
        ok: false,
        compacted: false,
        reason:
          "delegateCompactionToRuntime not available in openclaw plugin-sdk " +
          "(version too old or corrupted? Run: openclaw --version)",
      };
    }

    // Bridge did not discover openclaw at all — the launcher's
    // parent path didn't contain a ``package.json`` with
    // ``name: "openclaw"`` within MAX_WALK_DEPTH. Log once so
    // operators can see this in the gateway log, then return a
    // structured fallback.
    logError(
      "openclaw-sdk-bridge",
      new Error(
        "delegateCompactionToRuntime unavailable — openclaw could not be discovered from " +
          "the plugin's runtime path. Check that openclaw is installed at a discoverable " +
          "location (global npm, brew, nvm, asdf, or a source checkout) and that the gateway " +
          "is launched via the openclaw script.",
      ),
    );
    return {
      ok: false,
      compacted: false,
      reason: "openclaw plugin-sdk not discoverable from plugin runtime",
    };
  }

  /** afterTurn — auto-write turn summary. Enabled by default; opt out with MEMCLAW_AUTO_WRITE_TURNS=false. */
  async afterTurn(context: AfterTurnContext): Promise<void> {
    if (!MEMCLAW_AUTO_WRITE_TURNS) return;

    const lastAssistant = context?.messages
      ?.filter((m) => m.role === "assistant")
      ?.slice(-1)?.[0];
    if (!lastAssistant?.content) return;

    const content =
      typeof lastAssistant.content === "string"
        ? lastAssistant.content
        : JSON.stringify(lastAssistant.content);
    if (content.length < MIN_TURN_CONTENT_LENGTH) return;

    try {
      const tid = await ensureTenantId();
      const agentId = resolveAgentId(
        context as Record<string, unknown>,
        this.config,
      );
      const turnSummary =
        content.length > MAX_TURN_SUMMARY_LENGTH
          ? content.slice(0, MAX_TURN_SUMMARY_LENGTH) + "..."
          : content;
      await apiCall("POST", "/memories", {
        tenant_id: tid,
        agent_id: agentId,
        fleet_id: MEMCLAW_FLEET_ID || undefined,
        content: turnSummary,
        memory_type: "episode",
        tags: ["auto-turn-summary"],
      });
    } catch (e: unknown) {
      // 409 dedup rejection is expected when the same agent emits identical
      // content twice (very common for short replies). The existing memory
      // already encodes what we'd have written → soft no-op. Swallow to
      // keep operator-log signal-to-noise high; non-409 errors still log.
      if (isDuplicateMemoryError(e)) return;
      logError("Failed to persist turn summary", e);
    }
  }

  async prepareSubagentSpawn(
    context: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    return {
      memclawAgentId: resolveAgentId(context, this.config),
      memclawFleetId: MEMCLAW_FLEET_ID,
    };
  }

  async onSubagentEnded(_context: unknown): Promise<void> {}
}

