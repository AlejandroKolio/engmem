# Contract: install and uninstall

Source: `src/engmem/install.py`. What each agent mode writes, and where, is in
`ENGMEM-SPEC.md` §5 and is not repeated here. This file records the rules that decide
*how* those writes happen, because each was learned from damage rather than from design.

## Two kinds of file, and only one of them is engmem's

**engmem's own files** are the ones every install rewrites whole and can rebuild from the
package at any time: the templates in `~/.claude/commands/`, `.github/prompts/`, and
`~/.copilot/skills/<name>/SKILL.md`. A torn write to one of them costs a re-run of an
idempotent command, so they are written in place with `_write_template`.

**The user's files** are the ones engmem edits but does not own: the trigger-rule file
(`CLAUDE.md`, `.github/copilot-instructions.md`) and `claude_desktop_config.json`. Both
hold content engmem cannot regenerate — the user's own instructions, and every other MCP
server they configured, tokens included — and engmem rewrites them **whole** to append or
drop a couple of lines. These go through `_replace_user_file`, which stages a sibling temp
file and `os.replace`s it, so a failure mid-write leaves the original exactly as it was.
It reuses `staging`, the same staged write the store's documents get; there is no second
implementation of it here. Anything raised between the stage and the commit — a `BaseException`,
not only an `OSError` — takes the temp file with it: that file is a full copy of the original,
API tokens included, and a Ctrl-C during an install would otherwise leave one behind per run.

Four consequences follow from that choice, and each is deliberate:

- **A new file is created 0600, an existing one keeps its mode** (`staging.commit`
  restores it). The Desktop config routinely carries other servers' API tokens, so
  private-by-default is the right side to err on when engmem is the one creating it.
- **A symlink is written *through*, never *over*.** `_replace_user_file` resolves the link
  first and replaces the target. `os.replace` on the link itself would leave a regular
  file where the link was — and a `CLAUDE.md` symlinked into a dotfiles repository is the
  ordinary setup, not an exotic one. This is the opposite of `backfill`'s rule for store
  documents, which refuses a symlink outright; the reasoning there (a relocatable store,
  and two names sharing one `id`) does not apply to a fixed path in the user's own home.
- **A read-only file in a writable directory is still replaced**, with its mode restored.
  The directory is where the permission actually lives; this is how every atomic-replace
  editor behaves, and refusing would mean a `chmod` on a dotfiles copy blocking the
  install.
- **A hard link is broken.** `os.replace` puts a new inode at the path, so a second name
  for the same `CLAUDE.md` keeps the old content and stops tracking the one engmem wrote.
  This is inherent to atomic replacement and is not worked around: the alternative is
  truncate-and-rewrite in place, which trades a broken link for a destroyed file on a full
  disk or a crash. A symlink is the setup engmem protects (above); a hard link to a config
  file is rare enough not to be worth the whole file for.

## The file is read as bytes, and appended to in its own dialect

`_read_user_file` decodes the bytes itself rather than calling `read_text`, which
translates line endings and silently swallows a byte-order mark. A caller that rebuilds a
file from what `read_text` gave it hands a Windows user a whole-file diff — every line
re-ended, the BOM gone — in exchange for a two-line append. The trigger rule is therefore
appended with the newline the file already uses (`staging.newline_of`), and the BOM is
written back. The Desktop config is the exception: engmem re-serialises that document from
scratch, and RFC 8259 forbids emitting a BOM, so it is dropped there.

A file engmem cannot decode as UTF-8 stops the command with a named cause. It is not
skipped and it is not rewritten from a lossy reading: install's whole job on that file is
to append to a copy of it, and a copy engmem could not read is not one.

## Every failure is a diagnosis, never a traceback

`ENGMEM-SPEC.md` §5 promises exit 2 with a named cause on both streams for precondition
failures. That promise covers *every* failure the user's own environment can produce, not
the two directories that once had a check: a template path occupied by a directory, an
unwritable skill directory, a config directory that cannot be created, an instructions file
or a Desktop config engmem cannot decode, a `git` on PATH that cannot be executed, a full
disk during the replace.

`_SetupError` is the single carrier. Anything that talks to the filesystem raises it with
the path and the OS reason in the message; `cmd_install` catches it in one place and turns
it into `fail()` plus exit 2. Uninstall cannot do the same, because by the time it reaches
the instructions file the templates are already gone: it counts the failure instead
(`_remove_trigger_rule_reporting_failure`), still prints what *was* removed, names the
file left behind, and opens the summary with `engmem uninstall incomplete` — a reader who
stops at the first line must not read a partial uninstall as a clean one.

Two paths stay outside that promise on purpose, and both are bugs in engmem's own packaging
rather than anything a user can act on: a template missing from the installed wheel
(`_template_source`) and one that does not carry the front matter every shipped template has
(`_to_skill_front_matter`). "Fix it by hand and re-run" would be the wrong advice for a file
the user does not own; the traceback names the template and belongs in a bug report.

One failure is swallowed. `_claude_desktop_entry_present` only decides whether uninstall adds
its "you may have meant a different agent" nudge, so a config it cannot parse counts as
nothing found. The command it advises on has already done its work by then, and a second
diagnosis of a file this run was never going to write would bury the summary that matters.
