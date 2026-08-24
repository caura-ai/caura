#!/usr/bin/env bash
# Interviewer Phase 1.5 — capture-layer wet test (#654 / PR #658).
#
# The Phase-1 suite (interviewer-wet.sh) hand-feeds the buffer and is
# therefore a PIPELINE regression. This suite exercises the CAPTURE
# layer: events must originate from OpenClaw's real task_runs SQLite
# (created by a real `openclaw` gateway; rows from real system-event
# cron runs where possible, schema-introspecting inserts for edge
# shapes). No appendInterviewEvent calls anywhere in this file.
#
# Expects: plugin/ cwd with dist/ built; backend up on CAURA_API_URL;
# a real ~/.openclaw created by the installed OpenClaw (legacy layout
# phase A; run again post-upgrade for phase B). TASK_DB points at the
# discovered task DB for insert helpers.
set -u
set -o pipefail

: "${CAURA_API_URL:=http://localhost:8000}"
: "${CAURA_API_KEY:?set CAURA_API_KEY (admin key)}"
: "${CAURA_TENANT_ID:=t-wet-capture}"
: "${CAURA_FLEET_ID:=wet-fleet}"
: "${CAURA_NODE_NAME:=wet-node-capture}"
: "${TASK_DB:=$HOME/.openclaw/tasks/runs.sqlite}"
export CAURA_API_URL CAURA_API_KEY CAURA_TENANT_ID CAURA_FLEET_ID CAURA_NODE_NAME
export CAURA_INTERVIEWER=true
unset CAURA_INTERVIEWER_TASKS CAURA_TASK_DB_PATH || true

H() { node e2e/interviewer-wet.mjs "$@" | tail -1; }
PASS=0; FAIL=0
say()  { echo ">>> $*"; }
ok()   { PASS=$((PASS+1)); echo "  PASS: $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $*"; }
need() { # need <jq-expr> <expected> <json> <label>
  local got
  got=$(echo "$3" | jq -r "$1")
  if [ "$got" = "$2" ]; then ok "$4 ($1=$got)"; else bad "$4 — expected $1=$2, got $got  [$3]"; fi
}

say "A0 setup: enable tenant + register node; task DB = $TASK_DB"
H set-enabled true >/dev/null || { echo "setup failed"; exit 1; }
H heartbeat >/dev/null || { echo "heartbeat failed"; exit 1; }
R=$(H task-db-schema "$TASK_DB"); need '.ok' true "$R" "real task DB readable"
echo "  real columns: $(echo "$R" | jq -c .columns)"

say "A1 discovery: sync sees the real DB, no hand-fed events anywhere"
R=$(H sync)
need '.db_paths | length >= 1' true "$R" "discovery found >=1 task DB"
echo "  discovered: $(echo "$R" | jq -c .db_paths)"
A1_SYNCED=$(echo "$R" | jq -r .synced)
say "A1 synced $A1_SYNCED pre-existing real task events"

say "A2 real capture -> interview: fresh terminal tasks -> tick -> memories"
R=$(H task-insert "$TASK_DB" 3 phaseA terminal); need '.inserted' 3 "$R" "3 terminal tasks in the REAL DB"
R=$(H tick 0)
need '.status' done "$R" "tick done"
need '.result.submitted' true "$R" "window submitted"
need '.result.synced_tasks >= 3' true "$R" "synced_tasks counted the new tasks"
W=$(echo "$R" | jq -r '.result.watermark // empty'); [ -n "$W" ] || { echo "ABORT: no watermark"; exit 1; }
R=$(H memories); need '.interviewer_memories >= 1' true "$R" "interviewer memories written from task events"

say "A3 delta: second tick is a genuine no-op"
R=$(H tick $((W + 1)))
need '.result.submitted' false "$R" "nothing new submitted"
need '.result.synced_tasks' 0 "$R" "no re-emission (sidecar delta)"
need '.result.reason' "no new events since cursor" "$R" "idle reason"

say "A4 terminal transition: running -> completed emits the result half"
R=$(H task-insert "$TASK_DB" 1 phaseA4); TID=$(echo "$R" | jq -r .first_id)
R=$(H tick $((W + 1)))
need '.result.synced_tasks' 1 "$R" "discovered event synced"
W=$(echo "$R" | jq -r '.result.watermark // empty')
H task-finish "$TASK_DB" "$TID" "phaseA4 finished: verified terminal transition capture" ok >/dev/null
R=$(H tick $((W + 1)))
need '.result.synced_tasks' 1 "$R" "terminal event synced on transition"
W=$(echo "$R" | jq -r '.result.watermark // empty')

say "D1 observability: task capture disabled is a distinct note"
R=$(CAURA_INTERVIEWER_TASKS=false H tick $((W + 1)))
need '.result.synced_tasks' 0 "$R" "no sync when disabled"
need '.result.task_trail' "task capture disabled (CAURA_INTERVIEWER_TASKS=false)" "$R" "disabled note"

say "D2 observability: broken CAURA_TASK_DB_PATH names the path"
R=$(CAURA_TASK_DB_PATH=/nonexistent/tasks.sqlite H tick $((W + 1)))
need '.result.synced_tasks' 0 "$R" "no sync on broken override"
need '.result.task_trail | contains("/nonexistent/tasks.sqlite")' true "$R" "note names the misconfigured path"

say "D3 cap: 220-task burst -> capped tick -> remainder drains"
R=$(H task-insert "$TASK_DB" 220 phaseD3); need '.inserted' 220 "$R" "220 tasks inserted"
R=$(H tick $((W + 1)))
need '.result.synced_tasks' 200 "$R" "first tick capped at 200"
need '.result.task_trail | contains("capped")' true "$R" "capped note present"
W=$(echo "$R" | jq -r '.result.watermark // empty')
R=$(H tick $((W + 1)))
need '.result.synced_tasks' 20 "$R" "remainder drained next tick"
W=$(echo "$R" | jq -r '.result.watermark // empty')

echo
echo "capture-suite: PASS=$PASS FAIL=$FAIL (watermark=$W)"
[ "$FAIL" -eq 0 ] || exit 1
