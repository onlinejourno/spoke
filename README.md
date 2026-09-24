# Spoke

**A hub-and-spoke map of what was decided, deferred, blocked or found
across your projects — kept beside a Claude memory store, and put in
front of the next session without anyone remembering to look.**

A decision made in one session is lost to the next unless somebody
writes it down somewhere the next session reads. Spoke is that
somewhere: a ledger of nodes with rulings, a scan that derives the
products and decisions your repos already contain, flags computed at
render for what is blocked, held, stale or unruled, and three surfaces
— a board, a capability scale, and a preamble the session-start hook
prints — that raise it unasked. Anything you find wrong on a page can
be reported from that page; it becomes a node the next session is
handed.

It also keeps the memory store itself honest: structural and freshness
checks over the `MEMORY.md` index and the records it points at —
missing frontmatter, dead links, index drift, stale claims — and, with
a language model of your choosing, likely contradictions between them.

MIT. No organisation's name ships in the package; yours goes on it from
the settings screen.

## See it in a minute

```bash
spoke demo ~/spoke-demo
```

That clones two small MIT tools by the same author,
[Tare](https://github.com/onlinejourno/tare) and
[Forage](https://github.com/onlinejourno/forage), builds a scratch store
and registry under `~/spoke-demo`, scans them, and lays on top one of
each thing a person records: a hold and what it reaches, a block and its
chain, a deferral with its ruling, a decision superseded and saying by
what, a note gone stale, a capability one tool has and the other lacks,
a former name that still resolves, a problem reported from a page, and
scores on three axes with every basis -- one composite issued, one
withheld, each saying why. It prints the `serve` command to open it.
Nothing outside that directory is touched.

## Install

The first minute is on the page, not in a file. `spoke serve` starts
with no config at all and every page sends you to `/setup`: point it at
a memory store (it lists the ones already on the machine), register a
project by name and repo paths, put the first nodes on the map with a
scan or a report, declare the axes. Each step is done when it is true
-- including one done from the terminal.

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

### Keep the app running

```bash
.venv/bin/spoke serve --install --port 8770
```

A launchd agent with `KeepAlive`, so the app survives a crash and a
reboot. `--status` asks whether it is *answering*, not whether launchd
loaded it, and checks that the thing answering is actually Spoke — ports
collide, and a status that cannot tell who replied verifies nothing.

```bash
.venv/bin/spoke serve --status --port 8770
.venv/bin/spoke serve --uninstall
```

Pick a port nothing else uses. The default is 8765.

### Use it from an AI agent

Spoke speaks MCP, so an agent gets the same ledger it does — including
the same refusals. One command registers it:

```bash
.venv/bin/spoke mcp --install
```

That drives your client's own `mcp add`; Spoke never edits the client's
config file itself. Add `--dry-run` to see the exact registration and
change nothing, `--scope project` to keep it to this repo, or
`--project <name>` when several projects are registered.

```bash
.venv/bin/spoke mcp --status      # registered AND does it actually start?
.venv/bin/spoke mcp --uninstall
```

`--status` asks the client to start the server, not merely whether a line
exists in a config file. "Registered" and "running" are different facts,
and only one of them is worth anything.

### Being raised, unasked

Storage is the easy half. The half that matters is being *raised* to a
session that never asked -- an open item that waits for a session to come
looking is an open item nobody sees. Two hooks do that for Claude Code:
`SessionStart` prints the ledger's preamble at the top of every new
session, and `Stop` nags when commits happened and the ledger did not
move.

```bash
.venv/bin/spoke hooks --install     # registers both in ~/.claude/settings.json
.venv/bin/spoke hooks --status      # registered AND able to run?
.venv/bin/spoke hooks --uninstall   # removes only these two, leaves your others
```

Both scripts parse their input with `jq`, so `--status` goes red without
it and says so -- a hook that cannot run reports nothing, and silence
looks exactly like "nothing to report". `--settings <path>` targets a
different file; `--dry-run` prints what the file would become. Other
hooks already in the file are left exactly as they were.

## Expectations and checklists

Two things a node can carry that keep it raised until the world, or a
person, says otherwise.

An **expectation** is what a node says should be observably true -- a
URL answers, carries some text, was modified within some window:

```bash
spoke ledger expect the-nightly-brief --url https://example.org/brief --contains "Brief" --fresh-within 36h
spoke check --probe            # probe every expectation now
spoke doctor                   # probes them on its schedule, and pushes NEW findings to [notify]
```

The probe writes what it found back onto the node; the board's `unmet`
flag and the preamble line are read from that. An expectation nobody
has checked is unmet, not met. Freshness is judged by the response's
`Last-Modified` header, and a page that carries none is reported as
exactly that -- the fix is to make the thing expose its own freshness.

A **checklist** is the task ledger's four questions, or any list, kept
on the node. It cannot be closed while an item is open:

```bash
spoke ledger new fix-the-thing --type item --state open --title "…" --body "…" --questions
spoke ledger tick fix-the-thing 3 --note "loaded the page, saw the chart"
```

A problem reported from a page carries the four questions from the
start. Every step in the first-run flow does too, where it is a task.

## Configuration

Copy `config.example.toml` to `config.toml` and set `[store].path` to the
memory directory you want checked, e.g.
`~/.claude/projects/<slug>/memory`. There is no default store path — a
baked-in path would silently point one project's checks at another
project's memories.

`SPOKE_STORE_PATH` overrides `[store].path` from the environment.

### Notification

`doctor` can push a notification via [ntfy](https://ntfy.sh) when a run
finds defects that were not present on the previous run. Set
`[notify].ntfy_topic` in `config.toml` (or `SPOKE_NTFY_TOPIC`) to a topic
name of your choosing -- there is no default topic, so notification is off
until you set one, and `doctor` prints `notify: not configured` on every
run until you do. `[notify].ntfy_server` (or `SPOKE_NTFY_SERVER`) picks
the ntfy server, defaulting to the public `https://ntfy.sh`.

Only newly-appeared defects trigger a notification -- a clean run, or a
run whose defects were already known, sends nothing. If a topic is
configured but the send itself fails, `doctor` prints the failure and
exits non-zero even if the memory store is otherwise fine: a watcher that
cannot reach anyone must never report success.

## Commands

```bash
spoke check              # structural defects: missing frontmatter, dead links, index drift
spoke stale               # records whose cited claim looks out of date
spoke doctor              # check + stale, tracked run over run, reports what's new
spoke contradictions      # LLM-assisted scan for records that disagree with each other
spoke scan                # derive product/decision/shared-code nodes from the project's repos
spoke serve               # small HTTP API over the above
spoke schedule            # install/status/uninstall a daily launchd run of `doctor`
spoke hooks               # install/status/uninstall the SessionStart + Stop hooks
```

`check`, `stale`, `doctor`, `contradictions`, and `serve` each accept
`--config <path>` (default `config.toml` in the current directory) --
placed *after* the subcommand, e.g. `spoke check --config
other.toml`. A top-level `--config` before the subcommand is parsed but
then silently discarded once the subcommand's own `--config` (unset,
defaulting to `config.toml`) overwrites it; always put `--config` after
the subcommand name. `projects add`, `projects list`, and `projects
remove` do not take `--config` at all -- they read and write the
project registry, not `config.toml`.

## Projects

A **project** is a name plus one or more repo paths. It exists so one
`spoke` install can check several repos' memory stores without
any of them being able to see or affect the others.

### Why the store is derived, not typed in

Claude keys a session's memory to the absolute path of the repo it was
launched from (slugged into a directory name under
`~/.claude/projects/`). Given a project's repo paths, `spoke`
computes the matching store path itself rather than asking you to type
it — a typed path can drift out of sync with the repo it's supposed to
describe; a derived one cannot, because it's recomputed from the repo
path every time.

If a project's repos don't resolve to exactly one store holding
records, commands refuse rather than guess (see "Ambiguity is refused"
below). You can pin the store explicitly instead: pass
`--memory-store <path>` when adding the project.

### Adding a project

```bash
spoke projects add myproject --repo /path/to/repo
spoke projects list
```

`projects list` prints each project's repo paths, its derived store
path, and the record count found there — useful to confirm the derived
path is the one you expect before running any check against it.

A project can list more than one `--repo` (e.g. a repo that's been
cloned to two locations); their stores are deduplicated by resolved
path so records aren't double-counted.

Running `projects add` again with a name that exists **grows** it: the
new `--repo` paths are added and nothing else about the project changes.
A repo already present is reported, not duplicated. `--memory-store` and
`--accept` are refused on an existing name -- they would overwrite what
the project already carries, under a command you thought was adding a
repo. The registry file is the only place a repo can be *removed* from a
project without removing the project.

### Removing a project

```bash
spoke projects remove myproject
```

This forgets the registration only. It never touches that project's
repos or its memory store (the `.md` records under
`~/.claude/projects/.../memory`) -- deregistering is not deleting.

### Selecting a project

```bash
spoke check --project myproject
```

or, equivalently, set `SPOKE_PROJECT=myproject` in the environment. An
explicit `--project` flag always wins over the environment variable.
When a project is active, every command prints `project: <name>` as its
first line, so the output is never ambiguous about which store it read.

With exactly one project registered, that project is used automatically
and does not need to be named.

### Scheduling `doctor` (macOS launchd)

```bash
spoke schedule --install --project myproject   # write + load a daily launchd agent
spoke schedule --status                        # installed? loaded? when did it last actually run?
spoke schedule --uninstall                      # unload and remove it
```

`doctor` only helps if something actually runs it. `schedule --install`
writes a `launchd` agent (macOS only) that runs `doctor --project
<name>` once a day at 08:30 by default (`--hour`/`--minute` to change
it), and loads it immediately. Every path the agent references --
the `spoke` binary, `config.toml`, the doctor state file, the
log file -- is resolved to an absolute path at install time, since
launchd runs agents outside any shell's notion of a current directory
or `$PATH`.

With more than one project registered, `--install` requires `--project`
and refuses to guess, the same way every other command does.

The agent's `launchd` label defaults to a neutral `com.local.spoke`
-- override it with `--label-prefix` or `SPOKE_LAUNCHD_LABEL_PREFIX` to
match your own naming convention. There is no default naming any real
organisation baked into the package.

`--status` is what makes "installed but has never actually fired" visibly
different from "ran and found nothing": it reports whether the plist
exists, whether `launchd` currently has it loaded, and the `last_run`
timestamp read from the doctor state file the agent's own runs write to
-- not just whether the *installation step* succeeded.

### Ambiguity is refused, not guessed

With two or more projects registered and no `--project` and no
`SPOKE_PROJECT`, every command refuses outright (exit code 2) and
lists the registered project names, rather than silently picking one.
Acting on an unstated assumption about which project was meant is the
same defect as a hardcoded store path — this tool exists to remove
that, not reintroduce it in a different shape.

The registry lives at `~/.claude/spoke/projects.toml` by
default (override with `SPOKE_REGISTRY`). It records project names and
repo paths only — never the store contents themselves.

## Licence

MIT. See [LICENSE.md](LICENSE.md).

Spoke is the third of three MIT tools, alongside
[Tare](https://github.com/onlinejourno/tare) and
[Forage](https://github.com/onlinejourno/forage).
