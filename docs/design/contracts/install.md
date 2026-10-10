# Contract: install and uninstall

Source: `src/engmem/install.py`. What each agent mode writes, and where, is `ENGMEM-SPEC.md`
§5. This file records how those writes happen; each rule was learned from damage.

## Two kinds of file, and only one of them is engmem's

**engmem's own files** — the templates in `~/.claude/commands/`, `.github/prompts/`,
`~/.copilot/skills/<name>/SKILL.md` and `~/.agents/skills/<name>/SKILL.md` — are rewritten whole by every install and can be rebuilt
from the package at any time, so `_write_template` writes them in place: a torn write costs a
re-run of an idempotent command.

**The user's files** — the trigger-rule file (`CLAUDE.md`, `.github/copilot-instructions.md`,
Codex's `AGENTS.md`), `claude_desktop_config.json` and Codex's `config.toml` — hold content engmem cannot regenerate (the user's own
instructions; every other MCP server they configured, tokens included), and engmem rewrites
them **whole** to append or drop a couple of lines. They go through `_replace_user_file`,
which stages a sibling temp file with `staging` (the store's own staged write; there is no
second implementation here) and `os.replace`s it, so a failure mid-write leaves the original
exactly as it was. Anything raised between the stage and the commit — a `BaseException`, not
only an `OSError` — takes the temp file with it: that file is a full copy of the original, API
tokens included, and a Ctrl-C would otherwise leave one behind per run.

Four consequences follow, each deliberate:

- **A new file is created 0600, an existing one keeps its mode** (`staging.commit` restores
  it). The Desktop config routinely carries other servers' API tokens, so private-by-default
  is the side to err on when engmem is the one creating it.
- **A symlink is written *through*, never *over*.** `_replace_user_file` resolves the link
  first and replaces the target; `os.replace` on the link itself would leave a regular file
  where the link was, and a `CLAUDE.md` symlinked into a dotfiles repository is the ordinary
  setup. This is the opposite of `backfill`'s rule for store documents, which refuses a
  symlink outright (`contracts/backfill.md`, "What the replaced file inherits"); its reasons —
  a relocatable store, two names sharing one `id` — do not apply to a fixed path in the user's
  own home.
- **A read-only file in a writable directory is still replaced**, with its mode restored. The
  directory is where the permission lives, as in every atomic-replace editor; refusing would
  let a `chmod` on a dotfiles copy block the install.
- **A hard link is broken.** `os.replace` puts a new inode at the path, so a second name for
  the same `CLAUDE.md` keeps the old content and stops tracking the one engmem wrote. Not
  worked around: the alternative is truncate-and-rewrite in place, which trades a broken link
  for a destroyed file on a full disk or a crash, and a hard link to a config file is rare
  where a symlink is the setup engmem protects.

## The file is read as bytes, and appended to in its own dialect

`_read_user_file` decodes the bytes itself (`staging.read_document`) rather than calling
`read_text`, which translates line endings and silently swallows a byte-order mark; a file
rebuilt from what `read_text` returned hands a Windows user a whole-file diff — every line
re-ended, the BOM gone — for a two-line append. The trigger rule is therefore appended with
the newline the file already uses (`staging.newline_of`), and the BOM is written back. The
Desktop config is the exception: engmem re-serialises that document from scratch, and RFC 8259
forbids emitting a BOM, so it is dropped there.

A file engmem cannot decode as UTF-8 stops the command with a named cause. It is not skipped
and not rewritten from a lossy reading: install's whole job on that file is to append to a
copy of it, and a copy engmem could not read is not one.

## An earlier wording of the rule is updated in place, never skipped

`_append_trigger_rule` decides "leave alone" on `TRIGGER_MARKER` (`` `engmem search``), a
substring chosen so a rule the user reworded is never duplicated. Every wording engmem has
ever written contains that substring too, so without a second check the first wording
installed on a machine would be the last: a re-run of `engmem install` after the rule changed
would report "already present" and write nothing. That is what happened when `--session`
joined the rule on 2026-09-18 — the rule in the wild still read the earlier wording, and every
CLI search it triggered was unattributed.

The migration runs before the marker check. A line whose stripped text is exactly one of
`LEGACY_TRIGGER_RULES` is replaced with `TRIGGER_RULE` in place — same position, same leading
indent, same line ending, sentinel (if any) untouched, BOM written back — and the install
reports "trigger rule updated". Only an exact legacy line qualifies: a user's own sentence
that merely contains the marker is still skipped, per the rule above. A bare legacy line with
no sentinel (pre-sentinel installs) is updated in place too, without adding a sentinel, so the
file changes by exactly one line either way. `_remove_trigger_rule` recognises the legacy
wordings alongside the current one, sentinel-anchored or bare, so an uninstall on a file that
was never migrated still removes exactly engmem's lines.

`LEGACY_TRIGGER_RULES` grows by one entry each time the wording changes and never shrinks: an
entry dropped is a file somewhere that install will skip and uninstall will leave behind.
`tests/conftest.py` carries its own copy of both the current and the legacy text, so a change
to either constant fails a test rather than silently moving the goalposts.

## `--store` is used once, and said to be used once

`install --store X` creates and initialises `X` and saves it nowhere: `--store` means "this
command only" on every subcommand, and AC-05.2 forbids an override rewriting the saved choice.
Making install the one command where the flag also persists would give the same flag two
meanings, and a test install (`--store /tmp/scratch`) would silently retarget every later
command. Saving is its own command, `engmem store set X` (`contracts/runtime.md`). Owner decision
(2026-10-08): install does not save, not even when no choice is saved yet.

So when `X` is not the store that commands run without `--store` will resolve, install prints a
second stdout line naming that store, where it came from, and the action: `engmem store set X`,
or `ENGMEM_HOME=X` when the environment variable is what decides. Before any note existed,
install printed `store=X` and said nothing else, and every later draft, search and telemetry row
went to a different store than the one just created — or failed with "store not found".
`claude-desktop` gets the note too since US-05: its MCP entry records `X`, but the CLI does not
read that entry, so without the note Desktop and the shell would search different memories.
`chatgpt` prints its tunnel steps instead. A saved choice that cannot be read is named in the
note rather than raised: the install itself used `--store` and succeeded.

Without `--store`, install resolves the saved choice like every other command; a broken one
stops install (and uninstall) before anything is written or removed.

## Checking the wiring: `engmem store show`

The MCP entries install writes (`claude_desktop_config.json`, Codex's block) record the store
they were installed with, so a later `engmem store set` can leave them pointing elsewhere.
`installed_mcp_wirings` reads both configs — read only, through the same `_load_json_object` and
`_parse_toml` install uses — and `store show` prints one line per entry: matching, `mismatch:`
with the install command that rewires it, or, for a Codex entry the user wrote, the table to
edit by hand. The entry's `--store` is read the way `engmem mcp` will read it, through
`runtime.store_path_of`: the last occurrence wins, a blank value is no `--store` at all (the
server resolves at launch), and a leading `~` is expanded — neither client runs the args
through a shell, so the server expands it itself, and comparing the raw spelling reported a
false mismatch. A value still relative after that has no fixed base (it depends on the cwd the
client launches from) and is reported as such, never as a match or a mismatch. A config it cannot parse is a `warning:` line, not a failure: the effective store
is still the answer the user asked for. Nothing is rewritten and no document is moved; which
store holds the user's documents is their call (AC-05.4). The comparison itself is
`wiring_verdict` and the fix `rewire_action`, shared with `engmem doctor`, which checks the rest
of one client's wiring (`contracts/doctor.md`).

## Every failure is a diagnosis, never a traceback

`ENGMEM-SPEC.md` §5 promises exit 2 with a named cause on both streams for every failure the
user's own environment can produce: a template path occupied by a directory, an unwritable
directory, an instructions file or a Desktop or Codex config engmem cannot decode, a `git` on PATH that
cannot be executed, a full disk during the replace. `_SetupError` is the single carrier: every
filesystem step raises it with the path and the OS reason, and `cmd_install` turns it into
`fail()` plus exit 2 in one place. Uninstall cannot, because by the time it reaches the
instructions file the templates are already gone: `_remove_trigger_rule_reporting_failure`
counts the failure, the summary still prints what *was* removed and names the file left
behind, and it opens with `engmem uninstall incomplete` — a reader who stops at the first line
must not read a partial uninstall as a clean one.

Two paths stay outside that promise on purpose, both bugs in engmem's own packaging rather
than anything a user can act on: a template missing from the installed wheel
(`_template_source`) and one that does not carry the front matter every shipped template has
(`_to_skill_front_matter`). "Fix it by hand and re-run" is the wrong advice for a file the
user does not own; the traceback names the template and belongs in a bug report.

Failures are swallowed in one kind of place. `_claude_desktop_entry_present` and
`_codex_mcp_block_present` only decide whether uninstall adds its "you may have meant a
different agent" nudge, so a config they cannot read counts as nothing found: the command has
already done its work by then, and a second diagnosis of a file this run was never going to
write would bury the summary that matters. For the same reason `engmem uninstall --agent codex`
looks for its block before it parses `config.toml`: a broken config with no block in it holds
nothing of engmem's, and is not reported.

## Codex's `config.toml`: a block engmem owns inside a file it does not

The standard library reads TOML (`tomllib`) but cannot write it, and re-serialising the user's
config through a third-party writer would drop their comments and reorder their tables for a
five-line change. So the entry is plain text between `CODEX_MCP_BEGIN` and `CODEX_MCP_END`,
and both directions work on lines: install appends the block or replaces it where it stands,
uninstall cuts exactly that span. The file still goes through `_replace_user_file`, keeping
its BOM and line ending like the trigger-rule files.

Text edits on a structured file are only safe if the result is checked, and checking that it
still parses is not enough: a key the user typed below engmem's header belongs to engmem's
table, and with the header gone it parses fine as part of the table above — `enabled = false`
moved onto another server switches that server off. So every write compares meaning: install
requires the composed file to parse to the original config plus engmem's entry, uninstall
requires it to parse to the original minus that entry (and minus `mcp_servers` when it was
the only server). An inline `mcp_servers = {…}`, which a later table header cannot extend,
fails the same comparison. A reinstall that would rewrite the block refuses when the user
added keys inside it (`_refuse_keys_added_to_the_block`), naming them: the block is
rewritten whole, so they would be dropped. Every refusal names the path and the fix, and
leaves the bytes alone.

`_toml_string` reuses `json.dumps`, whose escapes TOML shares, with two exceptions it handles:
DEL, which TOML forbids raw and JSON leaves alone, and a lone surrogate (an undecodable byte
in a POSIX path), which no TOML string can hold and is refused by name.

An `[mcp_servers.engmem]` without the markers is the user's, and is reported, never rewritten,
never removed: the same rule as `TRIGGER_MARKER` for the trigger rule.

The config step runs before the skills and the trigger rule. It is the only codex step that
refuses on a well-formed environment, and running it last left a refused install with three
skills and a rule already written.

Codex sandboxes shell commands to the working tree by default, so the CLI half of the wiring
cannot write drafts, saves or telemetry to a store outside it, and the templates do not fall
back on their own: they choose MCP only when there is no shell. Install says so and names
`writable_roots` instead of editing the sandbox: widening what an agent may write is the
user's decision.

The trigger rule Codex gets is the shared wording, `/engmem` included, where Codex would say
`$engmem`. A Codex-only wording would be one more exact line for install and uninstall to
recognise forever (`LEGACY_TRIGGER_RULES`), for a reference the model reads correctly either
way. Codex reads `AGENTS.override.md` instead of `AGENTS.md` when it exists; install does not
write into the user's override and says so instead.

## Walkthrough: a Codex config of its own (US-17)

Owner decisions, 2026-10-10: the `walkthrough` subcommand is approved (D1); the demo has its own
`CODEX_HOME`, at the cost of one extra sign-in, and credentials are never copied (D2); the
sandbox is widened only in that demo config (D3). Coordinator defaults the owner may override:
D4 to D7 below.

`engmem walkthrough codex` writes a demo `CODEX_HOME` instead of pointing the user's own Codex at
the demo store. The user's `[mcp_servers.engmem]` launches the server with `--store` set to their
store, so a demo run with only `ENGMEM_HOME` changed would send shell commands to the demo and
MCP writes to the real store. That split is the one thing the demo must not allow. Skills are
the exception: Codex reads them from `~/.agents/skills` whatever `CODEX_HOME` says. They name no
store, so the walkthrough uses the installed ones and installs none. It reuses
`_install_codex_mcp` (given the demo's config path) and `_append_trigger_rule`, so a re-run is
as idempotent as install, and a user's own entries in the demo config are kept the same way.

Install never widens the sandbox, because that is the user's decision. The walkthrough does,
but only in the demo config it creates (D3): `writable_roots` holds only the demo store, and the
file is written once, so a later edit stays. The Codex login is not copied (D2). Credentials are
not engmem's to move, and the cost is one sign-in.

The demo directory is resolved once (D7), so `writable_roots`, `shell_environment_policy.set`
and the MCP entry's `--store` all name the store's real path. On macOS `/tmp` is a link to
`/private/tmp`, and a sandbox comparing paths could otherwise see two different directories.
Three things about Codex were confirmed only from the strings in the Codex 0.160.0 binary,
never by running it: that `shell_environment_policy.set` exists, that skills are read from
`~/.agents/skills` whatever `CODEX_HOME` is, and how `writable_roots` matches paths. The owner's
real Codex run (E-05) is what confirms them.

Every file the walkthrough creates is opened with `x` (exclusive). An existing file, or a link
left in its place, is never written over. Before anything is written, every path it writes
through is checked: the store and its `sessions/`, the demo `CODEX_HOME` and its two files, the
workspace, and each workspace file with its parent directories. A symbolic link is refused by
name. Any other path whose resolved form leaves the demo directory is refused too. That covers
a Windows junction, which `is_symlink()` does not report, and which could otherwise let the MCP
block be written into the user's own `.codex`. A directory without the walkthrough's marker is
refused when it is not empty: the marker is what makes "re-run" and "someone else's directory"
different.

The doctor run is a subprocess with `CODEX_HOME` and `ENGMEM_HOME` set for it alone, and
`PYTHONSAFEPATH=1`, so an `engmem/` directory left in the demo cannot stand in for the installed
package. The walkthrough process's own environment is never changed.

`--verify` reuses `_finds` from feedback, so "found" means what the usefulness summary means:
shown to a session other than the document's own, and not only as a weak candidate. It does not
check that the session came later or that its id names a demo document. Every published record
that qualifies is a candidate, because session 2 may save a `request_id` record of its own; the
check passes when any of them was found. It reads only the short-capture labelled lines
(`Decision:`, `Reason:`, `Source:`), so the walkthrough asks for `$engmem-save-quick` (D6).

The acceptance check `--verify` runs is the source engmem ships, passed to `python -c` with the
workspace on `PYTHONPATH`, never the workspace's `demo_orders/check.py`. The agent can edit that
copy, and an edited copy that prints PASS would otherwise complete the walkthrough. A changed copy
is reported in a `note:` line. What the check imports is still the workspace's `demo_orders`,
code the agent edited, and it runs with the Python engmem runs on (D5). The output says so.

The demo's MCP server, and shell `engmem` commands outside Codex's sandbox, share
`~/.cache/engmem` with the user's store (D4). A demo search can prune cache entries for the
user's documents. They are rebuilt on the next search, the sharing contracts/cache.md already
accepts, and no document is touched.

## ChatGPT: nothing to write

ChatGPT's connector lives in the ChatGPT workspace, so `--agent chatgpt` creates the store and
prints the `tunnel-client` commands with `--mcp-command` built from `_stdio_mcp_entry`, the same
launch line the other MCP clients get, quoted as one argument for the platform's shell
(`shlex` on POSIX, `list2cmdline` on Windows). Uninstall prints where the
connector is deleted and reports 0, without the "other agent present" nudge, which would read
as if it had looked for ChatGPT's files.
