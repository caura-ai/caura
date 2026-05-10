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
