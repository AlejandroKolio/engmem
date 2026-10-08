# Contract: `engmem doctor`

Source: `src/engmem/doctor.py`. The command surface is `ENGMEM-SPEC.md` §5. This file records
why each check is shaped as it is. Added by US-06: "no results" can mean an empty memory,
another store, an executable that no longer starts, or a write the filesystem refuses, and
before doctor the only way to tell them apart was comparing configs by hand.

## A command of its own, not more `store show`

`engmem store show` prints `store:` on its first line for the `engmem.start` template, which
runs it at the start of every task. Doctor runs subprocesses, creates a probe file and reads
every file one client's wiring depends on. Putting that into `store show` would slow every
`/engmem` and add a write to a command the templates run on every task. `store show` stays as it
was. Doctor reuses its parts: `runtime.locate_store` and `runtime.source_description` for the
store, and `install.wiring_verdict` and `install.rewire_action`, which `store show`'s
`_wiring_line` now uses too. So the two commands cannot disagree about whether an entry
matches.

One client per run, chosen by `--agent`, which defaults to `claude` like `install` and
`uninstall` (owner decision, 2026-10-08: without `--agent` only claude is checked, not every
installed client), and is checked by the same `_validate_agent`. `--local` means what it means for
`install`, and is rejected for the same home-scoped agents. The checks follow what `install`
writes for that client: templates or skills, the trigger rule, the MCP entry, the shell
`engmem` the templates call.

## The report

The first line counts the results: `engmem doctor --agent A: E error(s), W warning(s), U
unverified`. A reader who stops at line one still knows whether something is broken. Each
following line is `<status>: <subject>: <detail>`. The status is one of four words:

- `ok`: checked on this host and fine.
- `warning`: works, but likely explains missing memory: an empty store, a template from
  another engmem version, an override only this shell has, CLI and MCP on different stores.
- `error`: this will fail when the client uses it. The line names the path, the error type
  (`FileNotFoundError`, `PermissionError`, `TimeoutExpired`, "not executable") and the fix.
- `unverified`: engmem cannot check it from here.

The report goes to stdout only, and stderr stays empty. A broken check is the answer the user
asked for, not a failure of the command, so `fail()` is not used for it. Exit code: 0 when no
line is an error, 1 when at least one is, 2 for a usage error (an unknown agent, `--local` with
a home-scoped one, argparse's own). 1 and 2 are kept apart on purpose: a script can tell "the
setup is broken" from "doctor was called wrong". Owner decision (2026-10-08): these three codes
stay, although other commands use 2 for every failure.

**Different stores are a warning, never an error (AC-06.2).** Two stores can be intended, for
example a Desktop entry kept on a separate memory. The line names both paths and both sources:
the CLI store with its source, the MCP store as `--store in <config>`. It also gives the
rewire command, the same one `store show` prints. An entry without `--store` resolves its store
from the client's own environment at launch, so it is `unverified`. The line also says what
this shell resolves.

**A saved choice that cannot be read is an error line, not exit 2.** Every other command stops
on it (`contracts/runtime.md`). Doctor reports it and keeps going: the templates, the shell
command and the MCP entry are still worth checking. When `--store` or `ENGMEM_HOME` picks the
store, doctor also resolves what a client launched without that override would use (the saved
choice, else the default). When that differs, it is a warning. When the saved choice behind the
override is broken, it is an error, because such a client stops on it.

## Sessions: listed, counted, and written once

`sessions/` is listed with `Path.iterdir`, so a missing directory, a file in its place and a
denied read each surface as their own exception type. Doctor never creates the directory. The
markdown files are counted: zero documents is the "empty memory" cause, a warning.

Writability is checked by writing, not with `os.access`. `os.access` reads mode bits for the
real uid. It does not see a Windows ACL (there `W_OK` only checks the read-only attribute), a
macOS privacy denial on a protected folder, a full disk or quota, or a network mount that
squashes root. The probe goes through `staging.stage`, the call every draft and save makes
first: `O_CREAT | O_EXCL`, mode 0600, write, fsync. So when the probe fails, the write tools
fail the same way. Its name is `.engmem-doctor-probe.md.<uuid>.tmp`. That is dot-prefixed and
ends in `.tmp`, so `sessions/*.md` never matches it and a concurrent search never reads it.

Cleanup (AC-06.4): `stage` removes its own file on any exception, `KeyboardInterrupt` included.
After a successful stage, doctor unlinks the probe at once, inside the same `try`. An interrupt
that lands after `stage` returned is caught as a `BaseException`, the probe is discarded and the
interrupt raised again. A failed unlink is an error that names the file to delete by hand,
never a leftover nobody hears about. The store root, where
`telemetry.jsonl` lives, is not probed. A store whose `sessions/` is writable but whose root is
not is unusual enough that a second probe file is not worth it.

## Executables: stat first, then run only what engmem wrote

Doctor reports `sys.executable` and the package version as facts. For the agents whose
templates run `engmem` in a shell, `_on_path("engmem")` finds the command and
`engmem --version` runs it. A missing command is an error. A different version is a warning:
two installs, and the shell runs the other one.

For an MCP entry, the recorded command is checked step by step. A bare name is looked up on this
shell's PATH. It is `unverified` when found, because the client launches it with its own PATH,
and an error when not found. A relative path with a directory in it depends on the directory the
client starts in, and is a warning. An absolute path must exist and be executable
(`os.access(X_OK)`, reported as `PermissionError`). On Windows `X_OK` is true for every existing
file, so that branch never fires there; a file Windows cannot run is reported by the probe
instead, as `cannot run … (OSError: …)`. Then, only when the arguments start with `-m engmem.cli`, the
shape `install` writes, doctor runs `<command> -m engmem.cli --version`. Running it matters
because the usual failure passes a stat check: a Python that still exists but no longer imports
engmem (a venv recreated, Homebrew's Python upgraded). Any other shape is the user's own
program, and running it with arguments engmem made up could do anything (`uvx` would reach the
network), so that entry is stat-checked and reported as not run.

**Nothing is taken from the directory doctor runs in.** A project may hold its own `engmem/`
package or an `engmem.bat`, and doctor is run from inside projects. `python -m` puts the working
directory first on `sys.path`, so `<python> -m engmem.cli --version` started there imported and
ran the project's `engmem/cli.py` instead of the installed engmem, and reported whatever version
it printed. And `shutil.which` on Windows tries the current directory before PATH. So every
probe runs with `cwd` set to the filesystem root (`os.path.abspath(os.sep)`) and with
`PYTHONSAFEPATH=1`, which keeps the working directory off `sys.path` on Python 3.11 and later.
The neutral `cwd` also covers an older Python in the entry, and a relative `PYTHONPATH` entry
such as `.`, which `PYTHONSAFEPATH` does not touch. Commands are looked up by `_on_path`, not
`shutil.which`: it walks only the absolute PATH entries, with `PATHEXT` on Windows. There, as
in `shutil.which`, a name that already ends in one of the `PATHEXT` extensions (`python.exe`) is
tried as given before the suffixed names, and a bare name gets each suffix. An empty or
relative entry names the current directory too, and is skipped. The agent's shell starts in a
directory of its own, so a file found there tells nothing about what the agent will run.

Every probe is bounded by `_PROBE_SECONDS` (15.0), and `subprocess.run` kills the child when the
time runs out. Its output goes to anonymous temporary files, not pipes (read up to 64 KiB each).
After the kill, `subprocess.run` on Windows drains the pipes, and a grandchild that still holds
them (a `.bat` wrapper's Python, say) would keep doctor waiting past the bound. With files there
is nothing to drain, so the bound holds on every platform. The files are closed and gone when the
probe returns. On Windows, a grandchild that outlives the kill keeps its handle until it exits;
the file lives in the system temp directory, never in the store or a config. stdin is closed. `PYTHONDONTWRITEBYTECODE=1` keeps the import from writing
bytecode caches into the install being checked (AC-06.4). `--version` is argparse's own action,
which prints and exits before any subcommand code runs. The probe uses this shell's
environment, not the client's (a Desktop entry's `env` is not applied). That is why a
successful probe says "runs engmem X" and nothing about the client.

## Templates, skills and the trigger rule

Each installed file must carry the stamp `install` writes (`<!-- engmem-template: <name>
v<version> -->`). A missing file is an error with the install command. A stamp from another
version, or no stamp at all, is a warning: the file works, but it may describe steps the
installed CLI no longer has. The trigger rule is `ok` in the current wording or in the user's
own wording (`TRIGGER_MARKER`, the same rule install uses to leave it alone). It is a warning in
an earlier engmem wording, because those searches go unattributed, and a warning when it is
absent. Without the rule the agent is never told to search, but the templates still work. For
Codex, an `AGENTS.override.md` is a warning, because Codex then never reads the rule.

## Accepted limits (owner, 2026-10-08)

These are accepted as documented above: a store outside Codex's `writable_roots` is a warning,
not `unverified`. Only MCP commands of the shape `install` writes are run. The store root is not
write-probed. Codex profiles and an MCP entry's own `env` are not resolved.

## Sandboxes: a host result is not a guarantee (AC-06.5)

Every check runs in the shell doctor was started from. A client that runs commands in its own
sandbox may be refused what this shell was allowed. So no client ever gets an `ok` for access
from inside it. Each run ends with an `unverified: sandbox:` line naming the client and the
store. For ChatGPT the line is `tunnel`: `tunnel-client`'s profile records its own
`--mcp-command`, and engmem does not read it.

Codex has a sandbox config engmem can read, so doctor reads it. The top-level `sandbox_mode`
and `[sandbox_workspace_write] writable_roots` come from `$CODEX_HOME/config.toml`, through the
same `_parse_toml` install uses. `danger-full-access` is `unverified`. `read-only` is a warning:
shell writes are refused. Otherwise, a store at or under one of the `writable_roots` is
`unverified`: allowed by config, not tested from inside. A store outside them is a warning,
because shell `engmem` commands cannot write drafts, saves or telemetry there under
workspace-write. The action is the one `install` already prints: add the store to
`writable_roots`, or approve each write. Codex profiles are not resolved, and the workspace
Codex starts in is not known here. A store inside that workspace is writable without
`writable_roots`, and the warning names that exception.

## Paths a config cannot hold

A JSON or TOML string can carry a NUL byte (`\u0000`); a path cannot. On POSIX
`os.path.realpath` raises `ValueError` on one, and without a check such a `--store` in an MCP
entry, or such a `writable_roots` entry, ended doctor (and `engmem store show`) with a traceback
and lost every line after it. On Windows the non-strict `realpath` returns the path unchanged
instead of raising (gh-106242), so a check that waits for the exception never fires there: the
store read as a plain mismatch and the raw NUL reached the terminal. So the check looks for the
byte in the string, before any `realpath`, on every platform. `install.wiring_verdict` returns
`WiringVerdict.INVALID` for that store, shared by both commands, and `_real` returns None for
such a root. Doctor reports it as an error, because the server cannot start with it;
`store show` prints it as a `mismatch:` line, so `store set` shows it too. An unusable
`writable_roots` entry is a sandbox warning. Every such line names the config file and shows
the value quoted, so the NUL byte never reaches the terminal raw.

## What doctor does not do

It fixes nothing. Every action is printed for the user to run (US-17 owns any guided
walkthrough). It writes one probe file and removes it. It creates no directory, rewrites no
config, saves no store choice and moves no document. It reads the configs, the templates, the
instructions file and the listing of `sessions/`, never a document's content.
`tests/test_doctor.py` compares byte snapshots of the whole sandbox, configs and store
included, before and after a run.
