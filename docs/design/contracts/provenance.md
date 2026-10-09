# Contract: snapshot anchors and the current checkout

Source: `src/engmem/provenance.py`, rendered by `src/engmem/output.py` (`_snapshot_line`). Added
by US-11; the covered-files check by US-12. Before it, a record carried one `verified_at_commit`. That field could not say which
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

US-12 refines `DIFFERS` with a check of the covered files (next section). The commit
difference stays a visible fact next to whatever that check concludes (AC-11.2).

## The covered files (US-12)

A commit difference alone asks for a re-check on every new commit, most of which never touch
the code a decision rests on. So when a repository is `DIFFERS` and the record lists
`covers_files`, those files are compared between the anchor and the checkout's `HEAD`, and the
result (`RepoSnapshot.covered`, a `CoveredCheck`) replaces the bare `re-check`:

- `UNCHANGED`: every covered path was found at the anchor and has the same mode and object id
  at `HEAD`. A mode is compared as the octal number git reads, not as its text: older tools
  wrote a directory as `040000` instead of `40000`, and the same subtree under either spelling
  is the same directory. A mode that is not octal makes the answer unreadable. Shown as `HEAD is now <head>, covered files unchanged`, with no `re-check`: a
  change in another file is no signal (AC-12.1). It says the files are byte-identical, not
  that the decision holds.
- `CHANGED`: a covered path has another object id or mode at `HEAD`, or a tree that was read
  no longer has it. Shown as `covered file changed: src/a.py, re-check`, a deleted one as
  `src/b.py (deleted)`, in `covers_files` order, at most `COVERED_DISPLAY_MAX` (3) named and
  then `and N more` (AC-12.2). A rename reads as the old path deleted. The record keeps its rank;
  nothing on the line says it is false.
- `UNKNOWN`: `covered files not checked: <reason>, re-check` (AC-12.3). No fresh check is
  claimed, so the re-check stays.

A change wins over an unknown: when one file changed and another could not be compared, the
change is what the reader must look at, and review is asked for either way.

**Which repository the paths belong to.** `covers_files` is one flat list and stays one; a
per-repository mapping would make every engmem that predates it fail to load the record
(`_coerce_list_field` refuses a mapping). So the paths are read as files of the record's one
repository: the repositories named in `repos` and `verified_at`, compared by `repo_key`. With
two or more the check is not run, `covers_files does not say which of the record's N
repositories each file is in`; with unreadable links, `its repository links cannot be read`.
Like the legacy anchor, a file is never guessed onto the checkout at hand. A repository that
is not this session's checkout is `UNKNOWN` already and is never compared.

**A path is a name looked up in git's trees, nothing more.** Each entry, stripped (a blank
one is no entry), is a path from the top-level directory, `/`-separated, as `git ls-files`
prints it. It is never a pathspec (so `src/*.py` and `:(glob)…` are literal names) and never
touched on disk. An entry that is absolute, has an empty, `.` or `..` component, or holds a
control character is `covered file <path> is not a path inside the repository`
(`_is_repository_path`) and is not sent to git: `<rev>:./x` and `<rev>:../x` are read relative
to the working directory, and a line break would split one request into two. A directory
entry is compared as its tree, so any change below it is a change. More than
`MAX_COVERED_FILES` (100) entries are not checked at all.

**A deletion is claimed only where absence was seen.** git reports an object it cannot read
(a loose object it may not open, a partial clone's missing tree) exactly as it reports a path
that is not there: `missing`, with exit code 0. So the check never asks git for the file. It
asks for every directory on the way to each covered path at both commits, and walks them
itself (`_entry`): a path is absent only when a tree that *was* read lacks the next name, or
names a file where a directory should be. A tree that was named by its parent and could not be
read is `<dir> cannot be read at commit <sha>`, never a deletion. At the anchor, absence is
`<path> is not in commit <anchor>`: a bare file name (`WidgetCache.java` for
`src/.../WidgetCache.java`), a typo, or a file added later are all unknown, because the anchor
is the version the decision was checked against and it has no such file.

**One git call, which runs nothing the repository configures.** For each `DIFFERS` block with
covered files, one `git cat-file --batch` (`_run_cat_file`) is fed `<anchor>^{commit}` and then
`<commit>^{tree}` / `<commit>:<dir>` for every directory needed at both commits; the answers are
parsed as git's binary tree format (`_tree_entries`). It reads commits and trees only, never a
blob, so a blobless clone (`--filter=blob:none`) is checked without fetching. Chosen over
`git diff`/`git diff-tree` because those read the index, and reading the index runs a
repository's `core.fsmonitor` program: a probe repository configured with a marker script ran
it on a plain `git diff-tree <a> <b> -- <path>`. `cat-file` without `--textconv`/`--filters`
consults no diff driver, filter, pager, hook or fsmonitor; the test configures all of them to a
marker and asserts it never runs. A partial clone lacking a tree would lazily fetch it over a
transport its own config names (`core.sshCommand`, `ext::`, `uploadpack`), so the environment
carries `GIT_NO_LAZY_FETCH=1` and an empty `GIT_ALLOW_PROTOCOL`. From git 2.44 the first keeps
the tree missing, and it reads `the top-level directory cannot be read at commit <sha>` (or the
directory's name). An older git (Ubuntu 22.04 ships 2.34, Debian 12 2.39) ignores it; there the
empty allow-list alone refuses the transport before anything connects, and the line reads `git
cat-file failed: fatal: transport '<name>' not allowed`. Either way nothing is fetched and no
configured program runs; the tests pin each variable on its own. Same as the lookup: absolute-PATH git, repository-locating variables
removed, `LC_ALL=C`, `GIT_LOOKUP_SECONDS`, files instead of pipes, git's error line only
(`git cat-file failed: <line>`). `compare_covered` refuses an anchor or a `HEAD` that is not a
commit id before asking git: an empty one would make `<commit>:<dir>` the index path `:<dir>`,
and reading the index is what runs `core.fsmonitor`.

**Bounded per search.** A `cat-file` runs for each shown block in `DIFFERS` with covered files:
hits, successors and `--role` blocks, so up to a few per search. A git that times out or cannot
start is remembered in `SessionCheckout` for the rest of that search, and every later block gets
the same reason without another call. A stalled git therefore costs one `GIT_LOOKUP_SECONDS`
for the lookup and at most one more for the covered files; a git that is merely slow, but
answers inside the bound, is asked once per block.

The answer is checked before it is believed: `commit <anchor> is not in this checkout` (a
shallow clone, another history, a fake sha), `the anchor <a> is ambiguous in this checkout`, `the
anchor <a> names a branch or tag here, not a commit` (git resolves a short hex name to a branch of
that name before the commit; the resolved commit must start with the anchor), `git printed
output engmem cannot read` (a record that does not parse, or bytes after the last one), and
`the covered directories are too large to compare` past `_COVERED_OUTPUT_BYTES` (8388608 bytes, 8 MiB).

**Only a new anchor clears a review (AC-12.4).** Nothing is stored: the state is computed from
the anchor, `HEAD` and `covers_files` on every search, so finding or reading the record again
shows the same `re-check`. It goes away when the file is back to its anchored content, or when
someone re-checks the decision and writes the new commit into `verified_at`, which then reads
`same commit as HEAD`.

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
Both templates ask for `covers_files` as paths from the repository's top-level directory, the
way `git ls-files` prints them, and say that a record of several repositories is not compared.

`engmem_complete_draft` never refuses a record over its anchors. Each `verified_at` and
`verified_at_commit` load warning is named in the result as `warning: sessions/<id>.md: <load
warning>` (`_snapshot_notes`), worded for its own case: a value that is not a commit id or not
text says the search shows it as unknown, a duplicate says the first anchor is kept, a nameless
entry says it is ignored. A missing anchor is not warned about: leaving it out is what the
templates prescribe without a shell, and the search shows it.

## Owner decisions for US-12 (2026-10-09)

- A record linked to several repositories stays `covered files not checked`; `covers_files`
  stays a flat list, and binding files to repositories is a later story.
- A bare file name in an older record (`WidgetCache.java`) reads as unknown, `<name> is not in
  commit <sha>`, and is never searched for elsewhere in the tree.
- A deletion is shown in the changed list as `<path> (deleted)`, under one label.
- One `git cat-file` per block that needs it, not one per search.

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
- **Covered files are compared between commits.** An uncommitted edit to a covered file is
  not seen, for the same `core.fsmonitor` reason.
- **One repository per record for covered files.** A record linked to two repositories gets
  `covered files not checked`; a per-repository form of `covers_files` is a later slice.
- **Only the listed files.** A dependency, a caller or a config the decision also rests on is
  not compared unless it is listed; whether the decision still holds is never judged.
- **The number of repositories on the line is not capped.** The 4 KB budget trims a block like
  any other; a record linked to dozens of repositories spends that budget on this line.
