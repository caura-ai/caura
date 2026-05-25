/**
 * MemClaw ContextEngine — lifecycle hooks for OpenClaw memory integration.
 *
 * Provides: bootstrap (smoke test), ingest (message buffering + persistence),
 * assemble (token-budget-aware recall injection), afterTurn (auto-write),
 * compact (persist compaction summaries).
 *
 * Security:
 * - afterTurn enabled by default; opt out with MEMCLAW_AUTO_WRITE_TURNS=false
 * - Recall timeout enforced via AbortController
 */

import { createHash } from "crypto";
import { apiCall } from "./transport.js";
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
import { resolveAgentId } from "./resolve-agent.js";
import { logError, logErrorCritical } from "./logger.js";
import { fetchKeystonesBlock } from "./keystones.js";

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

function getTenantPrefix(config: Record<string, unknown>): string {
  return (config.tenantId as string) || MEMCLAW_TENANT_ID || "default";
}

function getSessionKey(config: Record<string, unknown>): string {
  const tenantPrefix = getTenantPrefix(config);
  // Always prefix with tenant to prevent cross-tenant buffer sharing,
  // even when config.sessionKey is provided.
  const sessionPart =
    (config.sessionKey as string) ||
    resolveAgentId(config) + ":" + (config.sessionId || "default");
  return tenantPrefix + ":" + sessionPart;
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
  private _bootstrapped = false;
  private _bootstrapPromise: Promise<void> | null = null;

  /** Engine metadata — tells OpenClaw this engine owns compaction. */
  readonly info = {
    id: "memclaw",
    name: "MemClaw Context Engine",
    ownsCompaction: true,
  };

  constructor(config: Record<string, unknown>) {
    this.config = config;
  }

  async bootstrap(): Promise<void> {
    if (this._bootstrapped) return;
    if (!this._bootstrapPromise) {
      this._bootstrapPromise = this._doBootstrap().catch((e) => {
        this._bootstrapPromise = null;
        throw e;
      });
    }
    return this._bootstrapPromise;
  }

  private async _doBootstrap(): Promise<void> {
    const bootAgentId = resolveAgentId(this.config);
    console.log(
      `[memclaw] ContextEngine bootstrap: agent=${bootAgentId}, ` +
        `fleet=${MEMCLAW_FLEET_ID || "(unset)"}, ` +
        `config keys=${Object.keys(this.config || {}).join(",") || "(empty)"}`,
    );

    const testContent = `memclaw-smoke-${Date.now()}`;
    let writtenId: string | null = null;
    try {
      const tid = await ensureTenantId();
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
        const firstResult = Array.isArray(sr)
          ? sr[0]
          : ((sr?.results as Record<string, unknown>[]) || [])[0];
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
      if (writtenId) {
        apiCall(
          "DELETE",
          `/memories/${encodeURIComponent(writtenId)}`,
        ).catch(() => {});
      }
    }
    this._bootstrapped = true;
  }

  /**
   * ingest — buffer messages per session and persist user messages as episodes.
   * Enables buildQueryFromMessages for richer recall in assemble().
   */
  async ingest(message: IngestMessage): Promise<void> {
    await this.bootstrap();
    if (!message || !message.content) return;

    const sessionKey = getSessionKey(this.config);
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
        const agentId = resolveAgentId(this.config);
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

    const agentId = resolveAgentId(this.config);
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

    const sessionKey = getSessionKey(this.config);
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
        const results = Array.isArray(sr)
          ? sr
          : ((sr as Record<string, unknown>)?.results as
              | Record<string, unknown>[]
              | undefined) || [];
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

  async compact(context: CompactContext): Promise<undefined> {
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
    return undefined;
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
