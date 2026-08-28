# Contract: store resolution and failure reporting

Source: `src/engmem/runtime.py`. The resolution *order* — `--store PATH`, then `ENGMEM_HOME`,
then `~/Developer/engmem` — is stated in `ENGMEM-SPEC.md` §3 and is not re-argued here. This
file records the decisions that order alone does not settle: what counts as a setting, what
shape the answer takes, what happens when that shape cannot be produced, and why a failure is
printed twice.

## A blank setting is not a setting

`ENGMEM_HOME=` is the ordinary result of `export ENGMEM_HOME="$SOME_UNSET_VAR"`, and
`--store ""` is the same slip one layer up. Neither names a directory, so both fall through to
the next source rather than being honoured. `ENGMEM_HOME="   "` is the same mistake wearing a
space: without this rule it resolved to a directory literally named `"   "` next to whatever
the shell's working directory happened to be, and `engmem install` would `git init` there and
start writing documents into it — a wrong store that reports nothing wrong (§10, principle VIII).

The value itself is never trimmed. `--store "notes "` is a legal POSIX path and stays one;
only a value that is *entirely* blank is discarded. Silently retargeting a path because it ends
in a space would be the same class of bug in the other direction.

## The answer is absolute, and it is not resolved

**Absolute**, because the resolved path outlives the process that computed it. `engmem install
--agent claude-desktop --store ./notes` writes that path into `claude_desktop_config.json`, and
Claude Desktop later launches the server from a working directory of its own choosing — a
relative path there names a different directory every time, or none. `_claude_desktop_mcp_entry`
already spends `sys.executable` rather than a bare `engmem` for exactly this reason; the store
argument sitting beside it had the same exposure. The same applies to every diagnostic that
names the store: `store not found: sessions` sends the reader nowhere.

**Not resolved**: the absolutising step below, never `Path.resolve()`. The reason is what the
user reads, not what the guards check.

- A symlinked store is a supported setup — `~/Developer/engmem` pointing into a synced folder
  or a dotfiles checkout — and the user is entitled to see the path they configured in the
  messages engmem prints back to them, not its canonical form.
- No security invariant rides on this choice. `cli._sessions_is_contained` and
  `mcp_server._resolve_sessions_dir` compare `(store / "sessions").resolve(strict=True)`
  against `store.resolve(strict=False) / "sessions"` — they re-resolve the store themselves,
  and `Path.resolve()` is idempotent, so a store handed to them already resolved leaves *both*
  halves of that comparison exactly as they were. A `sessions/` linking out of the store is
  caught either way. Whoever revisits the absolutising step versus `resolve()` here is trading
  the path the user typed against its canonical form, and nothing else.

The absolutising step also leaves `..` segments alone, where `os.path.abspath` would normalise
them lexically and, through a symlinked parent, name a directory that is not the one the kernel
would reach.

## Absolutising is hand-ported, not `Path.absolute()`

`resolve_store` and `default_store` both funnel through a private `_absolute()` rather than
calling `Path.absolute()` directly, because that method has no drive-letter branch before
Python 3.13 (bpo-89812; the fix added `if self.drive: cwd = os.path.abspath(self.drive)` —
"There is a CWD on each drive-letter drive"). `pyproject.toml` declares `requires-python =
">=3.11"` and CI runs `windows-latest` against 3.11, so this is a real interpreter this project
ships on, not a hypothetical one.

Concretely: `PureWindowsPath('D:/work', 'C:notes') == PureWindowsPath('C:notes')` — a
drive-letter component re-anchors whatever came before it — and `is_absolute()` is `False` for
a drive-relative path like `"C:notes"`. Before 3.13, `.absolute()` built its answer with
`_from_parts([cwd] + self._parts)`, a flat reparse that lets that same re-anchoring erase the
`cwd` it was supposed to prepend whenever the process's cwd is on a *different* drive than the
one `--store` named — `resolve_store("C:notes")` came back relative, silently. `install
--agent claude-desktop` depends on the "answer is always absolute" invariant above for exactly
the process-starts-from-its-own-cwd reason already given; a drive-relative store there is a
wrong store that reports nothing wrong.

`_absolute()` looks up the *right* cwd itself — `os.path.abspath(path.drive)` when `path`
carries its own drive, `os.getcwd()` otherwise — then joins with the ordinary `/` operator,
which has always been drive-aware (`PurePath.joinpath`, not the flat reparse `.absolute()` used
before 3.13). This matches what 3.13 does natively; the difference is that every supported
interpreter now takes the same branch instead of only 3.13+.

## An unresolvable `~`/`~user` does not raise

`Path.expanduser()` raises `RuntimeError` — it does not fall back — when the leading `~` or
`~user` component cannot be resolved: no `HOME` and no passwd entry for the uid (a container
running as an arbitrary uid), or a `~user` that does not exist (`--store "~teammate/engmem"`
typed for a teammate who isn't on this machine). Nothing between `resolve_store` and
`cli.main` catches it, so an uncaught `RuntimeError` would surface as a bare traceback on
stderr — with stdout empty. That is precisely the failure this module's other rules exist to
prevent: the consumer is an agent that reads stdout and never reads stderr (§10, principle
VIII), so a diagnostic on stderr alone is a swallowed error.

`resolve_store` and `default_store` both funnel the expansion step through a private
`_expand_home()` that catches `RuntimeError` and returns the path *unexpanded* instead. The
literal `~nosuchuser12345/notes` (or, for `default_store`, the literal `~`) then falls through
the rest of the pipeline as an ordinary relative path component, gets made absolute against the
cwd like any other relative input, and — because it does not exist — is named by the
already-existing `store not found: ...` diagnostic (`cli.py`'s `_load_sessions`, which calls
`fail()` below) exactly as a mistyped `--store` would be. `resolve_store` itself stays pure: it
never raises for this input and never has to guess whether the caller wants a fatal error, a
skipped item, or an interrupt — that choice already belongs to the command, per the next
section.

## A failure is printed to both streams

`fail` writes the same line to stderr and to stdout. That is not a debugging leftover: the
consumer is an agent that reads stdout and never reads stderr, so a diagnostic on stderr alone
is a swallowed error (§10, principle VIII; §5 states the same rule for the CLI's own usage
errors). stderr keeps the line where a human at a terminal expects it.

It returns `None` rather than exiting. The exit code belongs to the command, which knows
whether the failure it just reported is fatal (`return 2`), one document out of a batch
(`_cmd_backfill` counts it and carries on), or an interrupt (`return 130`). A `SystemExit`
inside `fail` would take that choice away from every caller at once.

The one path that must never call it is `engmem mcp`, where stdout is the JSON-RPC channel and
a single plain line corrupts the frame in flight (§5). That is a structural rule, not a
convention: `tests/test_stdout_is_protocol_only.py` reads `mcp_server.py`'s syntax tree and
fails if the module so much as imports `fail`.

## `~/Developer/engmem` is the default on every platform

`Developer` is a macOS convention, and the default is deliberately not per-platform anyway.
The path is quoted verbatim in `ENGMEM-SPEC.md` §3, `README.md`, `docs/design/data-model.md`,
the `--store` help text, and the `engmem.start` prompt template the agent reads at the start of
every session. A store is a git repository the user already has; moving the default would
silently point an existing install at an empty directory to buy a platform nicety that
`ENGMEM_HOME` already provides.
