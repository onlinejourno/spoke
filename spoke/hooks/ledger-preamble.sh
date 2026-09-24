#!/bin/bash
# SessionStart hook: surface the ledger's preamble at the top of a new
# session, unasked.
#
# Storage is the easy part of this project; being RAISED to a session that
# never asked for it is the half that matters. In one real run a parallel
# session recorded three open items "for the go-live session" with
# nothing to put them in front of it -- they were found by accident, days
# later, running `git log` for something unrelated. This hook is the fix:
# every SessionStart runs `spoke ledger preamble` and shows whatever
# it prints, so an open item cannot silently wait for a session that never
# comes looking.
#
# Follows the shape of memory-symlink-guard.sh: read the hook payload from
# stdin with jq, emit {"systemMessage": ...} built with jq -Rn, and NEVER
# exit non-zero -- a broken hook must not block a session. But "ran, found
# nothing" and "is broken" must never look the same (the whole point of
# this project), so a missing binary or a failing command says so visibly
# instead of staying quiet.

set -uo pipefail

say() { printf '{"systemMessage":%s}\n' "$(jq -Rn --arg s "$1" '$s')"; }

input=$(cat)
transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)

if [ -z "$transcript" ]; then
  say "ledger-preamble: no transcript_path in hook input; cannot locate this project. Ledger NOT shown this session."
  exit 0
fi

# Same derivation memory-symlink-guard.sh uses: the project directory is
# the transcript's containing directory, not a re-implementation of
# Claude's path-slugging scheme.
projdir=$(dirname "$transcript")

# Test seam: SPOKE_BIN lets tests point at a throwaway stub instead of a
# real install, so a test can never reach the founder's real registry or
# real memory store.
BIN="${SPOKE_BIN:-spoke}"

if ! command -v "$BIN" >/dev/null 2>&1; then
  say "ledger-preamble: spoke binary not found (looked for '$BIN'). Ledger NOT shown this session -- install it or set SPOKE_BIN."
  exit 0
fi

# `--project` on `ledger preamble` takes a registered project NAME, not a
# directory -- `projects which` is the lookup that turns the directory this
# hook derived above into that name. A directory nothing owns is a normal
# situation (an unregistered repo, a scratch checkout), not a broken hook,
# but it must say so plainly rather than silently rendering an empty
# preamble that looks identical to "genuinely zero open items".
project=$("$BIN" projects which "$projdir" 2>&1)
prc=$?

if [ $prc -ne 0 ]; then
  say "ledger-preamble: no registered project owns $projdir. Ledger NOT shown this session -- this directory is not registered (run \`spoke projects add\` to register it), not an empty ledger."
  exit 0
fi

# --ledger-only: Claude Code already injects MEMORY.md into this session
# at SessionStart and this hook cannot prevent that, so passing memory
# index lines into the preamble's budget here would duplicate content the
# session already has -- in practice thousands of duplicated tokens for a
# handful of lines of genuinely new information. See
# `spoke ledger preamble --help` for the full reasoning.
output=$("$BIN" ledger preamble --project "$project" --ledger-only 2>&1)
rc=$?

if [ $rc -ne 0 ]; then
  say "ledger-preamble: spoke exited $rc. Ledger NOT shown this session. Output: $output"
  exit 0
fi

if [ -z "$output" ]; then
  say "ledger-preamble: spoke produced no output. Ledger NOT shown this session."
  exit 0
fi

say "$output"
exit 0
