# Contract: the end-to-end agent harness

Source: `tools/agentsim/` (the harness), `tools/e2e.py` (its command line) and `tests/e2e/`.
The harness emulates how each kind of agent uses engmem from the start of a task to the save, and
measures what engmem costs and what it gives against the same agent without it. It tests engmem
as shipped: it adds no command, flag, MCP tool or front-matter field, and it drives only the
public entry points (the installed copies, the `engmem` CLI, `engmem mcp` over stdio).

## Two layers

- **Layer 1, deterministic emulation.** Scripted agents follow their rendered template step by
  step. A fixed, declared policy stands in for the agent's reasoning. It runs in CI.
- **Layer 2, live agents.** A runner starts real agents headless on the same scenarios. It never
  runs by default, and the shipped default is a dry run (see "Layer 2").

## Safety

Every run lives in a sandbox under its own temporary root. `HOME`, `USERPROFILE`, `APPDATA`,
`LOCALAPPDATA` and seven XDG variables (`XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`,
`XDG_STATE_HOME`, `XDG_RUNTIME_DIR`, `XDG_DATA_DIRS`, `XDG_CONFIG_DIRS`) point inside it.
`ENGMEM_STORE`, `ENGMEM_HOME`, `CODEX_HOME` and `CLAUDE_CONFIG_DIR` are removed, and git reads an
empty global config.

Every `--out` directory and every sandbox root must lie inside an allowed root: the repository's
`.tmp/` or the system temp directory (`tempfile.gettempdir()`, resolved). Anything else is refused
before anything is created, unless `tools/e2e.py` is given `--allow-out <path>` for it; that flag
never opens a protected root.

Protected roots are a second layer, refused even inside an allowed root. They are resolved once, at import, from
the environment the harness was started in, the way engmem resolves its store: the store engmem
would use there (`ENGMEM_HOME`, else the saved choice, else the default), engmem's default store,
`ENGMEM_STORE` when set, and every path in `ENGMEM_E2E_PROTECT` (separated by the platform's path
separator). A path is inside a protected root when its parts match the root's ignoring letter
case, or when it or an existing ancestor is the same file as the root, so neither a case variant
on a case-insensitive filesystem nor a link reaches a protected root. `tools/e2e.py` also refuses
a run whose stores, with their sandbox copies, would leave less than 2 GB free on the disk.

## Agent profiles

| profile | agent it stands for | how it calls engmem | how it saves |
|---|---|---|---|
| `shell-claude` | Claude Code | `engmem install --agent claude`; reads `~/.claude/commands/engmem.md`; CLI via subprocess | one git call, then `engmem draft create` / `draft complete`, content on stdin |
| `shell-codex` | Codex, shell only | `engmem install --agent codex`; reads `~/.agents/skills/engmem/SKILL.md` | as `shell-claude` |
| `shell-copilot-cli` | Copilot CLI | `engmem install --agent copilot-cli`; reads `~/.copilot/skills/engmem/SKILL.md` | as `shell-claude` |
| `mcp-shell` | Claude Code with the MCP server | `engmem mcp` over stdio: `prompts/get`, `tools/list`, `tools/call`; the shell for `store show`, git and the clock | `engmem_create_draft` / `engmem_complete_draft` |
| `mcp-only` | Claude Desktop, ChatGPT | the server entry `engmem install --agent claude-desktop` wrote, launched as the host does | MCP tools only; no clock, no git, so `capture_minutes`, `verified_at`, `author` and `branch` stay blank |
| `mcp-read-only` | a bridge client | the same entry with `--read-only`: the two search tools | none; the harness records what the save template tells it |
| `paste` | an IDE chat with no terminal | `engmem install --agent copilot-ide`; prints the search command; the simulated user runs it and pastes the output | none; the simulated user never asks for a hand-written file |
| `grep` (blind G) | the same agent without engmem, kept for continuity | `rg -c` over the same documents as plain files, whole-file reads in count order | none |
| `grep-content` (G) | the same agent without engmem | `rg -n -i -C1` over the documents, lines shown; ranged or whole-file reads | none |
| `grep-guided` (G+) | G given engmem's advice as an instruction | as G, plus a fixed instruction paragraph and the reads and `git log` it asks for | none |
| `grep-notes` (GN) | G whose earlier session wrote its own notes | as G, over the documents plus the free-form note its earlier agent wrote | one fixed instruction: write a short Markdown note |
| `no-history` (N) | the same agent with nothing | nothing | none |

The arms of the phase 4 comparison are E = `shell-claude`, blind G, G, G+, GN (class 8 only) and N.
The comparison of record is E against G+: G+ − G measures the ritual, E − G+ the tool. A claim must
also hold against G. Blind G stays, labelled, so earlier reports remain comparable; no claim rests
on it.

Every engmem profile first reads the start template and applies its scope gate (below,
"Policies"). Shell profiles then run `engmem mode show` (step 0). In daily mode, the default, the
start writes nothing: the profiles search and read with no session id and create no draft. The
save follows the daily save template literally: one git call for the front-matter facts (shell
profiles), then `engmem draft create` with the front matter and `engmem draft complete` with the
document, or `engmem_create_draft` and `engmem_complete_draft` over MCP. In research mode a profile
runs the research steps as before: `engmem store show` and `engmem draft create` (step 2) at the
start, the session id on every search and read, and the research save. Stdin stands in for the
template's quoted heredoc, so no profile needs a POSIX shell and every profile runs on Windows. The three shell
variants differ only in the installed copy. By default `shell-codex` and `shell-copilot-cli` run
class 1 only; `--every-profile` runs them everywhere. Real Codex also has the MCP server and its
template prefers MCP for the draft; that path is the `mcp-shell` profile. Each MCP request times
out after 60 seconds, so a hung server fails the run instead of hanging it.

## Policies

Declared once, applied to every profile and condition, and never read from the answer a run is
scored on. A policy that is wrong for engmem stays wrong in the report.

- **Mode.** Daily, the default; no Pre-reg, no baseline sub-agent, no draft before the save. No
  run sets research mode, so the harness measures the daily product; the research templates are
  held byte for byte by `tests/test_daily_templates.py` instead.
- **Scope gate.** A task whose description holds a word from a fixed list (`interview`,
  `practise`, `practice`, `study`, `homework`, `essay`, `blog`, `newsletter`, `marketing`,
  `holiday`, `vacation`, `recipe`, `birthday`) is not engineering work. An engmem profile then
  stops after reading the start template: no call, no draft, no search, no save. grep makes no
  call either. Any other task continues, as the template says to when unsure. The list stands in
  for the agent's reading of the task and decides classes 1–8 as engineering work.
- **Query.** The template's "key terms from the task": first every identifier the task names,
  verbatim and in order (backticked text, dotted names such as `a.b.c`, SCREAMING_SNAKE and
  CamelCase tokens), then its words minus a fixed stopword list, in order, at most eight words. The
  identifiers do not count toward the eight. Every arm uses this query, engmem and grep alike.
- **No role words.** The daily template's search and read steps name no section role beyond primer
  and decisions, so no arm derives a role from the task's words; the harness's earlier list of role
  words (`pitfall` → `lessons` and so on) was its own addition and is gone.
- **Successors.** engmem's search shows a superseded hit as a redirect line,
  `old: superseded by b → c`, followed by the end of the chain as a `### c (successor)` block. The
  policy reads a successor block like any result, together with the redirect lines that lead to it,
  and takes the chain's last id, so a redirect never sends it to a link that is itself superseded.
- **Relevance.** A result of the task query is relevant when its visible text shows at least two
  distinct query terms. A result of the component query (below) is relevant when it shows one
  component name, since an identifier is specific. A camelCase term counts when all its parts
  show. The rule is lexical: it cannot reject a document that shares the words and answers
  another question.
- **Second search.** Only when the first shows no relevant result: the names of the files the
  task touches, as the agent sees them in its checkout. At most two searches. `paste` never runs
  it: each search costs the user a round trip, and the template asks for one.
- **Second-search variant.** Class 2 runs twice: as above, and with the second search always run,
  for engmem and grep alike. The report shows both.
- **Load.** The template's rule: at most five documents in all, found documents first. Every
  relevant result is taken in rank order (a successor in place of a superseded result), the first
  search's before the second's, then the `related` ids of those at depth 1, until five. Each
  loaded document is read as the daily template's Read step names it: `--role primer`, then
  `--role decisions`, nothing else. A fact that lives only in Lessons Learned or Production
  Considerations (classes 3 and 13) therefore reaches E only through the search block's section
  line. Whether the daily template should also name Lessons is a template question for
  `src/engmem`, out of scope here. `mcp-read-only` cannot read; it calls `engmem_search_by_role`
  for decisions. `paste` loads nothing: the template gives a read no paste path.
- **Re-check.** When a loaded document's search block says `covered file(s) changed: <path>, re-check`
  (contracts/provenance.md), an engmem profile reads that file from its checkout, as the line asks.
- **Blind grep.** One `rg -i -c -e <term>...` over the documents, sorted by count and cut to the top 20
  lines with `head`. Its output names files and counts but shows no text to judge, so grep opens
  as many files, whole, in count order, as an engmem search shows results (three). A file whose
  front matter says `status: superseded` leads to its `superseded_by` file only when the file
  itself passes the relevance rule: the same gate engmem's load applies to a result before it
  takes its successor. The cap is the same five documents. The second search runs when no opened file shows two query terms.
- **Content grep (G).** One `rg -n -i -C1 -m 3 -e <term>... sessions | head -40`: the first three
  matching lines of each file with one line of context, files ranked by how many distinct query
  terms their matching lines hold, then by matching lines, then newest first, cut to 40 lines. The
  relevance rule judges each file by the lines that output shows of it, so G, like engmem, judges
  only what it sees. G opens the first three relevant files, five in all over both searches. A file
  under 4 KB is read whole. A larger one is located first (`rg -n -i -e <term>... <file>`, one call)
  and then read in ranges, one call per merged range: 30 lines either side of each match, or the
  first 60 lines when nothing matched. A file whose read text says `status: superseded` and passes
  the relevance rule leads to its `superseded_by` file, and from there link after link to the end
  of the chain (at most ten links; chain links do not count toward the five). The second search
  (the touched files' names) runs as for engmem.
- **Guided grep (G+).** G, plus one instruction paragraph injected at the start (`GUIDANCE` in
  `tools/agentsim/greparms.py`) with the advice the phase 4 brief lists as engmem's: check the notes,
  prefer the newest note on a subject, follow `superseded_by`, read Decision Log and Lessons Learned, and run
  `git log` on a note's `covers_files` since its `verified_at` commit for this checkout, re-reading
  any file that changed. G+ therefore opens relevant files newest first, its locating call also
  lists the headings, its ranges add the front matter, the Decision Log and Lessons Learned, and it
  runs `git log --oneline <commit>..HEAD -- <files>` for every opened note that names covered files
  and an anchor for its checkout, reading each file whole when the log is not empty.
- **Own notes (GN).** For class 8 the earlier agent, given one fixed instruction (`NOTES_INSTRUCTION`),
  writes a free-form Markdown note beside the documents: the decision, its reason and the files.
  It costs the instruction and the note it wrote. The finder then runs G over the documents and
  that note. GN runs class 8 only; elsewhere it would be G.
- **Save.** `/engmem.save.quick` for every task; `/engmem.save` for the class 8 saver. The
  decision is the task's known answer when its quote reached the context, else the naive one.
  A daily save lists in `related` the document the quote came from, and writes no Reuse Log,
  Search Trace or `capture_minutes`. In research mode a Reuse Log row is written only when the
  quote reached the context; it cites the `cite as` reference of the read that returned the
  quote, else of the search block. When completion
  refuses the row, the run keeps the attempted row, scores its citation invalid, and completes
  again with `Prior docs used: none.`. The simulated user confirms the preview, answers no to
  the supersede question and gives no usefulness feedback.

## Scenario classes

Classes 1–9 have one built-in task each, with its declared winner written before any run. Classes
10–13 are corpus generators: their documents are in every store, their tasks are authored
instances (below), and the pre-registered rules of Layer 2 decide them, not a declared winner.

| class | what it tests | declared winner |
|---|---|---|
| 1 | answer exists, keyword-findable | engmem and grep both beat no history |
| 2 | answer exists, wording gap | engmem over grep; both over no history |
| 2, variant | the same, with the second search always run | engmem and grep both reach the answer |
| 3 | answer in a non-primer section | engmem (role search) over grep (whole files); declared when the harness searched by role, which the daily template never asked for and the harness no longer does |
| 4 | stale trap: the old decision was superseded | engmem (redirect) over grep |
| 5 | no relevant history | no history; engmem should cost little and say none found |
| 6 | cross-repository answer | engmem and grep tie |
| 7 | larger store (413 documents; 3019 in the large tier) | engmem over grep on cost on a larger store |
| 8 | one agent saves, another agent kind finds and cites it | engmem; without it nothing was saved |
| 9 | non-engineering task (study for an interview) | engmem skips at its scope gate: no draft, no search row, no store write |
| 10 | hard stale: six supersede chains of depth 1, 3 and 6, the successor with the same words or renamed (new component and setting names, none of the old words); each stale value sits in its link's Decision Log, never its primer | decided by the rules |
| 11 | freshness: six notes in the `meter-service` checkout, `verified_at` at commit A and `covers_files` naming one source file; in variant (a) a later commit changed the value in that file, so only the code holds the answer; variant (b) is the unchanged control; three of each | decided by the rules |
| 12 | near-duplicates: five independent groups of 20 to 40 notes, each on one component and topic over two years; every third older note was never superseded; the newest is canonical and says so | decided by the rules |
| 13 | exact identifier: three config keys and three error strings, each verbatim in one body, outside the primer; grep is expected to tie or win | decided by the rules |

The class 8 saver is `shell-codex`, or `shell-claude` when the finder is `shell-codex`. It runs
once per saver kind; its store is copied into each finder's sandbox. Without engmem the saver
has no save path, so grep and no history have nothing to find. That assumes the agent keeps no
notes of its own, and it is engmem's structural advantage, not a retrieval result.

## Test data

`agentsim.corpus.generate` writes the stores from a fixed seed, so every run writes the same bytes.
The content is abstract: invented components, four invented repositories, no real names. The
hand-written core holds every answer, a superseded decision with its successor, a near-duplicate
of the class 1 answer, documents that share vocabulary and answer another question, and an
answer that lives only in Lessons Learned. Generated filler, full and quick shapes mixed, fills
the store. Filler on one component and topic forms a supersede chain: each document is
superseded by the next newer one on the same component and topic, so exactly one stays active
and no two active filler documents contradict each other. Random filler skips the component and
topic of the class 7 answer; that topic has one chain of short filler instead (two documents at
the default sizes, eight at the large tier), with different page sizes, whose last link the
hand-written answer `20260601-archive-exporter-page-size` supersedes. `tasks.json` records each task, its answer (document, section,
quoted value), the pitfall and the outcome without history. Every quote sits on one line of its
document, so a Reuse Log can cite it verbatim.

**Facts per class.** Every scored class (1–4, 6, 7, 10–13) holds at least five facts, each with its
own documents, entities and values: the hand-written core answer of classes 1–4, 6 and 7 plus four
generated ones each (`PLAIN_FACTS`; class 3's value sits in a Lessons Learned entry only, class 7's
facts are scored on the scale store), four more superseded pairs for class 4 (`STALE_PAIRS`), six
chains for class 10, six notes for class 11, five groups for class 12 and six identifiers for
class 13. Facts added after the first release come after the first ones, in the facts file and in
the generator's order, so the first ones keep their ids, values and documents; only class 11's
anchor commit changed, because the fixture gained two files. Classes 5, 8 and 9 are unchanged.

Sizes: the base store has 285 documents, the scale store (class 7) 625, the large tier 3231; each
holds the hand-written core, the generated fact documents and the generated filler.

**Document size.** `--doc-size 4kb | 12kb | 30kb` (default `4kb`) pads every full-shape document
that is smaller than the setting with realistic padding, unit by unit while the next unit still
fits: Kotlin settings code blocks in Code Implementation, step tables in System Architecture and
Testing Knowledge, and extra Tried/Failed/Fixed entries in Lessons Learned, in that rotation,
appended after what the section held. Padding is deterministic per document id and holds no digit,
so no regex of a numeric fact can match it. Quick-shape documents and documents already larger stay
as they are. The full-shape documents are 5.5–6.6 KB unpadded, so `4kb` leaves the store as
generated (mean 4089 bytes over both shapes), `12kb` gives a mean of 7.9 KB and `30kb` 19.5 KB.
Sizes are counted on the rendered text with `\n` line endings, which is also what is written, so
they do not depend on the platform.

**Class 11's checkout.** `meter-service` (six source files, one per note) is replayed into the sandbox from a fixed two-commit
history with a fixed author and fixed dates, so its commit ids are known before git runs:
`gitfixture.commit_ids` computes them from the trees (git's object format, no git needed), the
notes carry commit A, and the sandbox refuses a replay whose `HEAD` differs from the computed id.

## The facts file

`corpus.generate` writes `facts.json` beside `tasks.json`; `tools/e2e.py facts --out <dir>` writes
it alone. It is the blind author's only input. Format `engmem-e2e-facts/1`:

```json
{"format": "engmem-e2e-facts/1", "seed": 20261107, "doc_size": "4kb",
 "classes": {"10": "hard stale: ...", "...": "..."},
 "facts": [{"id": "f10-quotecache-refresh-period", "class": 10, "section": "decisions",
            "value": "95 seconds", "stale_values": ["10 seconds", "20 seconds", "45 seconds"],
            "entities": ["QuoteCache"], "repo": "tile-render", "doc": "20260620-quotecache-refresh-period",
            "topic": "the QuoteCache refresh period", "store": "base",
            "variant": "chain depth 3, successor with the same words", "identifiers": [],
            "covers_files": [], "source_file": "", "stale_docs": ["..."], "doc_size": "4kb"}]}
```

- `id`, `class`, `doc_size`: the fact, its class (1–4, 6, 7, 10–13) and the size setting.
- `section`: where the value lives: a role (`decisions`, `lessons`, `production`) of `doc`, or
  `code` when it lives in `source_file` at the checkout's `HEAD` (class 11, variant a).
- `value`: the current answer, verbatim on one line of its source. `stale_values`: values that
  were true once and are not now; each stays out of the current source.
- `entities`, `repo`, `topic`, `identifiers`, `covers_files`: what an author may name in a task.
  `repo` is where the fact was recorded; a class 6 task is worked in another repository.
- `store`, `variant`, `stale_docs`: where it lives and which variant of its class it is.

The same seed and size give the same bytes. Classes 5, 8 and 9 have no facts: they keep their
built-in tasks.

## Task instances

The harness consumes a task-instance file; it never writes the tasks it is measured on.
Format `engmem-e2e-instances/1`:

```json
{"format": "engmem-e2e-instances/1", "provisional": false, "author": "...", "note": "...",
 "instances": [{"id": "c04-paraphrase", "class": 4, "wording": "paraphrase",
                "task": "How many database connections should each report-hub instance keep open?",
                "required": ["\\b64 connections"], "stale": ["\\b200 connections"],
                "keys": ["f04-stale"], "repo": "report-hub", "files": ["src/report/ReportStoreClient.java"]}]}
```

- `class`: one of the fact classes. `wording`: `identifier` (names the component or key),
  `paraphrase` (says it in other words) or `symptom` (describes what goes wrong).
- `task`: the text the agent gets. `required`: Python regexes, matched case-insensitively, every
  one of which must match the answer. `stale`: regexes none of which may match it. `keys`: the
  fact ids behind the answer.
- `repo` (optional): the checkout the task is worked in; default the first key's `repo`. `files`
  (optional): files the task names, which the second search uses; without them there is none.

**The loader refuses** (`instances.InstanceError`) any unknown field, a missing one, a malformed
id or a repeated one, an unknown class or wording, an empty task or required list, a regex that does
not compile, a key not in the facts file or of another class, a required regex that matches no
keyed value, a keyed value no required regex matches, a required regex that matches the task's own
text (the task would give the answer away), a stale regex that matches a current value or no stale
value of its keys, and stale regexes that together miss any `stale_values` entry of the keys.
`instances.rejected` lists every refused instance with its rule, each judged on its own. Against the
generated store, at every size setting (4kb, 12kb and 30kb) and before anything runs, `phase4` and
`live` also refuse a stale regex that matches any line of a document other than its keys' own
documents and their `stale_docs` (`instances.stale_leaks`); the refusal names the instance, the
regex and the document. Before a run, every keyed value must sit on one line of its
source in the generated store and every required regex must match there (`answerable_problems`).
Layer 1 checks only this: that a task is answerable and its key correct. **Every instance in the
file runs**, in file order, under every arm; none is chosen or dropped by its outcome.

**Blind authoring.** An author who did not tune the templates writes the file from `facts.json`
alone, ten instances per class split across the three wordings, and the orchestrator runs that
author pass outside the harness. Its file is kept byte for byte as
`tools/agentsim/data/instances-blind.json` (100 instances, ten per class, `"provisional": false`)
and is the default of `phase4` and `live`; a rejected instance goes back to the author, never edited
here. The record of that pass (model, inputs, the prompt and the hashes of the file and transcript)
is `tools/agentsim/data/instances-blind.author.md`. `tools/agentsim/data/instances-provisional.json` holds two per
class marked `"provisional": true`, written by the harness implementer; the tests and the smoke
plan still use it; every report built from it says so in its first lines and supports no claim.

## Grading

A built-in task is scored as below ("Metrics"). An instance is graded by its regexes, against the
answer the arm would give, with the same rule for every arm:

- The context is split by source: each engmem search block and each `rg -n` output line belongs
  to its document, a read to the document or file it read.
- A document is flagged when the context marks it not current: a redirect line names it before the
  chain's end, a related line says it was superseded, its own text shows `status: superseded`, its
  engmem block says a covered file changed, or G+'s `git log` on its covered files printed a commit.
- The answer is everything the arm saw of the documents it opened (read), minus flagged ones; search
  output alone only points at a document. G+, whose instruction says to prefer the newest note,
  also drops a document when it opened a newer one on the same subject (the same title). E is
  graded literally: the daily template does not say to prefer the newest note, so E keeps every
  unflagged document it opened. The report shows E graded newest-first as a sensitivity row, never
  as its score. Code read from the checkout is always current.
- `correct`: every required regex matches the answer and no stale regex does. `stale avoided`: no
  stale regex matches it, counted only over runs that gave an answer (opened at least one document),
  with that denominator; N gives none and shows `n/a`. `stale seen` (reported, not scored): a stale
  regex matched anywhere in the context. `opened the answer document`: the arm opened a keyed
  fact's document, reported apart from `correct`, so finding the document is told apart from
  reading the section that holds the answer.
- Classes 3 and 13 carry the label "Daily template reads primer and decisions only; G+'s guidance
  names Lessons." in the report.
- The phase 4 report's cost of record is the mean over every run (cached turn tokens to the
  answer); the tasks both arms answered correctly appear only as a labelled secondary figure.

The rule cannot judge a document a real agent would reject on reading, or one it would trust
despite a flag; Layer 2 grades the agent's own `ANSWER:` text instead.

## Metrics

Per scenario × profile:

- **To the answer.** Everything that entered the context before the agent can answer: the start
  template the host injected (the installed copy with the task substituted, or the MCP prompt
  plus the `tools/list` definitions) and the output of the start, search and read calls; with
  the calls that produced it. grep and no history have no template. This is the headline cost.
- **Save.** The save template plus the save calls' output, and those calls, counted apart.
- **Speed.** Seconds per step and per run; user round trips (paste). engmem CLI steps include a
  Python interpreter start. grep and file reads run in-process with no process start, so their
  times are a lower bound.
- **Turn overhead.** Every tool call is a turn that re-reads the context so far, and one closing
  turn reads it all. `turn_tokens` sums those re-reads to the answer; `turn_tokens_cached` prices
  what an earlier turn already read at 0.1 and the new part at 1.25 (a prompt cache's read and
  write rates). `save_turn_tokens_cached` is what the save adds on top. The phase 4 report compares
  arms on the cached figure.
- **Break-even.** The reuses that repay one save: the save's cached turn tokens divided by what one
  reuse saves against G+ (or G), rounded up; `never` when a reuse saves nothing.
- **Tokens.** Layer 1 approximates: `ceil(UTF-8 bytes / 3.5)`, engmem's own estimate
  (`telemetry.estimate_tokens`). Layer 2 reads the agents' own reports.
- **Outcome.** `quoted`: the task's quoted value reached the context. `reached`: that and an
  answer document's id. `correct`: quoted and the stale trap avoided. `stale avoided`: the stale
  value did not reach the context, or it came with a superseded marker for its document.
  `pitfall avoided`: correct, or the pitfall itself reached the context. `none found said`:
  class 5 output says `prior context: none found`. `skipped`: the agent stopped at the scope
  gate. In class 9 an engmem run is correct only when it skipped and `store_written`, which
  compares a digest of every file in the store before and after the run, is false. `citation valid`: completion accepted the
  document with its row and reported no citation problem; a refused row scores no. `Reuse Log
  row`: the row cites an answer document and is classified `reuse`. Both are research-only and
  empty for a daily run; a daily run scores `related names answer`: its saved `related` names an
  answer document. `drafts_left` counts the session documents a run wrote that are still drafts.
- **engmem's ledger.** The bytes `cost.jsonl` recorded for the run's session beside the bytes
  the harness received. A daily run has no session id, so its ledger is the unattributed rows of
  its fresh store; a class 8 finder's store also holds the saver's rows, so it has no ledger. MCP rows must match exactly; CLI rows are larger by the stderr warnings
  and the closing `note:` lines the ledger does not count.

## How to read the report

`results.json` holds every step of every run; `report.md` holds the summary, a table per
scenario, the save cost, the template checks, the ledger cross-check and the store-size table. The
summary compares `shell-claude`, grep and no history on the to-answer cost, with engmem's save
cost in its own column. "Strong sides", "Weak sides" and "Ties" are computed from the runs, never
written by hand, and compare to-answer cost only: correct where the other is not; correct like
grep with fewer or more tokens to the answer (within 5 % is a tie); more calls to the answer than
grep; every scenario where engmem fails or another engmem profile misses what the shell finds;
and, on the weak side, the save each engmem run pays and grep never does. grep's search output
is cut to 20 lines, so its search bytes do not grow with the store; the store-size table shows
how many files it matched before the cut.

## Running it

- Default test suite: `tests/e2e/` is marked `e2e` and runs the smoke plan (every profile on
  class 1 but GN, which runs class 8 only; `mcp-only`, blind grep and no history on every
  scenario; `shell-claude` on classes 1, 4, 5, 8 and 9). `-m "not e2e"` deselects it. `ENGMEM_E2E_FULL=1` runs the full plan.
- Full default-size run with a report: `uv run python tools/e2e.py run`; it writes
  `results.json` and `report.md` under `.tmp/e2e/`.
- Large tier, opt-in: `uv run python tools/e2e.py run --large`, or
  `ENGMEM_E2E_LARGE=3000 uv run pytest tests/e2e/test_e2e_large.py`. The test records the
  outcome in its report and asserts only that every profile ran on the large store.
- The stores alone: `uv run python tools/e2e.py generate --out <dir> [--doc-size 12kb]`; the facts
  file alone: `uv run python tools/e2e.py facts --out <dir>`.
- Phase 4, every arm on authored instances: `uv run python tools/e2e.py phase4 [--instances <file>]
  [--doc-size 4kb --doc-size 12kb --doc-size 30kb]`; it writes `phase4-results.json` and
  `phase4-report.md`. With two sizes or more the report adds the E/G+ cost ratio per size for the
  role and stale classes (3, 4, 10) and where it crosses 1. `tests/e2e/test_e2e_phase4.py` runs its
  smoke plan (four instances, classes 4, 10, 11 and 13, and the class 8 handoff) by default.

Generated stores and results are never committed; `.tmp/` is ignored.

## Layer 2

`uv run python tools/e2e.py live [--instances <file>] [--stage pilot|mini|confirmatory]` prints the
plan of every stage with its estimate and the commands of one stage. It starts nothing.

**Arms.** E: the task as `/engmem <task>` (`$engmem` for Codex) in daily mode, then
`/engmem.save.quick` resumed in the same session. G: the task, the documents copied into the
checkout as `notes/`, and one sentence saying so. G+: as G with the `GUIDANCE` paragraph instead of
that sentence. N: the task alone. Every answering prompt ends with the same suffix: "Do not edit
files. End with `ANSWER: …` and `SOURCES: …`." The class 8 handoff runs E, GN, G and N: for E an
earlier session does the saver's task and saves before the finder starts in the same sandbox; for
GN the earlier session gets `NOTES_INSTRUCTION` (and the Write tool) and writes into `notes/`.

**Sessions.** Claude Code runs with `--output-format stream-json --verbose` (the tool calls are
captured), `--max-turns 25`, `--model <pinned>` (default `claude-opus-5-5`) and a per-arm
`--allowedTools`: E gets `engmem`, `git`, `date` and the read tools; G, G+ and GN get `rg`, `git` and
the read tools; N gets `git` and the read tools. Each session times out after 900 s. The model and
`<cli> --version` are recorded with every run. Codex has no turn cap; its runs say so.

**Order.** Blocks of agent × task × repeat run in a seeded random order (seed 20261111), and the
arms run in a seeded random order inside each block, so the arms interleave.

**Stages.** The pilot runs the first instance and the handoff once per arm. The mini run takes the
first instance of each class once. The confirmatory run takes every instance once. The dry run
also prices the confirmatory run at the planned ten instances per class and prints, per family,
its instances and minimum detectable effect. `--execute --i-accept-the-cost --pilot` runs the pilot
and writes `pilot.json`. Any later stage refuses unless `pilot.json` records, for every agent and
arm pair that stage plans (a Claude pilot does not unlock Codex), a start call that exited 0 with
non-empty usage, the pinned model as the model the agent reported, a parsed `ANSWER:`, no logged
violation and, for E, at least one `engmem search` or `engmem read` call (or the MCP tools
`engmem_search`, `engmem_search_by_role`, `engmem_read`) and a save call that exited 0 and added a
document to the store. It refuses
provisional instances: only the pilot may run on them.

**Grading and logs.** The answer is the text after the last `ANSWER:` line of the session's final
message, up to `SOURCES:`. The final message comes from a parser per agent: Claude's closing
`result` event when the session succeeded (an `error_max_turns` or other error result has none),
Codex's last `item.completed` agent message. There is no fallback to the transcript: a run with no
final message is incorrect and flagged (`final_message: false`). The answer is graded by the
instance's regexes as in Layer 1; `stale avoided` is empty for a run with no answer. A seeded 10 %
of the runs (at least one) goes to `audit-sample.jsonl` with an empty `human_verdict` for a human to
check the regexes.

Every path a tool call names (a Read, Grep or Glob path, or any path-like word of a shell command,
split at `&&`, `||`, `;`, `|` and line breaks) is resolved against the session's working
directory and logged in `violations` when it is not inside the sandbox (`Path.is_relative_to`, so
`sandboxes/10` is not inside `sandboxes/1`). For E, a command other than `engmem` naming a path
inside the store is logged too, segment by segment, so `engmem search … && cat store/…` is caught.
The corpus the sandboxes copy from keeps no answer key (`tasks.json` and `facts.json` are removed),
and the default results directory is under the system temp directory, outside the repository
that holds the instance files. Codex runs `--full-auto` with no tool restriction the harness can
set; every Codex run records `tools_restricted: false` and the dry run warns about it.

**Estimate.** Sessions times stated assumptions (an answering session: 150 s, 15 000 input, 300 000
cache-read, 40 000 cache-write and 5 000 output tokens; a save: 90 s, 5 000, 150 000, 20 000, 4 000;
no history: 90 s, 8 000, 120 000, 20 000, 4 000), priced at the pinned model's list price (Opus 5.5:
4 / 20 / 0.20 / 5 USD per million input, output, cache-read and cache-write tokens; Sonnet 5.5:
2 / 10 / 0.20 / 2.50; the published list prices at implementation time). The pilot replaces the
assumptions with measured usage. Every run that started counts toward correctness: a run with no
final message (max turns, a 900 s timeout, an error) is the arm failing the task and counts as
incorrect. A timed-out session keeps the output it printed before it was stopped, and its usage
is read from that partial output when there is any. A started run that reported no usage (a
timeout that printed nothing, for one) leaves only the cost ratio, never the correct rate. Only a
run that never started leaves the analysis. `analyse` prints, per arm, how many runs left the
analysis and how many left only the cost ratio; no run ever counts at $0.

A live agent does not inherit the parent environment. Its sandbox starts from `PATH`, `PATHEXT`,
`SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LANGUAGE`,
`LC_ALL`, `LC_CTYPE`, `TERM`, `COLORTERM` and `TZ`, adds only the variables named with
`--pass-env` (for example an API key), and sets the sandbox variables of "Safety". The removed
variables stay removed even when named with `--pass-env`.

Not yet verified, because no live run has happened: the exact agent flags, the stream-json event
shapes the parser reads, and whether an agent can authenticate under a sandboxed `HOME`. The pilot
exists to check them before anything larger runs.

### Pre-registered protocol

Written before any live run and not changed after one has started. The primary agent is Claude
Code. A family pools classes by what they test:

| family | classes | instances at 10 per class | MDE, SD 0.5 | MDE, SD 1.0 |
|---|---|---|---|---|
| retrieval | 1, 2, 6, 7 | 40 | 26 pts | 51 pts |
| stale | 4, 10, 11, 12 | 40 | 26 pts | 51 pts |
| section | 3, 13 | 20 | 36 pts | 72 pts |

The minimum detectable effect is the correct-rate difference a paired test detects with 80 % power
at the Holm worst case, two-sided α = .05 / 3, when the per-instance differences have the stated
standard deviation (1.0 is the bound for a difference of two yes/no outcomes). Per-class verdicts
would need at least 30 instances per class; class results are reported as description only, and
classes 5, 8 and 9 stay descriptive.

The confirmatory run takes each instance once, per arm. Per family the analysis (`tools/e2e.py
analyse --results live-results.json`, `agentsim.decision`) works at instance level: each instance's
correct rate per arm (over its repeats, if any), the mean per-instance difference E − G+ in
percentage points with a 95 % interval from 2000 bootstrap resamples of instances, a two-sided
paired sign-flip p-value on the per-instance differences (exact up to 16 instances, else 20 000
seeded flips) Holm-adjusted across the three families, and the USD-to-answer ratio E/G+ (USD of
the answering session; the save is reported apart, with its break-even) with its bootstrap
interval. Repeats never count as independent: duplicating them changes neither p nor the verdict.
The same analysis of G+ against G reads the ritual. Tasks are never selected by who wins: every
instance of the blind file runs in every stage it belongs to.

### Decision rules

Per family, Claude primary: **engmem better** if correct-rate E − G+ ≥ +15 pts (Holm-adjusted
p < .05), or correctness non-inferior (lower 95 % bound > −5 pts) and USD-to-answer E/G+ ≤ 0.80
(upper bound < 0.95). **Tie** if correctness CI within ±10 pts and cost-ratio CI within [0.8, 1.25].
**grep better** is the mirror. A general claim needs the same direction in a second agent.
Fixed readings: E > G+ on stale/freshness → "staleness-aware memory"; E ≈ G+ > G → "the value is the
discipline of notes, not search"; no family better → say so in the README.

As implemented: the mirror is G+ − E ≥ +15 pts (Holm p < .05), or the upper bound of E − G+ below
+5 pts and E/G+ ≥ 1.25 with its lower bound above 1/0.95. Anything that meets no rule is
`inconclusive`. Stale/freshness is the stale family (classes 4, 10, 11 and 12); "E ≈ G+ > G" is a
tie of E against G+ in a family where G+ is better than G by the same rules.

## Known limits

- Layer 1 measures the ritual and the retrieval, not the agent's judgement: the relevance rule
  is lexical, and a real agent may reject a lexically close document that the policy loads. In
  class 2 that rule decides the outcome; the second-search variant shows the result without it.
- Templates are counted in full on every session; host prompt caching is not modelled, and the
  one-line trigger rule in `CLAUDE.md` or `AGENTS.md` is not counted.
- Class 7 is not a scaling result. On the store before phase 4 (413 documents) engmem reached the
  answer with about 39 % fewer tokens than blind grep, and at 3019 documents the two tied; on the
  phase 4 store (625 documents, `4kb`) the gap is 72 % (2638 against 9328 tokens). Most of it is
  blind grep reading a relevant superseded chain on another topic (cache expiry) whole, link by
  link, while engmem's search shows one redirect line for it. The large tier was not re-measured. The report names grep's chain reads in a class section whenever grep
  made more than one, adds that note to the class's strong-side line when engmem is cheaper, and
  in class 7 says outright that the token difference is not a scaling result.
- Blind grep's output is cut at 20 lines and G's at 40, as an agent piping through `head` would; an
  agent reading the whole listing would pay more as the store grows.
- G ranks files by distinct terms, then matching lines, then newest. `rg` itself has no such order;
  it stands in for the agent's own narrowing. Claude Code's Grep tool orders by modification time,
  which in Layer 2's copied store says nothing.
- An E run reads primer and decisions only, as the daily template says. A fact in Lessons Learned
  or Production Considerations is seen only in the search block's section line (classes 3 and 13);
  G+ reads Lessons by its instruction. This is a candidate change to the daily template in
  `src/engmem`, outside this harness.
