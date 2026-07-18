#!/usr/bin/env bash
# Interviewer Phase 1 — wet-test orchestrator (task #6).
#
# Runs the crash/idempotency matrix against a live backend using the
# real plugin modules (see interviewer-wet.mjs). Expects to run from the
# plugin/ directory with dist/ built and the backend up on
# $MEMCLAW_API_URL. Exits non-zero on the first failed assertion.
set -u
# pipefail so H()'s `node ... | tail -1` propagates node's exit code —
# without it the P0 `|| exit` guards never fire (tail always exits 0) and
# a dead backend surfaces as confusing downstream assertion mismatches.
set -o pipefail

: "${MEMCLAW_API_URL:=http://localhost:8000}"
: "${MEMCLAW_API_KEY:?set MEMCLAW_API_KEY (admin key)}"
: "${MEMCLAW_TENANT_ID:=t-wet}"
: "${MEMCLAW_FLEET_ID:=wet-fleet}"
: "${MEMCLAW_NODE_NAME:=wet-node-1}"
export MEMCLAW_API_URL MEMCLAW_API_KEY MEMCLAW_TENANT_ID MEMCLAW_FLEET_ID MEMCLAW_NODE_NAME
export MEMCLAW_INTERVIEWER=true

# The plugin modules log progress lines to stdout; the harness's JSON is
# always the LAST line — capture just that.
H() { node e2e/interviewer-wet.mjs "$@" | tail -1; }
BUF="$HOME/.openclaw/plugins/memclaw/interview-buffer.jsonl"
PASS=0; FAIL=0

say()  { echo ">>> $*"; }
ok()   { PASS=$((PASS+1)); echo "  PASS: $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $*"; }
need() { # need <jq-expr> <expected> <json> <label>
  local got
  got=$(echo "$3" | jq -r "$1")
  if [ "$got" = "$2" ]; then ok "$4 ($1=$got)"; else bad "$4 — expected $1=$2, got $got  [$3]"; fi
}

say "P0 setup: enable tenant + register node"
H set-enabled true >/dev/null || { echo "setup failed"; exit 1; }
H heartbeat >/dev/null || { echo "heartbeat failed"; exit 1; }
NODE_UUID=$(H node-uuid | jq -r .node_uuid)
say "node uuid: $NODE_UUID"

say "P1 happy path: append 12 -> schedule -> heartbeat -> committed + pruned"
R=$(H append 12 p1); need '.appended' 12 "$R" "12 events appended"
R=$(H schedule);      need '.commands_queued >= 1' true "$R" "schedule queued a command"
R=$(H heartbeat);     need '.ok' true "$R" "heartbeat processed the command"
R=$(H commands);      need '.latest.status' done "$R" "command reported done"
                       need '.latest.result.submitted' true "$R" "window submitted"
W1=$(echo "$R" | jq -r '.latest.result.watermark // empty'); [ -n "$W1" ] || { echo "ABORT: no watermark after P1"; exit 1; }
say "committed watermark: $W1"
R=$(H memories);      need '.interviewer_memories >= 1' true "$R" "interviewer memories written"
M1=$(echo "$R" | jq -r '.interviewer_memories')
R=$(H count);         need '.count' 0 "$R" "buffer pruned through the watermark"
R=$(H schedule);      need '.commands_queued' 0 "$R" "not due again immediately (dueness gate)"

say "P2 torn line: corrupt tail -> restart-append -> seq continues, no dupes"
R=$(H append 3 p2)
printf '{"seq":9999,"ts":"2026-07-17T' >> "$BUF"   # simulated crash mid-append
R=$(H append 2 p2b)
R=$(H count)
need '.count' 5 "$R" "5 valid events (torn line skipped)"
need '.strictly_monotonic' true "$R" "seq strictly monotonic across the torn line"

say "P2b recovery interview over the post-crash window"
SINCE=$((W1 + 1))
R=$(H queue-cmd "$SINCE"); need '.ok' true "$R" "recovery command queued"
R=$(H heartbeat);          need '.ok' true "$R" "recovery heartbeat"
R=$(H commands);           need '.latest.result.submitted' true "$R" "recovery window submitted"
W2=$(H commands | jq -r '.latest.result.watermark // empty'); [ -n "$W2" ] || { echo "ABORT: no watermark after P2b"; exit 1; }
R=$(H count);              need '.count' 0 "$R" "buffer pruned after recovery"

say "P3 no-prune invariant: server refuses (tenant disabled) -> buffer intact -> recovers"
R=$(H append 5 p3)
H set-enabled false >/dev/null || { echo "ABORT: disable PUT failed"; exit 1; }
# Verify the gate actually flipped before queueing (guards against a
# swallowed PUT error or a settings-cache lag masking the invariant).
for i in $(seq 1 10); do
  EN=$(H get-enabled | jq -r .enabled)
  [ "$EN" = "false" ] && break
  sleep 1
done
[ "$EN" = "false" ] || { echo "ABORT: tenant still enabled after disable"; exit 1; }
SINCE=$((W2 + 1))
H queue-cmd "$SINCE" >/dev/null
R=$(H heartbeat)          # command fails server-side (403)
R=$(H commands);           need '.latest.status' failed "$R" "command reported failed on refusal"
R=$(H count);              need '.count' 5 "$R" "buffer NOT pruned on failed submit"
H set-enabled true >/dev/null || { echo "ABORT: enable PUT failed"; exit 1; }
# core-api runs 2 uvicorn workers with per-worker settings caches (5-min
# TTL); a PUT invalidates only its own worker. Production recovers via
# the scheduler retrying next tick — mirror that: retry until the gate
# is seen open (bounded).
SUBMITTED=false
for i in $(seq 1 8); do
  H queue-cmd "$SINCE" >/dev/null
  H heartbeat >/dev/null
  SUBMITTED=$(H commands | jq -r '.latest.result.submitted // false')
  [ "$SUBMITTED" = "true" ] && break
  sleep 2
done
R=$(H commands);           need '.latest.result.submitted' true "$R" "retry submitted after re-enable (eventual, per-worker cache)"
W3=$(echo "$R" | jq -r '.latest.result.watermark // empty'); [ -n "$W3" ] || { echo "ABORT: no watermark after P3"; exit 1; }
R=$(H count);              need '.count' 0 "$R" "buffer pruned after recovery"

say "P4 kill -9 durability: hard-kill mid-append, then verify + continue"
node e2e/interviewer-wet.mjs append-loop > /tmp/killer.log 2>&1 &
KPID=$!
sleep 3
kill -9 "$KPID" 2>/dev/null
wait "$KPID" 2>/dev/null
K=$(wc -l < /tmp/killer.log | tr -d ' ')
say "killer appended ~$K events before SIGKILL"
R=$(H count)
need '.strictly_monotonic' true "$R" "monotonic after SIGKILL"
C_AFTER_KILL=$(echo "$R" | jq -r '.count')
if [ "$C_AFTER_KILL" -ge $((K - 1)) ]; then ok "at most one (torn) event lost ($C_AFTER_KILL of ~$K)"; else bad "lost more than the torn tail: $C_AFTER_KILL of $K"; fi
R=$(H append 2 p4); need '.appended' 2 "$R" "appends continue after SIGKILL"

say "P5 backlog drain: 600 events -> 500-cap submit -> second window drains the rest"
SINCE=$((W3 + 1))
H append 600 p5 >/dev/null
H queue-cmd "$SINCE" >/dev/null
H heartbeat >/dev/null
R=$(H commands);           need '.latest.result.events' 500 "$R" "first window capped at 500"
W4=$(echo "$R" | jq -r '.latest.result.watermark // empty'); [ -n "$W4" ] || { echo "ABORT: no watermark after P5a"; exit 1; }
H queue-cmd "$((W4 + 1))" >/dev/null
H heartbeat >/dev/null
R=$(H commands)
LEFT=$(echo "$R" | jq -r '.latest.result.events')
say "second window carried $LEFT events"
R=$(H count);              need '.count' 0 "$R" "backlog fully drained"

say "P6 duplicate command: second command over a consumed window is a no-op"
M_BEFORE=$(H memories | jq -r '.interviewer_memories')
W5=$(H commands | jq -r '.latest.result.watermark // empty'); [ -n "$W5" ] || { echo "ABORT: no watermark before P6"; exit 1; }
H queue-cmd "$((W5 + 1))" >/dev/null
H heartbeat >/dev/null
R=$(H commands);           need '.latest.result.submitted' false "$R" "empty window -> submitted:false"
M_AFTER=$(H memories | jq -r '.interviewer_memories')
if [ "$M_AFTER" = "$M_BEFORE" ]; then ok "no duplicate memories ($M_AFTER)"; else bad "memory count changed $M_BEFORE -> $M_AFTER"; fi

echo
echo "==================================================="
echo "WET TEST RESULT: PASS=$PASS FAIL=$FAIL"
echo "==================================================="
[ "$FAIL" -eq 0 ]
