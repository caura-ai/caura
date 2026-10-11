/**
 * Value metering, plugin side: the turns and tokens a session spent before a
 * memory was written (docs/plans/value-metering.md in the repo root).
 *
 * OpenClaw's assistant messages carry the provider's own ``usage``. Each
 * ``afterTurn`` adds the usage of the assistant messages not seen before, per
 * session; the next memory write in that session sends the totals as
 * ``origin`` (source ``plugin``) and starts counting again. These are the
 * provider's numbers, read beside the agent — never the model's own estimate.
 *
 * ``afterTurn`` may hand over the whole history each time, so a message is
 * counted once: by its ``timestamp`` when it has one, else only the last
 * assistant message of the turn is.
 */

export interface Origin {
  tokens_in?: number;
  tokens_out?: number;
  turns: number;
  source: "plugin";
}

interface Tally {
  tokensIn: number;
  tokensOut: number;
  sawTokens: boolean;
  turns: number;
  lastTimestamp: number;
}

const MAX_SESSIONS = 500;
const tallies = new Map<string, Tally>();

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

/** Input and output tokens from one message's ``usage``, whatever the provider spelled them. */
export function usageOf(message: unknown): { tokensIn: number; tokensOut: number } | null {
  const usage = (message as { usage?: Record<string, unknown> } | null)?.usage;
  if (!usage || typeof usage !== "object") return null;
  const tokensIn =
    num(usage.input) + num(usage.cacheRead) + num(usage.cacheWrite) ||
    num(usage.inputTokens) + num(usage.cacheReadInputTokens) + num(usage.cacheCreationInputTokens) ||
    num(usage.input_tokens) + num(usage.cache_read_input_tokens) + num(usage.cache_creation_input_tokens) ||
    num(usage.prompt_tokens) ||
    num(usage.promptTokens);
  const tokensOut =
    num(usage.output) || num(usage.outputTokens) || num(usage.output_tokens) || num(usage.completion_tokens) || num(usage.completionTokens);
  return tokensIn || tokensOut ? { tokensIn, tokensOut } : null;
}

function tallyFor(sessionKey: string): Tally {
  let t = tallies.get(sessionKey);
  if (!t) {
    if (tallies.size >= MAX_SESSIONS) {
      const oldest = tallies.keys().next().value;
      if (oldest !== undefined) tallies.delete(oldest);
    }
    t = { tokensIn: 0, tokensOut: 0, sawTokens: false, turns: 0, lastTimestamp: 0 };
    tallies.set(sessionKey, t);
  }
  return t;
}

/** Count one turn of a session, and the usage of its assistant messages not counted before. */
export function recordTurn(sessionKey: string | undefined, messages: Array<{ role: string } & Record<string, unknown>> | undefined): void {
  if (!sessionKey) return;
  const t = tallyFor(sessionKey);
  t.turns += 1;
  const assistant = (messages ?? []).filter((m) => m?.role === "assistant");
  const stamped = assistant.filter((m) => typeof m.timestamp === "number");
  const fresh =
    stamped.length === assistant.length && assistant.length > 0
      ? stamped.filter((m) => (m.timestamp as number) > t.lastTimestamp)
      : assistant.slice(-1);
  for (const m of fresh) {
    const u = usageOf(m);
    if (u) {
      t.tokensIn += u.tokensIn;
      t.tokensOut += u.tokensOut;
      t.sawTokens = true;
    }
    if (typeof m.timestamp === "number") t.lastTimestamp = Math.max(t.lastTimestamp, m.timestamp);
  }
}

/** The origin to send with this session's next write, and reset its count. Undefined when nothing was counted. */
export function takeOrigin(sessionKey: string | undefined): Origin | undefined {
  if (!sessionKey) return undefined;
  const t = tallies.get(sessionKey);
  if (!t || t.turns === 0) return undefined;
  const origin: Origin = { turns: t.turns, source: "plugin" };
  if (t.sawTokens) {
    origin.tokens_in = t.tokensIn;
    origin.tokens_out = t.tokensOut;
  }
  t.tokensIn = 0;
  t.tokensOut = 0;
  t.sawTokens = false;
  t.turns = 0;
  return origin;
}

/** The header that puts a call in its OpenClaw session (core-api ``X-Caura-Session-Id``). */
export function sessionHeader(sessionKey: string | undefined): Record<string, string> | undefined {
  if (!sessionKey) return undefined;
  // core-api accepts visible ASCII up to 200; OpenClaw keys look like "agent:main:telegram:...".
  const safe = sessionKey.replace(/[^A-Za-z0-9._:-]/g, "_").slice(0, 200);
  return safe ? { "X-Caura-Session-Id": `openclaw:${safe}`.slice(0, 200) } : undefined;
}

export function _resetForTests(): void {
  tallies.clear();
}
