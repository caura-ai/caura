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

REVIEW=$(printf '%s' "$RESULT" | jq -r '.result // empty')
COST=$(printf '%s' "$RESULT" | jq -r '.total_cost_usd // empty')
echo "::notice::Claude review cost: \$${COST:-unknown} (model ${MODEL}, PR #${PR_NUMBER})"

if [ -z "$REVIEW" ]; then
  post "⚠️ Claude Code review failed: empty result."
  exit 1
fi

# Footer surfaces per-review spend on the PR itself, not just the job log.
post "${REVIEW}

---
*Reviewed by \`${MODEL}\` · cost \$${COST:-unknown}*"
