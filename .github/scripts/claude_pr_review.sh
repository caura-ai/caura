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
#   MODEL             model id (default: claude-sonnet-4-6)
set -euo pipefail

MODEL="${MODEL:-claude-sonnet-4-6}"

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
RESULT=$(printf '%s' "$DIFF" | claude --print --model "$MODEL" --output-format json "$PROMPT") || {
  post "⚠️ Claude Code review failed (check workflow logs)."
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
