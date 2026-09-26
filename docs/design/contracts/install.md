# Contract: install and uninstall

Source: `src/engmem/install.py`. What each agent mode writes, and where, is `ENGMEM-SPEC.md`
§5. This file records how those writes happen; each rule was learned from damage.

## Two kinds of file, and only one of them is engmem's

**engmem's own files** — the templates in `~/.claude/commands/`, `.github/prompts/` and
`~/.copilot/skills/<name>/SKILL.md` — are rewritten whole by every install and can be rebuilt
from the package at any time, so `_write_template` writes them in place: a torn write costs a
re-run of an idempotent command.

**The user's files** — the trigger-rule file (`CLAUDE.md`, `.github/copilot-instructions.md`)
and `claude_desktop_config.json` — hold content engmem cannot regenerate (the user's own
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

No config files is a hard constraint, so `install --store X` creates and initialises `X` and
writes it nowhere the installed templates read: they resolve `$ENGMEM_HOME`, else
`~/Developer/engmem`, each time they run. For the template agents, when `X` differs from what
they will resolve, install prints a second stdout line naming both and the `ENGMEM_HOME=` to
set. Before it did, install printed `store=X` and said nothing else, and every later draft,
search and telemetry row went to a different store than the one just created — or failed with
"store not found" — splitting documents from their telemetry. `claude-desktop` prints no
such line: its MCP entry records the store path itself.

## Every failure is a diagnosis, never a traceback

`ENGMEM-SPEC.md` §5 promises exit 2 with a named cause on both streams for every failure the
user's own environment can produce: a template path occupied by a directory, an unwritable
directory, an instructions file or a Desktop config engmem cannot decode, a `git` on PATH that
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

One failure is swallowed. `_claude_desktop_entry_present` only decides whether uninstall adds
its "you may have meant a different agent" nudge, so a config it cannot parse counts as
nothing found: the command has already done its work by then, and a second diagnosis of a
file this run was never going to write would bury the summary that matters.
