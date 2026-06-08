/**
 * Tests for the context engine's recall-policy gate (CAURA-444).
 *
 * The OpenClaw runtime calls our `assemble()` on every prompt assembly
 * with no triviality signal of its own; without `shouldRecall()` we
 * fire `/search` on every turn — including pings, no-reply lurk turns,
 * and tool follow-ups. These tests pin the gate's policy semantics.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";

import {
  shouldRecall,
  getRecallMetrics,
  isDuplicateMemoryError,
  type ShouldRecallInput,
} from "./context-engine.js";

const DEFAULT_KEYWORDS = [
  "memclaw",
  "ltm",
  "long term",
  "long-term",
  "remember",
  "recall",
  "what did",
  "earlier",
  "previously",
  "last time",
  "before",
  "we discussed",
  "you said",
  "i told",
  "history",
  "memory",
  "lookup",
];

function input(overrides: Partial<ShouldRecallInput> = {}): ShouldRecallInput {
  return {
    policy: "auto",
    prompt: "",
    messages: [],
    minPromptChars: 14,
    triggerKeywords: DEFAULT_KEYWORDS,
    sessionKey: "tenant:agent:default",
    denySessions: [],
    ...overrides,
  };
}

describe("shouldRecall — policy=always", () => {
  test("recalls regardless of prompt", () => {
    const r = shouldRecall(input({ policy: "always", prompt: "" }));
    assert.equal(r.recall, true);
    assert.equal(r.reason, "policy-always");
  });

  test("recalls even on a trivial ping", () => {
    const r = shouldRecall(input({ policy: "always", prompt: "hi" }));
    assert.equal(r.recall, true);
  });
});

describe("shouldRecall — policy=never", () => {
  test("skips regardless of prompt", () => {
    const r = shouldRecall(input({ policy: "never", prompt: "deploy now" }));
    assert.equal(r.recall, false);
    assert.equal(r.reason, "policy-never");
  });

  test("skips even when keyword present", () => {
    const r = shouldRecall(
      input({ policy: "never", prompt: "remember the deadline?" }),
    );
    assert.equal(r.recall, false);
  });
});

describe("shouldRecall — policy=keywords", () => {
  test("recalls when explicit trigger present", () => {
    const r = shouldRecall(
      input({ policy: "keywords", prompt: "do you remember the API key?" }),
    );
    assert.equal(r.recall, true);
    assert.equal(r.reason, "explicit-recall-trigger");
  });

  test("matches MemClaw / LTM / long term keywords (case-insensitive)", () => {
    for (const p of [
      "any memclaw context here?",
      "check LTM",
      "any long term notes about this",
      "Long-Term memory needed",
    ]) {
      const r = shouldRecall(input({ policy: "keywords", prompt: p }));
      assert.equal(r.recall, true, `expected recall for: ${p}`);
    }
  });

  test("skips when no trigger present", () => {
    const r = shouldRecall(
      input({ policy: "keywords", prompt: "let's deploy this build now" }),
    );
    assert.equal(r.recall, false);
    assert.equal(r.reason, "policy-keywords-no-trigger");
  });
});

describe("_hasTriggerKeyword boundary (intentional lenient one-sided match)", () => {
  // The boundary check is deliberately lenient: it fails only when the
  // keyword is EMBEDDED inside another word (letters on BOTH sides).
  // One-sided matches (suffix like "remembered", prefix like
  // "preremember") DO trigger.
  //
  // Rationale (locked here so a future "stricter is safer" refactor
  // has to explicitly change these assertions): morphological variants
  // are common in real prompts ("remembered yesterday's deploy?",
  // "recalling the API change"). The cost of a false positive is one
  // extra /search; the cost of a false negative on a short prompt is
  // a missed recall + below-threshold skip → no LTM context.

  test("morphological variants trigger (one-sided)", () => {
    for (const p of [
      "remembered yesterday's deploy?",
      "do you recall anything?",
      "recalling the API change",
      "what i told you previously",
    ]) {
      const r = shouldRecall(input({ policy: "keywords", prompt: p }));
      assert.equal(r.recall, true, `expected trigger match for: ${p}`);
      assert.equal(r.reason, "explicit-recall-trigger");
    }
  });

  test("end-of-string is treated as non-letter (matches a trailing keyword)", () => {
    const r = shouldRecall(
      input({ policy: "keywords", prompt: "what about before" }),
    );
    assert.equal(r.recall, true);
  });

  test("start-of-string is treated as non-letter (matches a leading keyword)", () => {
    const r = shouldRecall(
      input({ policy: "keywords", prompt: "memory leak yesterday?" }),
    );
    assert.equal(r.recall, true);
  });

  test("known minor false positive: 'memorylane' triggers (acceptable)", () => {
    // Lock this so future readers don't 'fix' it without re-thinking.
    // Switching to strict word-boundary would also lose
    // "remembered" etc. above; the trade-off is documented in
    // context-engine.ts.
    const r = shouldRecall(
      input({ policy: "keywords", prompt: "down memory lane yesterday" }),
    );
    assert.equal(r.recall, true);
  });

  test("embedded substrings (letters on BOTH sides) do NOT trigger", () => {
    for (const p of [
      "preremembered everything",  // letters both sides of "remember"
      "unbeforehand",              // letters both sides of "before"
      "premembering the past",     // letters both sides of "remember"
    ]) {
      const r = shouldRecall(input({ policy: "keywords", prompt: p }));
      // Under "keywords" policy, no trigger match → skip with the
      // policy-keywords-no-trigger reason.
      assert.equal(r.recall, false, `expected NO trigger match for: ${p}`);
      assert.equal(r.reason, "policy-keywords-no-trigger");
    }
  });

  test("matches keyword after an embedded occurrence of the same keyword", () => {
    // Bug regression: pre-fix _hasTriggerKeyword used a single
    // indexOf — the FIRST hit of "remember" inside "preremembering"
    // is embedded both-sides and rejected, but the second clean
    // occurrence ("remember the deadline") should win. The loop
    // walks every occurrence.
    const r = shouldRecall(
      input({ policy: "keywords", prompt: "preremembering: remember the deadline" }),
    );
    assert.equal(r.recall, true);
    assert.equal(r.reason, "explicit-recall-trigger");
  });
});

describe("shouldRecall — policy=auto (the default)", () => {
  test("recalls a substantive prompt", () => {
    const r = shouldRecall(
      input({ prompt: "Can you summarise yesterday's deploy decision?" }),
    );
    assert.equal(r.recall, true);
    // 'yesterday' isn't a trigger; the prompt is past-threshold so
    // it falls through as substantive — but 'before' might not match.
    // Either path is acceptable here.
    assert.ok(["default-substantive", "explicit-recall-trigger"].includes(r.reason));
  });

  test("skips trivial pings: hi / hello / ok / thanks / yes / 👍", () => {
    for (const p of ["hi", "Hello", "ok", "thanks", "Yes", "👍", "🦞"]) {
      const r = shouldRecall(input({ prompt: p }));
      assert.equal(r.recall, false, `expected skip for: ${p}`);
    }
  });

  test("skips below-threshold prompts (under 14 chars)", () => {
    const r = shouldRecall(input({ prompt: "hi can you?" })); // 11 chars
    assert.equal(r.recall, false);
    assert.equal(r.reason, "below-threshold");
  });

  test("skips pure-emoji turns even when long", () => {
    const r = shouldRecall(input({ prompt: "👍👍👍🦞🦞🦞" }));
    assert.equal(r.recall, false);
    assert.equal(r.reason, "trivial-ping");
  });

  test("skips slash commands under 60 chars", () => {
    for (const p of ["/help", "/clear", "/foo bar"]) {
      const r = shouldRecall(input({ prompt: p }));
      assert.equal(r.recall, false, `expected skip for: ${p}`);
      assert.equal(r.reason, "slash-command");
    }
  });

  test("trigger keyword OVERRIDES short / trivial / slash gate", () => {
    // even a tiny "hi remember" should recall because of explicit intent
    const r = shouldRecall(input({ prompt: "hi remember?" }));
    assert.equal(r.recall, true);
    assert.equal(r.reason, "explicit-recall-trigger");
  });

  test("trigger keyword 'memclaw' fires recall on otherwise-skip prompt", () => {
    const r = shouldRecall(input({ prompt: "memclaw?" }));
    assert.equal(r.recall, true);
    assert.equal(r.reason, "explicit-recall-trigger");
  });

  test("falls back to last user message when prompt is empty", () => {
    const r = shouldRecall(
      input({
        prompt: "",
        messages: [
          { role: "user", content: "What was the deadline we picked?" },
          { role: "assistant", content: "April 30." },
        ],
      }),
    );
    // Last user message is past threshold and substantive — recall
    assert.equal(r.recall, true);
  });

  test("empty prompt + no buffered user message → below-threshold", () => {
    const r = shouldRecall(
      input({
        prompt: "",
        messages: [{ role: "assistant", content: "Done." }],
      }),
    );
    assert.equal(r.recall, false);
    assert.equal(r.reason, "below-threshold");
  });
});

describe("shouldRecall — session denylist", () => {
  test("blocks recall when session-key matches a deny entry", () => {
    const r = shouldRecall(
      input({
        prompt: "definitely a substantive prompt about the deploy",
        sessionKey: "tenant:noisy-group-abc:default",
        denySessions: ["noisy-group-abc"],
      }),
    );
    assert.equal(r.recall, false);
    assert.equal(r.reason, "session-denied");
  });

  test("denylist applies even on policy=always", () => {
    const r = shouldRecall(
      input({
        policy: "always",
        sessionKey: "tenant:lurk-channel:default",
        denySessions: ["lurk-channel"],
      }),
    );
    assert.equal(r.recall, false);
    assert.equal(r.reason, "session-denied");
  });

  test("non-matching denylist passes through", () => {
    const r = shouldRecall(
      input({
        prompt: "tell me about the API",
        sessionKey: "tenant:agent:default",
        denySessions: ["unrelated-key"],
      }),
    );
    assert.equal(r.recall, true);
  });
});

describe("getRecallMetrics", () => {
  test("counters increment on each shouldRecall caller path", () => {
    // Note: this test just exercises the export. The recordDecision call
    // happens inside assemble(), not shouldRecall — but the metrics are
    // module-state we can observe here.
    const before = getRecallMetrics();
    assert.equal(typeof before.calls_total, "number");
    assert.equal(typeof before.skipped_total, "number");
    assert.equal(typeof before.skipped_by_reason, "object");
  });
});

// ---- isDuplicateMemoryError — afterTurn 409 swallow gate (CAURA-000) ----
//
// Pins the regex that ``afterTurn`` uses to decide whether to silently
// swallow a write rejection. Misclassifying a 5xx as a 409 would hide a
// real outage from the operator log; misclassifying a 409 as a 5xx would
// flood the log with ~5/hr noise (observed pre-fix on goodclaw). The
// shape of the matched error message is pinned by ``transport.ts:82``
// (``"MemClaw API " + status + ": " + body``) — if that ever changes,
// this test fails loudly and forces the catch site to be updated too.

describe("isDuplicateMemoryError — afterTurn 409 swallow gate", () => {
  test("matches the dedup error shape thrown by transport.ts on HTTP 409", () => {
    const e = new Error(
      'MemClaw API 409: {"detail":"Duplicate memory exists: 9eea03d6-be61-456b-bf67-06ace594cf43","error":{"code":"CONFLICT","message":"Duplicate memory exists: 9eea03d6-be61-456b-bf67-06ace594cf43"}}',
    );
    assert.equal(isDuplicateMemoryError(e), true);
  });

  test("does NOT match a 500 / 502 / 4xx-other — non-409 errors must still surface", () => {
    for (const status of [400, 401, 403, 404, 422, 500, 502, 503]) {
      const e = new Error(`MemClaw API ${status}: {"detail":"x"}`);
      assert.equal(
        isDuplicateMemoryError(e),
        false,
        `status ${status} must not be classified as a 409 dedup`,
      );
    }
  });

  test("does NOT match arbitrary errors or non-Error inputs", () => {
    assert.equal(isDuplicateMemoryError(new TypeError("fetch failed")), false);
    assert.equal(isDuplicateMemoryError(new Error("something else 409")), false); // word-boundary guard: '409' alone is not enough
    assert.equal(isDuplicateMemoryError("MemClaw API 409: string"), false); // must be an Error instance
    assert.equal(isDuplicateMemoryError(null), false);
    assert.equal(isDuplicateMemoryError(undefined), false);
    assert.equal(isDuplicateMemoryError({ message: "MemClaw API 409: x" }), false);
  });

  test("matches even when the 409 message has additional context appended", () => {
    // transport.ts truncates the body to 200 chars; the ``MemClaw API 409``
    // prefix is always present at the start.
    const e = new Error('MemClaw API 409: ...truncated...');
    assert.equal(isDuplicateMemoryError(e), true);
  });
});
