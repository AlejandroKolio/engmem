# Contract: exporting one document

Source: `src/engmem/export.py`, called by `engmem export` in `src/engmem/commands.py`. Added by US-20.
Before it, the only way to share one decision was to copy the file out of `sessions/` by hand. That
copy carries the front matter's links to other documents, YAML comments, the Reuse Log's quotes
from other documents and whatever local paths the text mentions. The owner decisions of 2026-10-10
(D1, D2) shape the rules below.

## Two steps, bound by a fingerprint

`engmem export <id>` prints the preview: the whole file text, the list of what was cut, and a
fingerprint, the first 16 hex digits of the SHA-256 of the file's bytes (`staging.version_of`,
the same digest a document version uses). `engmem export <id> --out FILE --confirm <fingerprint>`
builds the file again from the store as it is now and writes it only when it hashes to that
fingerprint.

- **No prompt, no `--yes` (D1).** A `[y/N]` prompt confirms "an export", not the bytes that were
  read. The fingerprint confirms one exact file: if the document, a neighbour's title, the store
  path or the home directory changed between the two steps, the bytes differ and the write is
  refused. A `--yes` would be a way to write a file nobody looked at.
- **The refusal does not print the new fingerprint.** Printing it would let the next command
  confirm text nobody has read. The message asks for a new preview.
- **The fingerprint is not a secret.** Anyone who can run the command can read the preview. It
  proves that the bytes written are the bytes shown. It does not prove a human read them.
- **`--out` and `--confirm` go together.** One without the other is refused and writes nothing,
  so a forgotten `--confirm` can never act as a confirmation.
- The comparison ignores case and surrounding space: copying from a terminal is not a change.

## The file

UTF-8 with no byte-order mark and LF line endings, whatever the source used: `parse_document`
reads with `utf-8-sig` and universal newlines, and the export starts from the same text. The
fingerprint is of these bytes. The preview prints the same text, so on Windows the terminal shows
CRLF while the file holds LF; the preview header says which the file has. Control characters other
than a line break and a tab are escaped (`read_report._escape_body`, as `engmem read` does) in the
text itself, before hashing, so the preview and the file stay the same text and a terminal escape
in a document cannot rewrite the preview.

Setext `##` headings (`Title` over `---`) are written as `## Title`. The export finds sections the
way `split_sections` does (`sections.h2_spans`), and keeps the text it searched. The rendered
Markdown is the same.

## Front matter: an allow-list

The front matter is parsed and dumped again (`yaml.safe_dump`). It is never copied as text,
because a YAML comment is where a reason for a link lives (`# why we split from <id>`), and a
comment is invisible to a parser that decides what to cut. Only the fields below are kept, and a
field written with no value is left out, as `stated` reads it. Nothing derived is added: a
document with no `title` in its front matter exports without one, and the preview notes the
missing fields.

| Field | In the file | Why |
|---|---|---|
| `id`, `title`, `date`, `task_date`, `status` | kept | what the record is and when; `status` tells the recipient a decision was superseded |
| `tags`, `entities` | kept | the record's own vocabulary |
| `repos`, `verified_at`, `verified_at_commit`, `covers_files`, `branch`, `pr` | kept | where to check the decision: repository names, commits and repository paths |
| `author`, `backfilled` | kept | who wrote it, and whether it was written after the fact |
| `related`, `superseded_by`, `supersedes`, `navigation_miss` | cut | they name other documents in the store; `navigation_miss` also holds private search words |
| `mode`, `baseline_unavailable`, `capture_minutes`, cost fields and any other | cut | the owner's own measurement and anything engmem does not know; an unknown field fails closed |

Every cut field is listed in the preview with its value. Kept values go through the same cutting
as the body, so a `covers_files` entry written as an absolute path is cut. The document's own id
is never cut. Its own title is kept wherever no other document's title runs across it: a neighbour
sharing the title is not a title to cut, and a neighbour's title lying inside the own one
(`Widget cache` in `Widget cache eviction`) is not matched there, because the own title sits in
the same longest-first alternation and wins (`_titles`). A neighbour's title that starts inside
the own title and runs past it (`Cache eviction policy` in `Widget cache eviction policy`) is
found by a second search that sees overlapping titles (`_title_spans`, run once per text), and
then the own title is cut with it. The cut runs to the end of that neighbour title, and on through
any further title that starts inside the cut (`_crossed_end`), so `Widget cache eviction policy
for tenants` goes whole when `Eviction policy for tenants` is another document's title. A
reference that begins inside a cut and runs past it is taken by the same cut. The preview lists
each cut as exactly the text removed from the file. A wrong cut shows in the preview; a
neighbour's title left in the file would not.

## The body: what is cut

Each of these is replaced by `[excluded reference]`, and listed in the preview with the file line,
the section it sits in, its kind and the text that was cut. The preview is for the owner only, so
it may name a private title or path: that is what lets the owner judge the cut. The file never
holds it.

- **Another document's id.** Every id in the store other than the selected one, when it has the
  id shape (`DOC_ID_RE`, two or more hyphen-separated parts), plus every id the document itself
  points at (`related`, `superseded_by`, `supersedes`, `navigation_miss`, Reuse Log rows),
  whatever its shape. A one-word stem such as `notes` is cut only when the document points at
  it: cut everywhere, it would delete an ordinary word. Every neighbour's file stem is an id too,
  so `sessions/old-name.md` is cut when that file's `id` says something else, and a broken
  neighbour cannot leak through its own load error. Matching ignores case and takes whole words
  only, bounded by anything but a letter, digit or `-`: `sessions/<id>.md` and `<id>_v2` are cut,
  and `scratchpad` is not the id `scratch`. `_` ends an id because an id never contains one.
- **A versioned citation**, `<id>@<16 hex digits>`, also when the cited document is gone.
- **A `[[wiki link]]`**, whatever it names.
- **Another document's title**, ignoring case: the title of every document the selected one
  points at (the same sources as for ids), whatever its length, and the title of any other
  document when it has two words or more. This mirrors the id rule. A one-word title of a
  document nothing points at is usually an ordinary word (`Tuning`), and cutting every
  occurrence would make the record unreadable; a referenced one (`Layoffs`) is a reference. The
  longest title is tried first, so a title that extends another is cut whole.
- **A local path**: an absolute POSIX path under `/Users`, `/home`, `/root`, `/tmp`, `/private`,
  `/var`, `/Volumes`, `/mnt`, `/media`, `/opt` or `/srv`; a drive as Git Bash spells it
  (`/c/Users/...`, `/d/home/...`); a Windows drive path with either slash; a UNC share; the
  extended-length forms `\\?\D:\...` and `\\?\UNC\...`; `~/` or `~user/`; a `file://` URL; `$HOME`, `%USERPROFILE%` and the other
  home and temp variables; and the store path and the home directory as spelled on this machine,
  wherever they live (`_home_paths`). A path runs to the first space, quote, backtick, bracket,
  parenthesis or `|`.
- **The Reuse Log's content, whole.** Its rows quote other documents, and a quote is their text,
  which AC-20.2 keeps out. The heading stays, so the recipient sees a section was there. The
  sentence `Prior docs used: none.` names nothing and stays.

What stays, because the recipient needs it to check the decision: repository names, commit ids,
`https://` links such as a PR, repository-relative paths (`src/cache/WidgetCache.java`), and
routes such as `/api/v1/orders`. A path is cut only under the roots above, so `/usr/bin/env` stays;
it names nothing private.

## Which documents can be exported

- **A draft is refused.** A draft is a session still in progress: its Decision Log may not
  exist yet and nothing was reviewed at save. Finish it first. (`engmem read` opens a draft for
  this session, marked as not reviewed; sharing it with someone else is a different act.)
- **A superseded document is exported.** The decision and why it was replaced can be worth
  sharing. The file keeps `status: superseded`, so the recipient knows a newer decision exists;
  the successor's id is cut like any other reference. The preview says so in a `note:` line.
- **A document with an incomplete spine is exported** with what it states, and a note names what
  it does not state.

Only the selected document is read for content. The rest of the store is read for ids and titles,
to know what to cut; no neighbour's text reaches the file or the preview (AC-20.1).

## Writing the file

The file is staged next to `FILE` with mode 0600 (on POSIX; Windows has no such mode, and the file
gets the directory's permissions) and published with `staging.commit_new`, a hard
link that fails on any existing entry, a dangling link included. So an existing file is never
replaced, also one that appears while the file is being staged; there is no separate existence
check to race. Where hard links are unavailable,
`commit_new` falls back to its lock, as for a new document. A failed write removes the staged file.
On POSIX the file stays 0600: it is a copy of private notes until the owner hands it over.

Refused, with nothing written:

- a directory that does not exist; engmem creates none;
- a `FILE` inside the store. `_inside` compares the target directory and each of its parents, by
  file identity after links (`os.path.samestat`), with the store and with every directory in the
  store's root (`sessions/`, `versions/`, any other). So `..`, a link or junction into the store,
  another letter case on a case-insensitive disk, and a store whose `sessions/` is itself a link
  to another place (a synced folder) are all caught: writing into that place writes into
  `sessions/`, and the next search would report a duplicate id. An export in `sessions/` would
  become a document; anywhere in the store it would change the store. A directory deeper in the
  store that is a link elsewhere is not followed; engmem creates none;
- an existing entry at `FILE`.

A directory swapped for a link into the store between the check and the write is not caught. The
command runs as the owner on the owner's own paths, and closing that window needs `openat`-style
calls Python does not offer on Windows.

## The store does not change

Preview and export leave the store byte-identical (AC-20.3), and nothing is logged:

- **No version is retained.** `engmem read` and a search retain the shown bytes under `versions/`
  so a Reuse Log quote can be checked later. An export is not a source for a quote in this store.
- **No `cost.jsonl` row.** That log measures what engmem delivered into an agent's context. An
  export is a file for a person, outside any session.
- **No `telemetry.jsonl` row and no export log.** A log line would make the store differ after an
  export, which AC-20.3 rules out, and nothing reads such a log. If the owner later wants a record
  of what left the store, that is a separate decision.

## No MCP tool, no network

There is no `engmem_export` tool. A client connected over MCP gets exactly the tools the server
lists, and a tool that writes a file outside the store would let an agent take a document out of
the store on its own. The CLI cannot tell a person from an agent with a shell; for that agent the
two steps still bind the write to one previewed text, and the file only goes as far as `FILE`.
engmem sends it nowhere: an unconfirmed export writes nothing, and engmem makes no network call
(`tests/test_no_network.py` covers this module like every other).

## Known limits

- **Prose is not judged.** A neighbour quoted outside the Reuse Log, a private name, a customer,
  a secret: none is detected. Finding secrets is out of scope for US-20. The preview is where
  the owner reads the whole text before confirming.
- A path with a space is cut up to the space; the preview shows the rest.
- A bare id of a deleted document (`sessions/20250101-deleted-doc.md`) is not known to the store
  and stays, unless the document points at it. Its versioned form is cut by shape.
- A relative path (`../Users/someone/secret.md`) is not recognised as local, in the body or in a
  kept field such as `covers_files`, where a relative path is the expected repository path.
- Windows has no 0600: the file gets the permissions of the directory it is written in.
- A relative link to a file outside the store (`../notes/plan.md`) is not a store reference and
  stays.
- A Reuse Log written as a `###` heading inside another section is not found as a section. Its
  versioned citations are still cut; its quotes are not.
- There is no flag to keep a cut. A wrong cut is fixed by editing the document and previewing
  again.
