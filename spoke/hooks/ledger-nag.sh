#!/bin/bash
# Stop hook: nag when a session committed code but recorded nothing in the
# ledger.
#
# A commit is exactly the moment a decision, a deviation, or an open item
# is freshest in a session's mind -- and exactly the moment it is easiest
# to leave unrecorded, because the code itself feels like the record. This
# hook does not block (a nag that could stop a session from ending would
# itself be the kind of silent-failure risk this project exists to
# remove) -- it only reminds, once, in the same systemMessage channel
# SessionStart uses.
#
# Follows the shape of memory-symlink-guard.sh: read the hook payload from
# stdin with jq, emit {"systemMessage": ...} built with jq -Rn, and NEVER
# exit non-zero.
#
# Real detection (no fake env vars set) is a best-effort git inspection:
# commits in the working repo since the transcript file's birth time, and
# commits touching ledger/ in the project's memory store over the same
# window. It intentionally has no test coverage of its own -- every test
# in tests/test_hooks.py drives this script through the
# SPOKE_FAKE_COMMITS / SPOKE_FAKE_LEDGER_CHANGES seam below instead, so
# no test ever depends on real git history. Treat the git path as a
# reasonable heuristic, not a proven one.

set -uo pipefail

say() { printf '{"systemMessage":%s}\n' "$(jq -Rn --arg s "$1" '$s')"; }

input=$(cat)
transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)

# Test seam: with both fakes set, skip real git inspection entirely so
# tests never depend on (or risk mutating) any real repository.
if [ -n "${SPOKE_FAKE_COMMITS+x}" ] && [ -n "${SPOKE_FAKE_LEDGER_CHANGES+x}" ]; then
  commits="$SPOKE_FAKE_COMMITS"
  ledger_changes="$SPOKE_FAKE_LEDGER_CHANGES"
else
  commits=0
  ledger_changes=0

  if command -v git >/dev/null 2>&1; then
    repo_root=$(git rev-parse --show-toplevel 2>/dev/null || true)
    if [ -n "$repo_root" ]; then
      since_epoch=""
      if [ -n "$transcript" ] && [ -e "$transcript" ]; then
        # Birth time where the platform supports it (macOS: stat -f %B;
        # Linux: stat -c %W, which may report 0 when unknown) -- fall
        # back to mtime rather than guessing a window with no anchor.
        since_epoch=$(stat -f %B "$transcript" 2>/dev/null || stat -c %W "$transcript" 2>/dev/null || true)
        if [ -z "$since_epoch" ] || [ "$since_epoch" = "0" ] || [ "$since_epoch" = "-1" ]; then
          since_epoch=$(stat -f %m "$transcript" 2>/dev/null || stat -c %Y "$transcript" 2>/dev/null || true)
        fi
      fi

      if [ -n "$since_epoch" ]; then
        commits=$(git -C "$repo_root" log --since="@$since_epoch" --oneline 2>/dev/null | wc -l | tr -d ' ')

        # ledger/ lives in the project's memory store, not necessarily in
        # this working repo -- derive that store the same way
        # memory-symlink-guard.sh derives it, from the transcript path.
        if [ -n "$transcript" ]; then
          projdir=$(dirname "$transcript")
          memdir="$projdir/memory"
          if git -C "$memdir" rev-parse --git-dir >/dev/null 2>&1; then
            ledger_changes=$(git -C "$memdir" log --since="@$since_epoch" --oneline -- ledger 2>/dev/null | wc -l | tr -d ' ')
          fi
        fi
      fi
    fi
  fi
fi

# Silent, deliberately: no commits this session means nothing to nag
# about, and printing nothing (not even an empty systemMessage) is what
# "found nothing" looks like for this hook.
if [ -z "$commits" ] || [ "$commits" = "0" ]; then
  exit 0
fi

# The ledger moved too -- also silent, same reasoning.
if [ -n "$ledger_changes" ] && [ "$ledger_changes" != "0" ]; then
  exit 0
fi

say "ledger: this session committed ($commits commit(s)) but nothing moved under ledger/. If anything here should reach a later session, record it now -- \`spoke ledger new\` -- before it ends up findable only by accident."
exit 0
