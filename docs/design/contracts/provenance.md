# Contract: snapshot anchors and the current checkout

Source: `src/engmem/provenance.py`, rendered by `src/engmem/output.py` (`_snapshot_line`). Added
by US-11. Before it, a record carried one `verified_at_commit`. That field could not say which
repository it belonged to once a story touched two. It was also never compared with anything,
so a search showed a three-month-old decision exactly like one checked this morning.

## The field: one anchor per repository

```yaml
repos: [platform-core, widget-cache-client]
verified_at: {platform-core: 9c1d2e4f..., widget-cache-client: 77ab01c3...}
```

`verified_at` maps a repository name to the commit its checkout was at when the decision was
checked. The names are spelled like `repos` (the checkout's directory name) and compared the
same way, by `repo_key`: case and surrounding space do not count. A value is a git object name,
7 to 64 hex characters (`COMMIT_ID_RE`): abbreviated or full, SHA-1 or SHA-256.

It is never a load gate. Each defect costs only what it touches, and each is a load warning:

- a value that is not a commit id (`nope`, `abc12`) keeps its entry. The search shows that
  repository as `unknown, its anchor is not a commit id`;
- a value that is not text is kept as unknown, `its anchor is not text`. Unquoted, an all-digit
  sha is an int to YAML and one with a leading zero is read as octal. The number YAML hands over
  may no longer be the commit that was written, so it is never turned back into one;
- a blank value is "no anchor", like a repository left out, and draws no warning. A save with
  no shell writes nothing here, and that is allowed;
- an entry with no name (blank, or YAML's null `~`, which would otherwise read as a repository
  called "None") is dropped, and a second spelling of one name keeps the first;
- a field that is not a mapping (`verified_at: [a]`) makes every repository of the record
  `unknown, verified_at cannot be read (see the load warning)`. A record that names no
  repository still gets that line, so the unreadable field is visible in the result.

## The old single field is used only when it is unambiguous

`verified_at_commit` is still read, by the same rule for a value YAML did not read as text:
unquoted, `0123456` is the octal int 42798, a number nobody wrote. Such a value is a load warning
and the anchor is `unknown, its anchor is not text` (`_legacy_anchor`). When a record has no
`verified_at` entries:

- with exactly one linked repository (after `repo_key`), the old value is that repository's
  anchor;
- otherwise it is shown on its own, `verified_at_commit abc1234: unknown`, with the reason: `the
  record names no repository`, `the record names N repositories`, or `its repository links
  cannot be read`. It is never guessed onto one of them; each linked repository is then shown
  as `no anchor recorded`.

When `verified_at` has entries, the old field is ignored. The templates no longer write it.

## The current snapshot: the checkout this session runs in

engmem knows no local path of any repository, and a mapping file of paths is more than a
one-line setting. So the current snapshot is the one checkout at hand: the git checkout the
process's working directory is in. Its identity is the top-level directory's name, the same
convention `repos` already asks the templates to follow. That checkout's `HEAD` is compared
with the anchor recorded for the repository of the same name. Every other repository is
`unknown, no checkout of it here, engmem's working directory is in <top-level path>`. An
agent working in `platform-core` sees `platform-core` compared, and `widget-cache-client`
said to be unchecked, rather than certified by the first.

The lookup is one `git rev-parse --show-toplevel HEAD`:

- **At most once per search, and only when needed.** `SessionCheckout` runs it the first time
  a shown block has a well-formed anchor to compare, and reuses the answer for the rest of
  that search. A store with no anchors never runs git. `compose` makes a new one per call, so a
  long-running MCP server sees `HEAD` move between calls.
- **git is found the way doctor finds a command** (`executables.on_path`, cited in
  `contracts/doctor.md` as `_on_path`): absolute `PATH` entries only, never the working
  directory, which is the project's.
- **Only `rev-parse`.** It reads refs and config and runs nothing from them. `git status` would
  tell a dirty tree apart, but it refreshes the index, and a repository's own
  `core.fsmonitor` names a program to run for that. So uncommitted changes are not seen: the
  comparison is between commits (Known limits).
- **The environment names this checkout.** `GIT_DIR`, `GIT_WORK_TREE` and the other
  repository-locating variables (`_REPOSITORY_ENV`) are removed. A git hook or a wrapper
  exporting them would otherwise answer for that repository instead.
  `LC_ALL=C`/`LANGUAGE=C` keep git's messages in English, because the reasons below are read
  from them; under `LANGUAGE=de` "not a git repository" arrives in German.
- **Bounded.** `GIT_LOOKUP_SECONDS` (5.0), stdin closed, output to temporary files like doctor's
  probes. A timeout, a git that cannot start, a directory outside any checkout, or a checkout
  with no commit yet becomes a reason. None of them is an exception, and none fails the search.

## The states, and what they do not say

Each repository gets one `RepoSnapshot` with a `SnapshotState`:

- `MATCH`: the anchor is a prefix of the checkout's `HEAD` (case-insensitive, so an
  abbreviated anchor matches its full commit). Shown as `<repo> <anchor>: same commit as HEAD`.
  This says that the code is the code the decision was checked against. It does not say the
  text is true (AC-11.1), and no wording on the line claims that ("verified", "valid").
- `DIFFERS`: `<repo> <anchor>: HEAD is now <head>, re-check`. The record keeps its rank and its
  place. It is not marked false, stale or superseded (AC-11.2); only re-checking is asked for.
- `UNKNOWN` always comes with a reason (AC-11.3): `no anchor recorded`, `its anchor is not a
  commit id`, `its anchor is not text`, `verified_at cannot be read (see the load warning)`,
  `no checkout of it here, engmem's working directory is in <path>`, `engmem's working
  directory <path> is not in a git checkout`, `git refuses this checkout: it is owned by another
  user`, `git is not on PATH`, `git did not answer within 5 s`, `git could not be run
  (<error>)`, `the checkout <name> has no commit at HEAD yet`, or `git rev-parse failed:
  <git's error line>`.

The reasons that name a place name engmem's working directory, the directory the process runs
in, not "this session": under a desktop client that is `/` or the home directory, and the
reader has to see that the search ran there. A path is shown to `_PATH_SHOWN_MAX` characters (60),
cut from the front, so the checkout's own name at the end survives.

**Only git's error line reaches the reason.** git prints advice around its error, and the
advice is addressed to a human at a shell. For a checkout owned by another user it is
`git config --global --add safe.directory <path>`, a command that turns git's ownership check
off. The reason is read by an agent, on every anchored hit, so taking git's last line handed it
that command. `_git_error` takes the first `fatal:` or `error:` line and never a hint; with
none, the reason is the exit code. The ownership refusal is matched first and given a fixed
reason (`DUBIOUS_OWNERSHIP`) that suggests no command at all: whether to trust a checkout
someone else owns is the user's call, not one engmem's output should prompt.

Commits are shown abbreviated to `COMMIT_DISPLAY_CHARS` characters (7, git's default); an
anchor that is not a commit id is shown escaped and cut at `ANCHOR_DISPLAY_MAX`.

US-12 refines `DIFFERS` with a check of the covered files. The state and `RepoSnapshot.current`
are kept as values for that reason, and the commit difference stays a visible fact next to
whatever that check concludes.

## Where it is shown

One `snapshot:` line per block, after `path:` (and after `repos:` in a scoped search), in every
hit, successor and `--role` block, on both channels through `search_report.compose`. The parts
are joined by ` | `, one per repository in `repos` order, then repositories named only in
`verified_at`, then an unattributed old anchor. One line per block, not one per repository:
a record linked to two repositories costs one line, and the two states still read separately
(AC-11.4).

A record that names no repository and carries no anchor gets no line: there is no repository
to state anything about, and an identical "unknown" on every such record would be noise.

Every name, anchor and reason goes through `_escape_controls`: the names come from a file and
the reason can carry a directory name, so either could otherwise forge a line.

The line is inside `_rendered`'s budget and so inside `context_bytes`: it is text the reader
pays for, and a trim cuts it like any other line.

**This changes the default output.** US-09 and US-10 kept a plain search byte-identical and put
new lines behind a flag (`contracts/output.md`). This line cannot be opt-in: the story is about
an old decision shown in an ordinary search, and a flag nobody passes would hide the "re-check"
exactly where it matters. So every record that carries an anchor or a repository link now has
one more line in a plain search, and `context_bytes` for such results is larger than in
telemetry rows written before. Records with neither are byte-identical. Owner decision
(2026-10-09): the `snapshot:` line is shown in the default search output, always.

## Recorded at capture

`/engmem.save` and `/engmem.save.quick` write `verified_at` with one entry per repository in
`repos`, each from `git -C <that checkout> rev-parse HEAD`. A repository with no checkout or no
shell at hand is left out, and a sha is never guessed. `/engmem` writes the draft with
`verified_at:` empty.

`engmem_complete_draft` never refuses a record over its anchors. Each `verified_at` and
`verified_at_commit` load warning is named in the result as `warning: sessions/<id>.md: <load
warning>` (`_snapshot_notes`), worded for its own case: a value that is not a commit id or not
text says the search shows it as unknown, a duplicate says the first anchor is kept, a nameless
entry says it is ignored. A missing anchor is not warned about: leaving it out is what the
templates prescribe without a shell, and the search shows it.

## Known limits

- **One checkout per session.** A story touching two repositories can be compared only in the
  one the session runs in. A `--repo-path` option or a path mapping would lift this. It was left
  out as more surface than the first slice needs.
- **The directory name is the identity.** A clone in a directory of another name, or a git
  worktree (`engmem-wt2`), reads as `no checkout of it here`. The answer is wrong in the safe
  direction: unknown, never a false match.
- **The MCP server's working directory is the client's choice.** Claude Code starts it in the
  project; a desktop client may start it in `/` or the home directory, where every anchor reads
  `engmem's working directory / is not in a git checkout`, or, for a home directory that is a
  dotfiles checkout, names that checkout's path.
- **Uncommitted changes are not seen**, for the `core.fsmonitor` reason above. A dirty tree at
  the anchored commit reads `same commit as HEAD`, which the wording claims and no more.
- **A short anchor is compared as a prefix.** Two commits sharing their first seven characters
  in one repository would read as a match.
- **The number of repositories on the line is not capped.** The 4 KB budget trims a block like
  any other; a record linked to dozens of repositories spends that budget on this line.
