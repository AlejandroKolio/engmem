"""The content-mode grep arms: G (lines shown, no guidance), G+ (engmem's advice as an instruction), GN
(G over notes an earlier agent wrote itself). Declared in contracts/e2e.md, "Policies"."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentsim import policy
from agentsim.corpus import Task
from agentsim.grading import CODE, GIT_LOG
from agentsim.profiles import LOAD, SAVE, SEARCH, START
from agentsim.sandbox import Sandbox
from agentsim.trace import TEMPLATE, TOOL, Step, Transcript

CONTENT_TOP_LINES = 40
MATCHES_PER_FILE = 3
WINDOW = 30
WHOLE_FILE_BYTES = 4096
HEAD_LINES = 60
CHAIN_LIMIT = 10
GUIDANCE = (
    "Earlier engineering notes are in ./notes, one Markdown file per past task. Before you answer, "
    "search them for the task's key terms. Prefer the newest note on a subject. When a note says "
    "`status: superseded`, follow `superseded_by` to the note that replaced it. Read each relevant "
    "note's Decision Log and Lessons Learned. When a note lists `covers_files` and a `verified_at` "
    "commit for this repository, run `git log` on those files since that commit and re-read any "
    "file that changed."
)
NOTES_INSTRUCTION = (
    "Before you finish, write what you decided and why, in a few lines of Markdown, to a new file in "
    "./notes named after today's date and the task."
)
_FRONT_END = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_SUCCESSOR = re.compile(r"^status: superseded\s*\nsuperseded_by: (\S+)", re.MULTILINE)
_COVERS = re.compile(r"^covers_files: \[(.*)\]$", re.MULTILINE)
_VERIFIED = re.compile(r"^verified_at: \{(.*)\}$", re.MULTILINE)
_SECTION = re.compile(r"^## (?:\d+\. )?(Decision Log|Lessons Learned)\s*$")


@dataclass
class _Shown:
    """One file in a content search's output and the lines of it that output shows."""

    path: Path
    lines: list[str] = field(default_factory=list)


class ContentGrepAgent:
    """`rg -n -i -C1` over the notes, judged by the lines it shows, then ranged or whole-file reads."""

    def __init__(
        self, sandbox: Sandbox, task: Task, transcript: Transcript, checkout: Path, *, guided: bool
    ):
        self.sandbox = sandbox
        self.task = task
        self.t = transcript
        self.checkout = checkout
        self.guided = guided
        self.opened: list[str] = []
        self.started = 0

    def run(self) -> None:
        if not policy.engineering_task(self.task.description):
            return
        if self.guided:
            self.t.add(Step(START, TEMPLATE, "guidance", 0.0, GUIDANCE))
        terms = policy.key_terms(self.task.description)
        found = self._round(terms, policy.RELEVANT_TERMS)
        components = policy.component_terms(self.task.files)
        if components and (self.task.always_second_search or not found):
            self._round(components, policy.RELEVANT_COMPONENTS)

    def _round(self, terms: list[str], minimum: int) -> bool:
        shown = self._rg(terms)
        relevant = [s for s in shown if policy.relevant(terms, "\n".join(s.lines), minimum)]
        if self.guided:
            relevant.sort(key=lambda s: s.path.name, reverse=True)
        found = False
        for item in relevant[: policy.RESULTS_SHOWN]:
            if self.started >= policy.MAX_DOCS_LOADED:
                break
            text = self._open(item.path, terms, minimum)
            found = found or policy.relevant(terms, text, policy.RELEVANT_TERMS)
        return found

    def _rg(self, terms: list[str]) -> list[_Shown]:
        """Files ranked by the distinct query terms they hold, then by matching lines, then newest; the first
        `MATCHES_PER_FILE` matches of each with one line of context, cut to `CONTENT_TOP_LINES` lines."""
        started = time.perf_counter()
        pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
        ranked = []
        for path in self._notes():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            hits = [i for i, line in enumerate(lines) if pattern.search(line)]
            if hits:
                distinct = policy.visible_terms(terms, "\n".join(lines[i] for i in hits))
                ranked.append(((distinct, len(hits)), path.name, path, lines, hits))
        ranked.sort(key=lambda item: (-item[0][0], -item[0][1], _reverse(item[1])))
        output: list[str] = []
        shown: list[_Shown] = []
        for _, _, path, lines, hits in ranked:
            hits = hits[:MATCHES_PER_FILE]
            item = _Shown(path)
            wanted = sorted({j for i in hits for j in range(max(0, i - 1), min(len(lines), i + 2))})
            previous = None
            for j in wanted:
                if len(output) >= CONTENT_TOP_LINES:
                    break
                if previous is not None and j != previous + 1:
                    output.append("--")
                    if len(output) >= CONTENT_TOP_LINES:
                        break
                mark = ":" if j in hits else "-"
                output.append(f"sessions/{path.name}{mark}{j + 1}{mark}{lines[j]}")
                item.lines.append(lines[j])
                previous = j
            if item.lines:
                shown.append(item)
            if len(output) >= CONTENT_TOP_LINES:
                break
        argv = (
            "rg -n -i -C1 -m 3 " + " ".join(f"-e {t}" for t in terms)
            + f" sessions | head -{CONTENT_TOP_LINES}"
        )
        step = Step(SEARCH, TOOL, argv, time.perf_counter() - started, "\n".join(output) + ("\n" if output else ""))
        step.detail = {"files_matched": len(ranked)}
        self.t.add(step)
        return shown

    def _notes(self) -> list[Path]:
        return sorted(self.sandbox.sessions.glob("*.md"))

    def _open(self, path: Path, terms: list[str], minimum: int) -> str:
        if path.stem in self.opened:
            return ""
        self.started += 1
        text = self._read(path, terms)
        self._after_read(path, text, terms, minimum)
        return text

    def _read(self, path: Path, terms: list[str]) -> str:
        """Whole under `WHOLE_FILE_BYTES`; otherwise located by one `rg -n` and read in merged ranges
        around its matches (G+ adds front matter, Decision Log, Lessons; nothing matched: the head)."""
        self.opened.append(path.stem)
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content.encode("utf-8")) < WHOLE_FILE_BYTES:
            self.t.add(Step(LOAD, TOOL, f"Read {path.name}", 0.0, content, source=path.stem))
            return content
        lines = content.splitlines()
        hits = self._locate(path, lines, terms)
        ranges = [(max(0, i - WINDOW), min(len(lines), i + WINDOW + 1)) for i in hits]
        if self.guided:
            ranges += _guided_ranges(lines)
        if not ranges:
            ranges.append((0, min(len(lines), HEAD_LINES)))
        texts = []
        for start, end in _merge(ranges):
            chunk = "\n".join(lines[start:end]) + "\n"
            self.t.add(Step(LOAD, TOOL, f"Read {path.name} lines {start + 1}-{end}", 0.0, chunk, source=path.stem))
            texts.append(chunk)
        return "".join(texts)

    def _locate(self, path: Path, lines: list[str], terms: list[str]) -> list[int]:
        started = time.perf_counter()
        pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
        shown = [
            i for i, line in enumerate(lines) if pattern.search(line) or (self.guided and line.startswith("## "))
        ][:CONTENT_TOP_LINES]
        hits = [i for i in shown if pattern.search(lines[i])]
        listing = "".join(f"{i + 1}:{lines[i]}\n" for i in shown)
        argv = "rg -n -i " + " ".join(f"-e {t}" for t in terms) + (" -e '^## '" if self.guided else "")
        self.t.add(Step(LOAD, TOOL, f"{argv} {path.name} | head -{CONTENT_TOP_LINES}", time.perf_counter() - started, listing, source=path.stem))
        return hits

    def _after_read(self, path: Path, text: str, terms: list[str], minimum: int, depth: int = 0) -> None:
        """Follows `superseded_by` from a file that passed the relevance gate, link after link."""
        if self.guided:
            self._freshness(path, text)
        successor = _SUCCESSOR.search(text)
        if successor is None or depth >= CHAIN_LIMIT:
            return
        if depth == 0 and not policy.worth_following(terms, text, minimum):
            return
        target = path.parent / f"{successor.group(1)}.md"
        if not target.is_file() or target.stem in self.opened:
            return
        self._after_read(target, self._read(target, terms), terms, minimum, depth + 1)

    def _freshness(self, path: Path, text: str) -> None:
        """G+'s stated `git log` policy: covered files changed since the note's commit are re-read."""
        covers = _COVERS.search(text)
        verified = _VERIFIED.search(text)
        if not covers or not verified or not self.sandbox.has_git:
            return
        anchors = dict(
            (name.strip(), sha.strip()) for name, _, sha in
            (part.partition(":") for part in verified.group(1).split(","))
        )
        anchor = anchors.get(self.checkout.name)
        files = [f.strip() for f in covers.group(1).split(",") if f.strip()]
        if not anchor or not files:
            return
        argv = ["git", "log", "--oneline", f"{anchor}..HEAD", "--", *files]
        done = self.sandbox.run(argv, cwd=self.checkout)
        self.t.add(Step(LOAD, TOOL, " ".join(argv), done.seconds, done.stdout + done.stderr,
                        done.returncode == 0, source=f"{GIT_LOG}{path.stem}"))
        if done.returncode != 0 or not done.stdout.strip():
            return
        for relative in files:
            target = self.checkout / relative
            if target.is_file():
                content = target.read_text(encoding="utf-8", errors="replace")
                self.t.add(Step(LOAD, TOOL, f"Read {relative}", 0.0, content, source=f"{CODE}{relative}"))


def _guided_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """The front matter, the Decision Log and Lessons Learned, as G+'s heading search shows them."""
    text = "\n".join(lines) + "\n"
    front = _FRONT_END.match(text)
    ranges = [(0, front.group(0).count("\n"))] if front else []
    headings = [i for i, line in enumerate(lines) if line.startswith("## ")]
    for index, i in enumerate(headings):
        if _SECTION.match(lines[i]):
            end = headings[index + 1] if index + 1 < len(headings) else len(lines)
            ranges.append((i, end))
    return ranges


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _reverse(name: str) -> tuple[int, ...]:
    return tuple(-ord(c) for c in name)


def write_own_notes(sessions: Path, task: Task, today: str) -> tuple[str, str]:
    """GN's earlier agent: the free-form note its fixed instruction asks for, written beside the
    others. Returns the note's id and its text."""
    saver = task.saver
    doc_id = f"{today.replace('-', '')}-{saver['doc_slug']}"
    tried, failed, fixed, files = saver["lesson"]
    text = (
        f"# {saver['title']}\n\n{saver['decision']}\n\n"
        f"Tried {tried} It failed because {failed} Fixed by {fixed}\nFiles: {files}\n"
    )
    (sessions / f"{doc_id}.md").write_text(text, encoding="utf-8", newline="\n")
    return doc_id, text


def notes_save_cost(text: str) -> Transcript:
    """What writing the note cost GN's earlier agent: the instruction it read and the note it wrote."""
    transcript = Transcript()
    transcript.add(Step(SAVE, TEMPLATE, "notes instruction", 0.0, NOTES_INSTRUCTION))
    transcript.add(Step(SAVE, TOOL, "Write notes", 0.0, text))
    return transcript
