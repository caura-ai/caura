import { test } from "node:test";
import assert from "node:assert/strict";

import { _resetForTests, recordTurn, sessionHeader, takeOrigin, usageOf } from "./origin-usage.js";

test("usage is read in every common provider spelling", () => {
  assert.deepEqual(usageOf({ usage: { input: 100, output: 20, cacheRead: 50 } }), { tokensIn: 150, tokensOut: 20 });
  assert.deepEqual(usageOf({ usage: { input_tokens: 10, output_tokens: 5 } }), { tokensIn: 10, tokensOut: 5 });
  assert.deepEqual(usageOf({ usage: { prompt_tokens: 7, completion_tokens: 3 } }), { tokensIn: 7, tokensOut: 3 });
  assert.equal(usageOf({ content: "no usage" }), null);
  assert.equal(usageOf(null), null);
});

test("a write carries the turns and tokens since the session's previous write, then starts again", () => {
  _resetForTests();
  const m1 = { role: "assistant", timestamp: 1, usage: { input: 1000, output: 100 } };
  const m2 = { role: "assistant", timestamp: 2, usage: { input: 2000, output: 200 } };
  recordTurn("s1", [{ role: "user" }, m1]);
  // The next turn hands over the whole history: m1 must not count twice.
  recordTurn("s1", [{ role: "user" }, m1, { role: "user" }, m2]);
  assert.deepEqual(takeOrigin("s1"), { turns: 2, source: "plugin", tokens_in: 3000, tokens_out: 300 });
  assert.equal(takeOrigin("s1"), undefined);
});

test("without timestamps only the turn's last assistant message counts", () => {
  _resetForTests();
  const a = { role: "assistant", usage: { input: 10, output: 1 } };
  const b = { role: "assistant", usage: { input: 20, output: 2 } };
  recordTurn("s2", [a, b]);
  assert.deepEqual(takeOrigin("s2"), { turns: 1, source: "plugin", tokens_in: 20, tokens_out: 2 });
});

test("a session with turns but no reported usage sends turns only, never a guess", () => {
  _resetForTests();
  recordTurn("s3", [{ role: "assistant", content: "hi" }]);
  assert.deepEqual(takeOrigin("s3"), { turns: 1, source: "plugin" });
});

test("sessions are kept apart, and an unknown session sends nothing", () => {
  _resetForTests();
  recordTurn("a", [{ role: "assistant", timestamp: 1, usage: { input: 5, output: 1 } }]);
  assert.equal(takeOrigin("b"), undefined);
  assert.equal(takeOrigin(undefined), undefined);
  assert.equal(takeOrigin("a")?.tokens_in, 5);
});

test("the session header is safe for core-api", () => {
  assert.deepEqual(sessionHeader("agent:main:telegram:123"), { "X-Caura-Session-Id": "openclaw:agent:main:telegram:123" });
  assert.equal(sessionHeader("weird key/with spaces")?.["X-Caura-Session-Id"], "openclaw:weird_key_with_spaces");
  assert.equal(sessionHeader(undefined), undefined);
});
