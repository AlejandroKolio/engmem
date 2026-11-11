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
| `shell-claude` | Claude Code | `engmem install --agent claude`; reads `~/.claude/commands/engmem.md`; CLI via subprocess | `engmem draft create` / `draft complete`, content on stdin |
| `shell-codex` | Codex, shell only | `engmem install --agent codex`; reads `~/.agents/skills/engmem/SKILL.md` | as `shell-claude` |
| `shell-copilot-cli` | Copilot CLI | `engmem install --agent copilot-cli`; reads `~/.copilot/skills/engmem/SKILL.md` | as `shell-claude` |
| `mcp-shell` | Claude Code with the MCP server | `engmem mcp` over stdio: `prompts/get`, `tools/list`, `tools/call`; the shell for `store show`, git and the clock | `engmem_create_draft` / `engmem_complete_draft` |
| `mcp-only` | Claude Desktop, ChatGPT | the server entry `engmem install --agent claude-desktop` wrote, launched as the host does | MCP tools only; no clock, no git, so `capture_minutes`, `verified_at`, `author` and `branch` stay blank |
| `mcp-read-only` | a bridge client | the same entry with `--read-only`: the two search tools | none; the harness records what the save template tells it |
| `paste` | an IDE chat with no terminal | `engmem install --agent copilot-ide`; prints the search command; the simulated user runs it and pastes the output | none; the simulated user never asks for a hand-written file |
| `grep` | the same agent without engmem | `rg`-style search over the same documents as plain files, whole-file reads | none |
| `no-history` | the same agent with nothing | nothing | none |

Every engmem profile first reads the start template and applies its scope gate (below,
"Policies"). Shell profiles then run it in its order: `engmem mode show` (step 0), then
`engmem store show` and `engmem draft create` (step 2). Stdin stands in for the template's quoted
heredoc, so no profile needs a POSIX shell and every profile runs on Windows. The three shell
variants differ only in the installed copy. By default `shell-codex` and `shell-copilot-cli` run
class 1 only; `--every-profile` runs them everywhere. Real Codex also has the MCP server and its
template prefers MCP for the draft; that path is the `mcp-shell` profile. Each MCP request times
out after 60 seconds, so a hung server fails the run instead of hanging it.

## Policies

Declared once, applied to every profile and condition, and never read from the answer a run is
scored on. A policy that is wrong for engmem stays wrong in the report.

- **Mode.** Daily, the default; no Pre-reg and no baseline sub-agent.
- **Scope gate.** A task whose description holds a word from a fixed list (`interview`,
  `practise`, `practice`, `study`, `homework`, `essay`, `blog`, `newsletter`, `marketing`,
  `holiday`, `vacation`, `recipe`, `birthday`) is not engineering work. An engmem profile then
  stops after reading the start template: no call, no draft, no search, no save. grep makes no
  call either. Any other task continues, as the template says to when unsure. The list stands in
  for the agent's reading of the task and decides classes 1–8 as engineering work.
- **Query.** The task description's words minus a fixed stopword list, in order, at most eight.
- **Role search.** When the task names a kind of section (`pitfall` → `lessons`, `rejected` →
  `decisions`, `tested` → `testing`, `rollback` → `production`), the search passes that role, as
  the role tool's description says.
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
  loaded document is read by role: primer and decisions (the template's minimum), plus the role
  the task asked about. `mcp-read-only` cannot read; it calls `engmem_search_by_role` once per
  extra role. `paste` loads nothing: the template gives a read no paste path.
- **grep.** One `rg -i -c -e <term>...` over the documents, sorted by count and cut to the top 20
  lines with `head`. Its output names files and counts but shows no text to judge, so grep opens
  as many files, whole, in count order, as an engmem search shows results (three). A file whose
  front matter says `status: superseded` leads to its `superseded_by` file only when the file
  itself passes the relevance rule: the same gate engmem's load applies to a result before it
  takes its successor. The cap is the same five documents. The second search runs when no opened file shows two query terms.
- **Save.** `/engmem.save.quick` for every task; `/engmem.save` for the class 8 saver. The
  decision is the task's known answer when its quote reached the context, else the naive one.
  A Reuse Log row is written only when the quote reached the context; it cites the `cite as`
  reference of the read that returned the quote, else of the search block. When completion
  refuses the row, the run keeps the attempted row, scores its citation invalid, and completes
  again with `Prior docs used: none.`. The simulated user confirms the preview, answers no to
  the supersede question and gives no usefulness feedback.

## Scenario classes

One task per class, each with its declared winner written before any run:

| class | what it tests | declared winner |
|---|---|---|
| 1 | answer exists, keyword-findable | engmem and grep both beat no history |
| 2 | answer exists, wording gap | engmem over grep; both over no history |
| 2, variant | the same, with the second search always run | engmem and grep both reach the answer |
| 3 | answer in a non-primer section | engmem (role search) over grep (whole files) |
| 4 | stale trap: the old decision was superseded | engmem (redirect) over grep |
| 5 | no relevant history | no history; engmem should cost little and say none found |
| 6 | cross-repository answer | engmem and grep tie |
| 7 | larger store (413 documents; 3019 in the large tier) | engmem over grep on cost on a larger store |
| 8 | one agent saves, another agent kind finds and cites it | engmem; without it nothing was saved |
| 9 | non-engineering task (study for an interview) | engmem skips at its scope gate: no draft, no search row, no store write |

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

Sizes: the base store has 73 documents, the scale store (class 7) 413, the large tier 3019. The
mean document is about 3.7 KB. Real seventeen-section documents are often longer, and grep reads
whole files while engmem reads sections, so the synthetic store understates grep's read cost.

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
  row`: the row cites an answer document and is classified `reuse`.
- **engmem's ledger.** The bytes `cost.jsonl` recorded for the run's session beside the bytes
  the harness received. MCP rows must match exactly; CLI rows are larger by the stderr warnings
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
  class 1; `mcp-only`, grep and no history on every scenario; `shell-claude` on classes 1, 4, 5,
  8 and 9). `-m "not e2e"` deselects it. `ENGMEM_E2E_FULL=1` runs the full plan.
- Full default-size run with a report: `uv run python tools/e2e.py run`; it writes
  `results.json` and `report.md` under `.tmp/e2e/`.
- Large tier, opt-in: `uv run python tools/e2e.py run --large`, or
  `ENGMEM_E2E_LARGE=3000 uv run pytest tests/e2e/test_e2e_large.py`. The test records the
  outcome in its report and asserts only that every profile ran on the large store.
- The stores alone: `uv run python tools/e2e.py generate --out <dir>`.

Generated stores and results are never committed; `.tmp/` is ignored.

## Layer 2

`uv run python tools/e2e.py live` prints the plan (task × agent × condition, with each command),
the estimated time and tokens from stated per-run assumptions, and a price only when
`--usd-per-mtok` is given. It starts nothing. `--execute --i-accept-the-cost --pilot` runs the
first planned run and writes `pilot.json`. A full run refuses unless `pilot.json` records a run
whose start call exited 0 with non-empty usage, and, for the engmem condition, whose save call
did too. Usage comes from Claude Code's `--output-format json` result and Codex's `exec --json`
turn events.

A live agent does not inherit the parent environment. Its sandbox starts from `PATH`, `PATHEXT`,
`SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LANGUAGE`,
`LC_ALL`, `LC_CTYPE`, `TERM`, `COLORTERM` and `TZ`, adds only the variables named with
`--pass-env` (for example an API key), and sets the sandbox variables of "Safety". The removed
variables stay removed even when named with `--pass-env`. The grep condition copies the
documents into the checkout as `notes/` and adds one sentence saying so to the prompt.

Not yet verified, because no live run has happened: the exact agent flags, the usage fields, and
whether an agent can authenticate under a sandboxed `HOME`. The pilot exists to check them
before anything larger runs. Class 8 needs two linked sessions and is left out of Layer 2.

## Known limits

- Layer 1 measures the ritual and the retrieval, not the agent's judgement: the relevance rule
  is lexical, and a real agent may reject a lexically close document that the policy loads. In
  class 2 that rule decides the outcome; the second-search variant shows the result without it.
- Templates are counted in full on every session; host prompt caching is not modelled, and the
  one-line trigger rule in `CLAUDE.md` or `AGENTS.md` is not counted.
- Class 7 is not a scaling result. At 413 documents engmem reaches the answer with about 39 %
  fewer tokens than grep, mostly because grep reads a relevant superseded chain on another topic
  (cache expiry) whole, link by link, while engmem's search shows one redirect line for it; at 3019
  documents the two tie. The report names grep's chain reads in a class section whenever grep
  made more than one, adds that note to the class's strong-side line when engmem is cheaper, and
  in class 7 says outright that the token difference is not a scaling result.
- grep's output is cut at 20 lines, as an agent piping through `head` would; an agent reading
  the whole listing would pay more as the store grows.
