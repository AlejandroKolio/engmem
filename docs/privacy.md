# What engmem touches on your machine

**No network.** No telemetry upload, no model calls, no fetching at runtime. Search,
ranking, and storage are entirely local. Two runtime dependencies, `pyyaml` and
`markdown-it-py`; neither opens a socket.

**Your store is private.** engmem reads and writes markdown under a directory you choose.
That store commonly holds notes about proprietary code — it lives outside this repository
and should stay that way. There is no encryption at rest; protect it the way you protect
the source it describes.

**Reads stay inside the store.** A link in `sessions/` or `versions/`, or `sessions/`
itself being a link, is refused by name and nothing behind it is read, so an imported or
shared store cannot hand the agent a file from elsewhere on your machine as a recorded
decision. The logs follow the same rule: `telemetry.jsonl`, `cost.jsonl` and
`feedback.jsonl` are never appended to or read through a link, so a search cannot write
into a file outside the store. The store directory itself may be a link, for example into a
synced folder.

**Writes stay inside `sessions/`.** The MCP write tools resolve every path first and
compare after, so `..` segments, absolute paths, symlinks pointing out of the store, and
ids containing separators or NUL bytes are refused. Writes stage to a temporary file and
re-validate before the document appears, so a crash mid-write cannot leave half a document.
A new draft is published with a hard link, which refuses an id that already exists (where
hard links are unavailable, a lock does the same), so of two calls creating one id exactly
one succeeds. Two engmem calls rewriting one document take turns (`os.replace` under a
lock), and finishing a draft can name the version it read, so a stale copy is refused
instead of replacing newer text.

**`engmem export` writes one new file, outside the store, and only what you previewed.** It
needs the preview's fingerprint, refuses an existing file and a path inside the store, and leaves
the store byte-identical. There is no MCP tool for it, so an agent connected over MCP cannot take
a document out of the store. Spotting secrets in the text is not its job: read the preview.

**Two one-line setting files.** `~/.config/engmem/store` and
`~/.config/engmem/mode` (`$XDG_CONFIG_HOME/engmem/`; `%APPDATA%\engmem\` on Windows) are
written only by `engmem store set` and `engmem mode set`, each holding one line. Neither
`install` nor any other command writes them. Deleting a file returns that setting to its
default.

**`engmem search` runs read-only git commands.** When a hit has an anchor to compare, the
search runs `git rev-parse --show-toplevel HEAD` once in its working directory. When `HEAD` has
moved and the record lists `covers_files`, it runs one `git cat-file --batch` per such result,
which reads commits and trees only and never fetches. Each is bounded to 5 seconds, with git
taken from an absolute `PATH` entry, never from the project directory. It runs neither
`git status` nor `git diff`, because both can start a program the repository configures, so
uncommitted changes are not seen.

**`engmem doctor` changes nothing.** It reads configs, templates and the rows of
`telemetry.jsonl` and `cost.jsonl`, runs `engmem --version` and
`<python> -m engmem.cli --version` with the MCP entry's Python, and writes one probe file into
`sessions/`, removed straight away, even when interrupted.

**`engmem walkthrough` writes only inside the directory you give it.** It creates a demo
store, a demo Codex config and workspaces there, and runs `git init` in each, `engmem doctor`,
and for `walkthrough codex --verify` the acceptance check engmem ships, against the workspace's
`demo_orders`. `walkthrough handoff` also writes Claude Code's `.mcp.json` and
`.claude/settings.json` inside its `storefront/` workspace, and makes one commit per workspace
with your git hooks and commit signing switched off. The demo sessions leave two things
outside the directory, neither written by the command: Claude Code records the `storefront/`
folder's trust in `~/.claude.json` and the transcript under `~/.claude/projects/`, as for any
folder; and every search on the demo store (the printed `engmem search ... --store`, the agents'
own searches, the demo MCP server) uses the same `~/.cache/engmem` as your store and drops the
cached entries for your documents. Results stay right; your next search only rebuilds them. The
Codex sign-in the demo asks for stays inside, in the demo `CODEX_HOME`, and goes with the
directory.

**The cache is JSON, never pickle.** Token counts live under `~/.cache/engmem/`, which any
process running as you can write to. Unpickling from there would execute whatever it
contained; JSON cannot. A corrupted entry degrades to a cache miss. An entry is used only when
the sha256 of the document body a search just read equals the one stored with it, so an edited
document is never scored from its old text, whatever its size or modification time say.

**Installing touches known paths only.** `engmem install` creates the store when it is missing
and runs `git init` in it, writes templates into the agent directory you name, appends the
trigger rule to the instructions file of `claude`, `copilot-ide` and `codex` and, for
`claude-desktop` and `codex`, adds one server entry to that app's config. `engmem uninstall`
removes exactly what it added and never touches the store. The files engmem edits but does not
own — your instructions file and those configs, which hold every other MCP server you configured
— are replaced atomically, through any symlink rather than over it, so a failed write leaves
them exactly as they were.

**Two more logs beside `telemetry.jsonl`.** `feedback.jsonl` holds your assessments, and
`cost.jsonl` one line per search or `engmem read` response (its byte count, never its text) and
per baseline figure your client reported. A read, like a search, keeps a copy of the version it
showed under `versions/` and prints the `cite as` reference for it. A `--read-only` MCP server
writes no version copy and neither log; its searches still write telemetry rows.

One thing to know: `engmem search` prints matched document text to stdout. In a shared
terminal or a logged CI job, that text goes wherever the output goes.
