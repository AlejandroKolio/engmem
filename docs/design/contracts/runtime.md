# Contract: store resolution and failure reporting

Source: `src/engmem/runtime.py` and `src/engmem/settings.py`. The store resolves `--store PATH`, then `ENGMEM_HOME`, then
the choice saved by `engmem store set`, then `~/Developer/engmem` (`ENGMEM-SPEC.md` §3). This
file records what that order leaves open: what counts as a setting, what shape the answer takes,
where the saved choice lives and what happens when it cannot be read, and why a failure is
printed twice. It also records the second one-line setting, the mode (US-08).

## The saved choice: one file, one line (US-05, owner decision 2026-10-07)

Before US-05 engmem had no config file at all, and `install --store X` was used once and
forgotten: the installed templates, every later shell command and the MCP entry each resolved
their own store, and CLI and MCP could work with different memories without saying so. The
owner relaxed "no config files" to exactly one exception: a file holding one line, the absolute
store path, and nothing else. A second setting would need its own decision; this file is not a
place to grow one. US-08 took that decision for the mode, as a second file of its own (below), so
the rule now reads "one-line setting files: the store path and the mode".

**Where.** `$XDG_CONFIG_HOME/engmem/store`, default `~/.config/engmem/store`, the same XDG
family as the cache (`contracts/cache.md`). `XDG_CONFIG_HOME` is honoured on every platform when
it is an absolute path; a relative one is ignored, as the XDG spec requires, because it would
make the choice depend on each command's working directory. On Windows without it, the file is
`%APPDATA%\engmem\store` (`~/AppData/Roaming` when `APPDATA` is unset) — where Windows keeps
roaming per-user settings and where `claude_desktop_config_path` already looks, so a Windows
user finds it next to the client config it has to agree with; `~/.config` is not a place a
Windows user ever looks. The default store itself stays the same on every platform (below):
moving it would point existing installs at an empty directory, and the setting file is new, so
there is nothing to move.

**Read lazily, in fourth place.** `locate_store` reads the file only when neither `--store` nor
`ENGMEM_HOME` names a store. An override is a one-off: it never writes the file, and a broken
file does not stop a command that does not use it, so a user can still work with
`--store` while repairing the saved choice. `engmem store show` reads it either way and names a
saved choice the override is hiding.

**What the line may hold.** One line, with or without its line ending (LF or CRLF) and with a
UTF-8 byte-order mark tolerated, because Notepad writes one. The path is never trimmed, for the
same reason as the blank rule below. A leading `~` is expanded, and the result must be absolute:
a relative path has no fixed base (each command has its own cwd), and resolving it against the
file's own directory would be a rule nobody guesses. `engmem store set` always writes the
absolute path, so only a hand edit can produce one.

**An unusable file is an error, never "no choice".** Empty or blank, more than one line, a NUL
byte, invalid UTF-8, a relative path, a directory or an unreadable file at that path, and a
symlink whose target is gone all raise `StoreSettingError` with the file and the reason. A
command stops with exit 2 (`cli.main` turns the error into `fail` in one place; `engmem mcp`
writes it to stderr only). Falling back to `~/Developer/engmem` would search and write a
different memory while reporting nothing wrong — exactly the failure US-05 removes (AC-05.3).
Only a file that does not exist (and is not a dangling link) means "no saved choice", and then
the behaviour is the one from before US-05, byte for byte.

**A saved store that does not exist** is not this module's error: `resolve_store` stays pure and
the commands name `store/sessions` as missing, as they do for a mistyped `--store`, now with a
pointer to `engmem store show` to see which setting chose the path. Neither the CLI nor the MCP
write tools look anywhere else.

**No `store unset`** (owner decision, 2026-10-08): deleting the file is the way back to the
default, and `store show` prints its path. **Install never saves** either (`contracts/install.md`).

**Writing it.** `save_store` stages a sibling temp file and replaces atomically (`staging`), so
a crash leaves the old choice or the new one, never half a path. A symlinked setting file is
written through, like the user files install edits (`contracts/install.md`). A path with a line
break, or one that cannot be encoded as UTF-8, is refused rather than written as something the
reader would reject.

## The saved mode: a second one-line file (US-08, owner decision 2026-10-08)

`$XDG_CONFIG_HOME/engmem/mode`, next to the store file and found by the same rules
(`settings.setting_file`, Windows `%APPDATA%` included), holds one word: `daily` or `research`.
`engmem mode set daily|research` writes it the way `store set` writes the store, atomically and
through a symlink; `engmem mode show` prints `mode: <mode>` on its first line, the line the
start template reads, then where it came from. No file means `daily`, the default the owner
chose: research mode is opt-in, so ordinary work never pays for the experiment's ritual.

**A second file, not a second line.** The store file's reader refuses anything but one line
holding an absolute path, and that strictness is what makes a broken store choice a named
error; a second line there would mean loosening the one check US-05 depends on. Two files keep
each reader exact and let one be repaired or deleted without touching the other.

**Tolerant of how a person types it, strict about what it says.** Surrounding whitespace, case,
a line ending and a UTF-8 BOM are accepted (`Research\r\n` reads as `research`), because none of
them can change which of two words was meant. Empty, more than one line, invalid UTF-8, a
directory or an unreadable file, and any other word raise `ModeSettingError` with the file and
the cause. `cli.main` turns it into exit 2 on both streams, like `StoreSettingError`. Reading a
broken file as `daily` would silently drop a user who chose research out of the experiment.

**Read only where the mode decides something.** `engmem mode show`, the MCP server's start
prompt and `engmem_create_draft` read it, each time they run, so a change applies to the next
session even in a server process that has been running since before the change (AC-08.4).
Search, roles, telemetry and backfill never read it: a broken mode file is not a reason a search
fails. Templates without a shell read the file by hand, with the same default.

**No environment override.** The store has `ENGMEM_HOME` because a one-off store is a real need;
a per-shell mode would let the shell, which runs the template, and the MCP server, launched by
the client with an environment of its own, record different modes for one session, the
divergence US-05 removed for the store. One file is the one answer both read.

**`settings.py` and not `runtime.py`.** The mode is read inside `engmem mcp`, whose stdout is the
protocol channel. `runtime` holds `fail`, which writes to stdout, and
`tests/test_stdout_is_protocol_only.py` keeps every module the server imports free of stdout
prints. The setting-file mechanics (location, read, atomic write) moved to `settings.py`, which
both `runtime` and `mcp_server` import; the store-specific rules stayed in `runtime`.

## A blank setting is not a setting

An empty or whitespace-only `--store` or `ENGMEM_HOME` falls through to the next source.
`ENGMEM_HOME=` is the ordinary result of `export ENGMEM_HOME="$SOME_UNSET_VAR"`, and honouring
`ENGMEM_HOME="   "` once resolved to a directory literally named `"   "` under the shell's
working directory, which `engmem install` would `git init` and start writing documents into —
a wrong store that reports nothing wrong (§10, principle VIII).

The value is never trimmed: `--store "notes "` is a legal POSIX path, and silently retargeting
it would be the same bug in the other direction.

## The answer is absolute, and it is not resolved

Absolute, because the answer outlives the process that computed it: `install --agent
claude-desktop --store ./notes` writes the path into `claude_desktop_config.json` (and `--agent
codex` into `config.toml`), and the client launches the server from a working directory of its
own — the reason `stdio_mcp_entry` already spends `sys.executable` rather than a bare `engmem`. Every
diagnostic that names the store gains the same way: `store not found: sessions` sends the
reader nowhere.

Not `Path.resolve()`, because the path is what the user reads back in every message, and a
symlinked store (`~/Developer/engmem` pointing into a synced folder or a dotfiles checkout) is
a supported setup. No security invariant rides on this: `commands._sessions_is_contained` and
`drafts._resolve_sessions_dir` re-resolve the store themselves, so a store handed to them
already resolved changes neither half of their comparison. `..` segments are left alone for
the same reason — `os.path.abspath` would collapse them lexically and, across a symlinked
parent, name a directory the kernel would not reach.

## Absolutising is hand-ported, not `Path.absolute()`

`resolve_store` and `default_store` go through `_absolute()`, which picks the cwd itself —
`os.path.abspath(path.drive)` when the path carries a drive, `os.getcwd()` otherwise — and
joins with `/`. `Path.absolute()` gained that drive branch only in Python 3.13 (bpo-89812);
before it, `resolve_store("C:notes")` with the process on another drive came back relative,
because a drive-letter component re-anchors a flat reparse
(`PureWindowsPath('D:/work', 'C:notes') == PureWindowsPath('C:notes')`). `pyproject.toml`
declares `requires-python = ">=3.11"` and CI runs `windows-latest` on 3.11, so that interpreter
ships; a drive-relative store written into the Desktop config is the wrong store that reports
nothing wrong.

## An unresolvable `~`/`~user` does not raise

`Path.expanduser()` raises `RuntimeError` rather than falling back when `~` or `~user` cannot
be resolved (no `HOME` and no passwd entry for the uid; `--store "~teammate/engmem"` for a
user this machine lacks). `_expand_home()`, which both `resolve_store` and `default_store`
use, catches it and returns the path unexpanded, so the literal `~nosuchuser/notes` (or
`default_store`'s bare `~`) becomes an ordinary relative component, is made absolute like any
other, and — not existing — is named by the `store not found` diagnostic
(`commands._read_sessions`) exactly as a mistyped `--store` would be. The alternative, an uncaught
traceback on stderr with stdout empty, is the swallowed error the next section exists to
prevent. `resolve_store` stays pure: whether the failure is fatal, one skipped item or an
interrupt is the command's choice, not this module's.

## A failure is printed to both streams

`fail` writes the same `error:` line to stderr and to stdout, because the consumer is an agent
that reads stdout and never stderr (§10, principle VIII; §5 applies the same rule to argparse's
own usage errors). stderr keeps the line where a human at a terminal expects it.

It returns `None` rather than exiting: the exit code belongs to the command, which knows
whether the failure is fatal (`return 2`), one document out of a batch (`cmd_backfill` counts
it and carries on), or an interrupt (`return 130`). A `SystemExit` inside `fail` would take
that choice from every caller at once.

`engmem mcp` must never call it: there stdout is the JSON-RPC channel and one plain line
corrupts the frame in flight (`contracts/mcp-server.md`, "Transport and the stdout-purity
rule"). `tests/test_stdout_is_protocol_only.py` reads `mcp_server.py`'s syntax tree and fails
if the module so much as imports `fail`.

## Reads stay inside the store (US-22)

A document in `sessions/` and a retained copy in `versions/<id>/` are read through
`confined.read_bytes`, and `sessions/` itself is listed through `confined.list_names`. The
parts of the path below the store root must not be links. A symbolic link there is refused by
name, and so is a Windows junction, which `is_symlink()` does not report. Nothing behind the
link is read. Without this, one link committed to a shared or imported store hands the agent
any file on the machine. That file comes back as a search hit, a `read` or an export preview,
and it is presented as a recorded decision. That is worse than a leak: it quietly replaces
the context the agent decides on. The logs in the store root follow their own section below
(US-23).

The root itself may be a link. `~/Developer/engmem` pointing into a synced folder is a
supported setup ("The answer is absolute, and it is not resolved", above). The root is the
owner's choice, so it is followed once and everything below it is held to the rule. A root
link whose target is gone is "store not found". It never falls back to another store.

How a read is made:
- **With `dir_fd` (Linux, macOS).** The read opens the root, then each part below it relative
  to the directory before, with `O_NOFOLLOW`. A link swapped in after the listing, at any
  depth, makes the open fail. No check-then-open window is left for it to land in.
- **The final open adds `O_NONBLOCK`,** and anything but a regular file is refused after the
  `fstat`. Without that, a FIFO named like a document would hang the search.
- **Without `dir_fd` (Windows).** Each part is checked with `lstat`, then the file is opened
  by name. A part counts as a link when it is a symlink or a name-surrogate reparse point,
  which covers a junction. Other reparse points are files in their own place: a OneDrive
  placeholder or a deduplicated file. Refusing them would lock a store in a synced folder out
  of every document. The type is checked before the open, because Windows opens a directory
  with `PermissionError` instead of `IsADirectoryError`. After the read, the name must still be the
  file that was read (`st_dev`, `st_ino`). That check catches a swap of the file itself. A
  directory above it swapped for a junction in the window between the check and the open is
  not caught. This is the residual race on that platform.
- **A hard link is not detected.** It is one more name for a file, and nothing about it
  points out of the store. `contracts/backfill.md` leaves it open for writes for the same
  reason.

A linked `sessions/` is not walked for stray markdown either, since its subtree is outside
the store and the stray warning would print the names found there. `engmem doctor` reports a
linked `sessions/` as an error, because a search behind it finds nothing.

A refused file is a load error like an unreadable one. Its id is missing from the store, and
the error names the link. `read` and `export` of that id say why it is missing, so the answer
is not just "no document". A search shows the error on stderr. Over MCP it shows only the
`N failed to load` count, because the client never sees stderr.

A document is also re-read after it was loaded, and those re-reads use the same reader:
- the current version a `read` compares with a cited one;
- the text an export rebuilds;
- the bytes a version is retained from, and an existing copy compared against them;
- the Reuse Log line number;
- a backfill proposal and its write;
- the MCP completion check and the supersede rebuild.

A link that lands between the load and one of these re-reads is refused there too. Otherwise,
for the retained version and the supersede rebuild, outside bytes would be copied into the
store, where every later read would trust them.

`tests/test_reads_stay_inside_the_store.py` covers:
- a file link, a linked `sessions/`, and the three places in `versions/` a link can sit;
- both read paths, with the `dir_fd` one switched off to run the Windows path on any platform;
- a linked and a dangling root;
- a swap between the listing and the open, and between the check and the open;
- each re-read after a load;
- which reparse points count as links, and a directory refused before it is opened;
- a linked `sessions/` in the stray scan (`tests/install/test_doctor.py` covers it in doctor);
- a junction, on Windows only.

## Logs are written inside the store (US-23)

`telemetry.jsonl`, `cost.jsonl` and `feedback.jsonl` are appended through
`confined.append_line` and read through `confined.read_log_lines`, with the same boundary as a
document: the file itself must not be a link or junction. A plain search appends telemetry, and
a read appends cost. Before this, a link at one of these names made every search write into a
file outside the store, and that file could be any file the user can write. The readers are
covered too. A feedback line carries the user's decision text, so `feedback summary` read
through a link would print whatever the outside file says. Telemetry decides which finds a
feedback record may assess, so a linked log could vouch for a document no search showed.

A refused or failed append is a named miss, never a success:
- search and read add `note: telemetry not recorded (...)` or `note: cost not recorded (...)`
  and still answer; a search whose store refuses two or more of its records prints one
  `note: the store refused writing — ...` line for them instead (`contracts/output.md`, "One
  note for an unwritable store");
- `feedback record` and `record-baseline` fail with `cannot write`.

The same applies to a directory or a FIFO at the name, or a file the user cannot write. A
dangling link creates nothing: opening it for append would otherwise create its target outside
the store. With `dir_fd` the open itself fails on the link (`O_NOFOLLOW`), so `O_CREAT` never
acts. Without `dir_fd` the `lstat` check refuses it before the open.

A reader treats only a missing name as an empty log. A link at the name, dangling or not, is
refused and named, as the writer refuses it. Otherwise `engmem telemetry` would print "0 rows"
for a log that every search reports it cannot write.

How an append is made:
- **With `dir_fd`.** The file is opened `O_RDWR | O_APPEND | O_CREAT | O_NOFOLLOW |
  O_NONBLOCK` relative to the store root, with mode `0o666` as `open()` would use. Anything but
  a regular file is refused after the `fstat`. A torn last line is closed with `\n`, and the
  line is written with one `os.write`. On a local Linux or macOS filesystem, `O_APPEND` writes
  to one file are serialised, so concurrent appenders land whole lines one after another. POSIX
  does not promise this for regular files, and over NFS or SMB, where the client computes the
  end itself, lines can collide. A store on a network share is not a supported setup for
  concurrent writers. Two appenders both closing the same torn line leave an empty line, which
  every reader skips.
- **Without `dir_fd` (Windows).** There `O_APPEND` is a seek followed by a write, so two
  appenders can overwrite each other. The append runs under the store's write lock for that
  file (`staging.document_lock`, a `.<name>.write-lock` directory in the store root). A held
  lock that outlasts the wait is a named miss. Its message gives the lock's path and says to
  remove it if no engmem process is running. A lock left by a killed process is never broken
  automatically, for the reason `contracts/mcp-server.md` gives for document locks. Until
  someone removes it, every append to that log is a named miss: every search shows its `note:`
  line, and logging of that file stops. A store root that refuses the lock's `mkdir`
  (read-only, write-protected media) fails at once with the `PermissionError`; it is not waited
  out as a busy lock (`contracts/mcp-server.md` gives the rule). So does a root whose ACL
  allows writing an existing log but not creating a directory: that store could log before
  US-23 and now reports every append as a named miss. The file is checked with
  `lstat` and opened by name. Before writing, its identity must still match the name, so a link
  swapped in after the check is not written through. A link swapped in for a missing log in
  that window can still leave an empty file outside the store. This is the residual race on
  that platform.

Telemetry used to be written in text mode, so a Windows store got `\r\n` line endings. It
now gets `\n` like the other two logs. Every reader splits the same way either way.

`tests/test_logs_stay_inside_the_store.py` covers:
- each writer through a link, on the CLI and over MCP, including read-only search;
- each reader through a link;
- a directory, a FIFO and an unwritable file at the name;
- a dangling link;
- forty concurrent appends after a torn line, where every append that returned is present
  once and every one refused is named;
- the check-then-open swap and the lock on the path without `dir_fd`: a held lock is named
  with its remedy, and a refused `mkdir` is a permission error, not a wait;
- a summary of a dangling log link.

## `~/Developer/engmem` is the default on every platform

`Developer` is a macOS convention, and the default is deliberately not per-platform. The path
is quoted verbatim in `ENGMEM-SPEC.md` §3, `README.md`, `docs/design/data-model.md`, the
`--store` help text and the `engmem.start` template; a store is a git repository the user
already has, so moving the default would silently point an existing install at an empty
directory to buy a platform nicety `ENGMEM_HOME` and `engmem store set` already provide.
