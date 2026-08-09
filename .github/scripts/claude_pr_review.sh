#!/usr/bin/env bash
#
# Shared Claude PR review.
#
# Fetches a pull request's diff from the GitHub API, reviews it with Claude, and
# posts the result as a PR comment. BOTH the on-open `claude-review` job and the
# `@claude` `claude-retrigger` job call this same script, so the two paths are
# guaranteed identical — historically they diverged, and the on-open path (the
# claude-code-action agent) never actually received the diff: it tried to fetch
# it via `gh`, hit the headless permission wall, and the workflow then stamped a
# meaningless "No issues found". Feeding the diff on stdin to `claude --print`
# is the mechanism that demonstrably works.
#
# Required env:
#   REPO              owner/name
#   PR_NUMBER         pull request number
#   ANTHROPIC_API_KEY Anthropic API key
#   GH_TOKEN          token with pull-requests:write / issues:write
#   EXTRA_PROMPT      repo context, prepended to the review prompt
#   REVIEW_PROMPT     review instructions + output format
# Optional env:
#   MODEL             model id (default: claude-sonnet-5)
#   MAX_BUDGET_USD    per-review spend ceiling, passed to --max-budget-usd (default 10.00)
set -euo pipefail

MODEL="${MODEL:-claude-sonnet-5}"

# A ceiling so one runaway review cannot bill without bound. The reviewer reads the repo
# across turns to judge a diff, which is what makes it useful and also what makes an
# unbounded run possible; sibling repos on the org's shared pipeline have billed $6.11 on a
# two-file diff. The org-membership gate above stops a fork pull request from triggering a
# review at all, but it does not bound what a member's large pull request costs.
#
# A runaway guard, not a budget target. The CLI checks the ceiling BETWEEN turns, so a run
# can overshoot it by roughly one turn, and on a single hung turn it never fires at all —
# the job timeout is the only bound there. Sized above observed spend on purpose: the
# expensive reviews are the ones that find real defects, so capping near the average would
# truncate exactly the runs worth paying for.
MAX_BUDGET_USD="${MAX_BUDGET_USD:-10.00}"
# Shape, then value. Shape does not require a leading digit, so `.50` is accepted the way the
# CLI accepts it, while `.`, `1.`, `10,00`, `$10`, `-1` and `1e3` are rejected. Value is
# checked arithmetically rather than with a second pattern: a zero ceiling is accepted by the
# CLI and makes every review fail on its first turn — which reads as a broken pipeline rather
# than a bad setting — and spelling "zero" as a regex means enumerating 0, 00, 0.0, .0, 0.00
# and .00, where the no-leading-digit forms are the easy ones to miss.
if ! [[ "$MAX_BUDGET_USD" =~ ^[0-9]*\.?[0-9]+$ ]] \
   || ! awk -v v="$MAX_BUDGET_USD" 'BEGIN { exit !(v + 0 > 0) }'; then
  echo "::error::MAX_BUDGET_USD must be a positive decimal number, got '${MAX_BUDGET_USD}'" >&2
  exit 1
fi

post() { gh api "repos/${REPO}/issues/${PR_NUMBER}/comments" -f body="$1" >/dev/null; }

DIFF=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}" -H "Accept: application/vnd.github.diff")
if [ -z "$DIFF" ]; then
  echo "::notice::Empty diff for PR #${PR_NUMBER} — nothing to review"
  exit 0
fi

PROMPT="${EXTRA_PROMPT}
${REVIEW_PROMPT}

Review the PR diff provided on stdin. Review ONLY the changed lines. If after a careful review you find no real issues, reply with exactly this single line and nothing else:
**Claude Code Review** :white_check_mark: No issues found."

# No 2>&1: claude's stderr (warnings/progress) must not contaminate the JSON on
# stdout, or jq would parse garbage and yield an empty review. A non-zero exit is
# still caught below; stderr goes to the workflow log for debugging.
# --tools is the security half, and it matters more here than anywhere: this repo is PUBLIC.
# The threat is concrete — a prompt injection in the diff reaching a CLI whose environment holds
# GH_TOKEN with pull-requests:write. Restricting the reviewer to read-only tools leaves the
# injection nothing to act through. Reviewing a diff needs Read, Grep and Glob and nothing else.
#
# --bare closes two paths the allow-list provably does not reach, so it is load-bearing rather
# than belt-and-braces. --tools names tools from the BUILT-IN set, so it does nothing about
# MCP-provided ones: a pull request adding an .mcp.json would otherwise hand the reviewer tools
# outside the allow-list entirely. --bare also skips CLAUDE.md auto-discovery, so a reviewed
# tree's own CLAUDE.md cannot quietly steer the review of that same tree.
#
# Both flags exist in the pinned 2.1.159 installed above. Verify them again when bumping the pin
# — a build that dropped either would review in a writable mode without saying so.
RESULT=$(printf '%s' "$DIFF" | claude --print --model "$MODEL" --output-format json \
  --bare \
  --tools "Read,Grep,Glob" \
  --max-budget-usd "$MAX_BUDGET_USD" \
  "$PROMPT") || {
  CLAUDE_EXIT=$?
  # `VAR=$(cmd)` keeps cmd's stdout even when cmd fails, and claude reports several failures
  # (auth, quota, and budget exhaustion) there rather than on stderr. This branch used to
  # discard it and post "check workflow logs" above a log that held nothing — which would have
  # made a ceiling hit the least diagnosable outcome in the script.
  echo "Claude exited ${CLAUDE_EXIT}. First 2000 chars of its stdout:" >&2
  printf '%s\n' "${RESULT:0:2000}" >&2
  # Budget exhaustion is an EXPECTED outcome carrying a machine-readable marker, and it exits
  # 1 exactly like a crash, so without this branch the ceiling would surface as a bare exit
  # code and read as a broken pipeline. `jq -e` rather than `grep -q`: grep stops reading on
  # match, the upstream printf takes SIGPIPE, and pipefail then turns a MATCH into a non-zero
  # pipeline. jq drains stdin.
  if printf '%s' "$RESULT" | jq -e '.subtype == "error_max_budget_usd"' >/dev/null 2>&1; then
    SPENT=$(printf '%s' "$RESULT" | jq -r '.total_cost_usd // "unknown"' 2>/dev/null || echo unknown)
    post "⚠️ Claude Code review reached the \$${MAX_BUDGET_USD} spend ceiling after \$${SPENT} without finishing. Split the PR, or raise \`MAX_BUDGET_USD\` on the workflow step."
    exit 1
  fi
  post "⚠️ Claude Code review failed: exit ${CLAUDE_EXIT} (see workflow logs)."
  exit 1
}

# jq runs under `set -e`; a parse failure (claude returned non-JSON — a warning
# banner, rate-limit HTML) must not silently kill the script before we post an
# error. On failure, log the raw response so the workflow log actually has
# something to check, then post a visible error.
REVIEW=$(printf '%s' "$RESULT" | jq -r '.result // empty' 2>/dev/null) || {
  echo "Claude returned non-JSON output. First 2000 chars of the raw response:" >&2
  printf '%s\n' "${RESULT:0:2000}" >&2
  post "⚠️ Claude Code review failed: response was not valid JSON (see workflow logs)."
  exit 1
}

# Bail before logging any cost, so an empty .result can't leave a cost table in
# the job summary next to a "review failed" comment.
if [ -z "$REVIEW" ]; then
  post "⚠️ Claude Code review failed: empty result."
  exit 1
fi

COST=$(printf '%s' "$RESULT" | jq -r '.total_cost_usd // empty' 2>/dev/null || echo "unknown")
echo "::notice::Claude review cost: \$${COST:-unknown} (model ${MODEL}, PR #${PR_NUMBER})"

# Cost + token table in the Actions job summary (when running in a workflow).
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ -n "$COST" ] && [ "$COST" != "unknown" ]; then
  TOKENS_IN=$(printf '%s' "$RESULT" | jq -r '.usage.input_tokens // empty' 2>/dev/null || true)
  TOKENS_OUT=$(printf '%s' "$RESULT" | jq -r '.usage.output_tokens // empty' 2>/dev/null || true)
  {
    echo "### Claude Code Review Cost"
    echo "| Metric | Value |"
    echo "|--------|-------|"
    echo "| Cost | \$${COST} |"
    echo "| Input tokens | ${TOKENS_IN:-?} |"
    echo "| Output tokens | ${TOKENS_OUT:-?} |"
  } >> "$GITHUB_STEP_SUMMARY"
fi

# GitHub caps comment bodies at 65536 chars; truncate so a very large review
# can't 422 and then silently fail under set -e.
MAX_BODY=65000
if [ "${#REVIEW}" -gt "$MAX_BODY" ]; then
  REVIEW="${REVIEW:0:$MAX_BODY}

_[Review truncated — exceeded GitHub's comment size limit.]_"
fi

# Footer surfaces per-review spend on the PR itself, not just the job log.
post "${REVIEW}

---
*Reviewed by \`${MODEL}\` · cost \$${COST:-unknown}*"
