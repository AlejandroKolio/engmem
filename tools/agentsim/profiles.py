"""Agent profiles: how each kind of agent reaches engmem, or the same documents without it."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agentsim import policy
from agentsim.corpus import Task
from agentsim.mcp import McpSession
from agentsim.sandbox import ENGMEM_BIN, Sandbox
from agentsim.trace import PASTE, TEMPLATE, TOOL, Step, Transcript

ENGMEM = "engmem"
GREP = "grep"
NONE = "none"

START, SEARCH, LOAD, SAVE = "start", "search", "load", "save"
VERSION = re.compile(r"version: ([0-9a-f]{16})")
REUSE_NONE = "Prior docs used: none."
NOT_STATED = "not stated in the available material."


@dataclass(frozen=True)
class Profile:
    """One row of the profile table in contracts/e2e.md."""

    name: str
    condition: str
    agent: str
    shell: bool = False
    mcp: bool = False
    read_only: bool = False
    paste: bool = False
    saves: bool = False


PROFILES = (
    Profile("shell-claude", ENGMEM, "claude", shell=True, saves=True),
    Profile("shell-codex", ENGMEM, "codex", shell=True, saves=True),
    Profile("shell-copilot-cli", ENGMEM, "copilot-cli", shell=True, saves=True),
    Profile("mcp-shell", ENGMEM, "claude-code-mcp", shell=True, mcp=True, saves=True),
    Profile("mcp-only", ENGMEM, "claude-desktop", mcp=True, saves=True),
    Profile("mcp-read-only", ENGMEM, "claude-desktop", mcp=True, read_only=True),
    Profile("paste", ENGMEM, "copilot-ide", paste=True),
    Profile("grep", GREP, "none"),
    Profile("grep-content", GREP, "none"),
    Profile("grep-guided", GREP, "none"),
    Profile("grep-notes", GREP, "none"),
    Profile("no-history", NONE, "none"),
)
BY_NAME = {p.name: p for p in PROFILES}
ARMS = {
    "shell-claude": "E",
    "grep": "blind G",
    "grep-content": "G",
    "grep-guided": "G+",
    "grep-notes": "GN",
    "no-history": "N",
}
COVERED_CHANGED = re.compile(r"covered files? changed: (.+?), re-check")

COPY_PATHS = {
    "claude": Path(".claude") / "commands",
    "codex": Path(".agents") / "skills",
    "copilot-cli": Path(".copilot") / "skills",
}
SAVE_NAMES = {
    "claude": ("engmem.md", "engmem.save.md", "engmem.save.quick.md"),
    "codex": ("engmem/SKILL.md", "engmem-save/SKILL.md", "engmem-save-quick/SKILL.md"),
    "copilot-cli": ("engmem/SKILL.md", "engmem-save/SKILL.md", "engmem-save-quick/SKILL.md"),
    "copilot-ide": ("engmem.prompt.md", "engmem.save.prompt.md", "engmem.save.quick.prompt.md"),
}
DAILY = "daily"
FILES_COPY_PHRASES = {
    "research": ("engmem draft create", "engmem search", "--session", "engmem read", "engmem store show"),
    DAILY: ("engmem mode show", "engmem search", "engmem read"),
}
MCP_PROMPT_PHRASES = {
    "research": ("engmem_create_draft", "engmem_search", "session_id", "engmem_read"),
    DAILY: ("engmem_search", "engmem_read"),
}
SAVE_SHELL_PHRASES = {
    "research": ("engmem draft complete", "--expected-version", "engmem feedback summary"),
    DAILY: ("engmem draft create", "engmem draft complete", "--expected-version"),
}
SAVE_MCP_PHRASES = {
    "research": ("engmem_complete_draft", "expected_version"),
    DAILY: ("engmem_create_draft", "engmem_complete_draft", "expected_version"),
}
DAILY_START_LEAVES_OUT = ("engmem draft create", "--session", "session_id", "## Search Trace")
GIT_FACTS = "git rev-parse --show-toplevel HEAD --abbrev-ref HEAD; git config user.name"
NO_WRITE_FALLBACK = "If you can do neither"
SCOPE_SKIP_LINE = "engmem: skipped — not an engineering task"


@dataclass
class RunState:
    """What the scripted agent knows mid-run."""

    task: Task
    today: str
    draft_id: str | None = None
    version: str | None = None
    mode: str = DAILY
    skipped: bool = False
    searched: bool = False
    write_tools: bool = False
    template_checks: dict[str, bool] = field(default_factory=dict)
    save_refusals: int = 0
    saved: bool = False
    save_text: str = ""
    save_note: str = ""
    reuse_row: str = ""
    related: str = ""
    row_refused: bool = False


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def install(sandbox: Sandbox, profile: Profile, checkout: Path) -> None:
    """Installs engmem for the profile's agent the way its user would."""
    if profile.condition != ENGMEM or profile.agent == "claude-code-mcp":
        return
    sandbox.setup_engmem("install", "--agent", profile.agent, cwd=checkout)


def _copy(sandbox: Sandbox, profile: Profile, checkout: Path, index: int) -> Path:
    if profile.agent == "copilot-ide":
        return checkout / ".github" / "prompts" / SAVE_NAMES["copilot-ide"][index]
    return sandbox.home / COPY_PATHS[profile.agent] / SAVE_NAMES[profile.agent][index]


def _desktop_entry(sandbox: Sandbox) -> list[str]:
    config = next(sandbox.home.rglob("claude_desktop_config.json"))
    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["engmem"]
    return [entry["command"], *entry["args"]]


def _contains_all(text: str, phrases: tuple[str, ...]) -> bool:
    return all(phrase in text for phrase in phrases)


class Agent:
    """The ritual of one engmem profile, step by step as its rendered template prescribes."""

    def __init__(self, profile: Profile, sandbox: Sandbox, task: Task, checkout: Path, transcript: Transcript):
        self.profile = profile
        self.sandbox = sandbox
        self.task = task
        self.checkout = checkout
        self.t = transcript
        self.state = RunState(task=task, today=today())
        self.mcp: McpSession | None = None

    def _cli(self, phase: str, action: str, *args: str, stdin: str | None = None) -> Step:
        done = self.sandbox.engmem(*args, stdin=stdin, cwd=self.checkout)
        return self.t.add(Step(phase, TOOL, action, done.seconds, done.stdout + done.stderr, done.returncode == 0))

    def _shell(self, phase: str, action: str, argv: list[str]) -> Step:
        done = self.sandbox.run(argv, cwd=self.checkout)
        return self.t.add(Step(phase, TOOL, action, done.seconds, done.stdout + done.stderr, done.returncode == 0))

    def _tool(self, phase: str, name: str, arguments: dict) -> Step:
        assert self.mcp is not None
        text, is_error, seconds = self.mcp.tool(name, arguments)
        return self.t.add(Step(phase, TOOL, name, seconds, text, not is_error))

    def _clock(self) -> Step:
        started = time.perf_counter()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.t.add(Step(SAVE, TOOL, "date -u", time.perf_counter() - started, stamp + "\n"))

    def run(self, saver: dict | None = None) -> RunState:
        if self.profile.mcp:
            argv = [str(ENGMEM_BIN), "mcp"] if self.profile.agent == "claude-code-mcp" else _desktop_entry(self.sandbox)
            if self.profile.read_only:
                argv.append("--read-only")
            with McpSession(argv, self.sandbox.env, self.checkout) as session:
                self.mcp = session
                self._ritual(saver)
            self.mcp = None
        else:
            self._ritual(saver)
        return self.state

    def _description(self, saver: dict | None) -> str:
        return saver["description"] if saver else self.task.description

    def _ritual(self, saver: dict | None) -> None:
        description = self._description(saver)
        text = self._start_template(description)
        self.state.template_checks["start template has the scope gate"] = f"`{SCOPE_SKIP_LINE}`" in text
        if not policy.engineering_task(description):
            self.state.skipped = True
            self.state.save_note = "skipped at the scope gate: no draft, no search"
            return
        self._start_calls(text)
        if self.state.mode != DAILY:
            self._create_draft(description, saver)
        views = self._search(description)
        self._load(views, description)
        if self.profile.saves and self.state.mode == DAILY:
            self._save_daily(saver)
        elif self.profile.saves and self.state.draft_id:
            self._save(saver)
        else:
            self._save_unavailable()

    def _start_template(self, description: str) -> str:
        """The start template the host injects, read before any call: the scope gate comes first."""
        if self.mcp is not None:
            text, seconds = self.mcp.prompt("engmem", description)
            self.t.add(Step(START, TEMPLATE, "prompts/get engmem", seconds, text))
            tools, seconds = self.mcp.tools()
            self.t.add(Step(START, TEMPLATE, "tools/list", seconds, json.dumps(tools)))
            self.state.write_tools = any(tool["name"] == "engmem_create_draft" for tool in tools)
            self.state.template_checks["write tools match the server mode"] = self.state.write_tools != self.profile.read_only
            match = re.search(r"engmem mode for this session: (\w+)", text)
            self.state.mode = match.group(1) if match else DAILY
            if self.profile.read_only and self.state.mode != DAILY:
                self.state.template_checks["start prompt says what to do without a write tool"] = NO_WRITE_FALLBACK in text
            self.state.template_checks["start prompt names the MCP steps"] = _contains_all(
                text, MCP_PROMPT_PHRASES[self.state.mode]
            )
            self._check_daily_start(text)
            return text
        started = time.perf_counter()
        text = _copy(self.sandbox, self.profile, self.checkout, 0).read_text(encoding="utf-8")
        text = text.replace("$ARGUMENTS", description)
        self.t.add(Step(START, TEMPLATE, "installed copy", time.perf_counter() - started, text))
        if self.profile.paste:
            self.state.template_checks["copy describes the paste bridge"] = "paste the output back" in text
        return text

    def _check_daily_start(self, text: str) -> None:
        if self.state.mode == DAILY and "\n## 1. Pre-reg" not in text:
            self.state.template_checks["daily start writes nothing and passes no session id"] = not any(
                phrase in text for phrase in DAILY_START_LEAVES_OUT
            )

    def _start_calls(self, text: str) -> None:
        """Step 0's mode read and, in research mode, step 2's store check: the first calls."""
        if self.mcp is not None:
            if self.profile.shell and self.state.mode != DAILY:
                self._cli(START, "engmem store show", "store", "show")
            return
        if not self.profile.paste:
            shown = self._cli(START, "engmem mode show", "mode", "show")
            match = re.search(r"^mode: (\w+)", shown.text, re.MULTILINE)
            self.state.mode = match.group(1) if match else DAILY
        self.state.template_checks["installed copy names the shell steps"] = _contains_all(
            text, FILES_COPY_PHRASES[self.state.mode]
        )
        self._check_daily_start(text)
        if not self.profile.paste and self.state.mode != DAILY:
            self._cli(START, "engmem store show", "store", "show")

    def _draft_text(self, doc_id: str, title: str) -> str:
        return "\n".join([
            "---",
            f"id: {doc_id}",
            f"title: {title[:80]}",
            f"date: {self.state.today}",
            f"task_date: {self.state.today}",
            "status: draft",
            "superseded_by:",
            "backfilled: false",
            "tags: []",
            "entities: []",
            "related: []",
            "covers_files: []",
            f"repos: [{self.task.repo}]",
            "verified_at:",
            "capture_minutes:",
            f"mode: {self.state.mode}",
            "---",
            "",
        ])

    def _create_draft(self, description: str, saver: dict | None) -> None:
        slug = saver["doc_slug"] if saver else self.task.id
        doc_id = f"{self.state.today.replace('-', '')}-{slug}"
        content = self._draft_text(doc_id, description)
        if self.mcp is not None:
            if not self.state.write_tools:
                return
            step = self._tool(START, "engmem_create_draft", {"id": doc_id, "content": content})
        elif self.profile.shell:
            step = self._cli(START, "engmem draft create", "draft", "create", doc_id, stdin=content)
        else:
            return
        match = VERSION.search(step.text)
        if step.ok and match:
            self.state.draft_id = doc_id
            self.state.version = match.group(1)

    def _search_once(self, query: str, role: str | None) -> Step:
        session = self.state.draft_id
        if self.mcp is not None:
            arguments: dict = {"query": query}
            if session:
                arguments["session_id"] = session
            if role:
                arguments["role"] = role
                return self._tool(SEARCH, "engmem_search_by_role", arguments)
            return self._tool(SEARCH, "engmem_search", arguments)
        args = ["search", query]
        if role:
            args += ["--role", role]
        if session:
            args += ["--session", session]
        if self.profile.paste:
            done = self.sandbox.engmem(*args, cwd=self.checkout)
            return self.t.add(Step(SEARCH, PASTE, "user runs engmem search", done.seconds, done.stdout, done.returncode == 0))
        return self._cli(SEARCH, "engmem search", *args)

    def _search(self, description: str) -> list[tuple[policy.SearchView, list[str], int]]:
        terms = policy.key_terms(description)
        step = self._search_once(" ".join(terms), None)
        self.state.searched = True
        view = policy.read_search(step.text)
        step.cite = view.citations
        views = [(view, terms, policy.RELEVANT_TERMS)]
        if self.profile.paste:
            return views
        if not self.task.always_second_search and policy.any_relevant(view, terms):
            return views
        components = policy.component_terms(self.task.files)
        second = self._search_once(" ".join(components), None)
        second_view = policy.read_search(second.text)
        second.cite = second_view.citations
        views.append((second_view, components, policy.RELEVANT_COMPONENTS))
        return views

    def _load(self, views: list[tuple[policy.SearchView, list[str], int]], description: str) -> None:
        if self.profile.paste:
            return
        chosen: list[str] = []
        for view, terms, minimum in views:
            for doc_id in policy.documents_to_load(view, terms, minimum):
                if doc_id not in chosen and len(chosen) < policy.MAX_DOCS_LOADED:
                    chosen.append(doc_id)
        if not chosen:
            return
        roles = policy.READ_ROLES
        if self.profile.read_only:
            for role in roles[1:]:
                arguments = {"query": " ".join(views[-1][1]), "role": role}
                self._tool(LOAD, "engmem_search_by_role", arguments)
            return
        for doc_id in chosen:
            primer = self._read(doc_id, roles[0])
            for role in [*roles[1:], *policy.asked_roles(description, primer.text)]:
                self._read(doc_id, role)
        self._recheck(views, chosen)

    def _recheck(self, views: list[tuple[policy.SearchView, list[str], int]], chosen: list[str]) -> None:
        """A loaded document whose snapshot line says a covered file changed: that file is re-read
        from the checkout, as the line asks."""
        for doc_id in chosen:
            block = next((view.blocks[doc_id] for view, _, _ in views if doc_id in view.blocks), "")
            match = COVERED_CHANGED.search(block)
            if not match:
                continue
            for relative in (part.strip() for part in match.group(1).split(",")):
                target = self.checkout / relative
                if " " in relative or not target.is_file():
                    continue
                started = time.perf_counter()
                text = target.read_text(encoding="utf-8", errors="replace")
                self.t.add(Step(LOAD, TOOL, f"Read {relative}", time.perf_counter() - started, text,
                                source=f"code:{relative}"))

    def _read(self, doc_id: str, role: str) -> Step:
        session = self.state.draft_id
        if self.mcp is not None:
            arguments = {"id": doc_id, "role": role}
            if session:
                arguments["session_id"] = session
            step = self._tool(LOAD, "engmem_read", arguments)
        else:
            args = ["read", doc_id, "--role", role]
            if session:
                args += ["--session", session]
            step = self._cli(LOAD, "engmem read", *args)
        step.source = doc_id
        match = policy.CITE_AS.search(step.text)
        if match:
            step.cite = {doc_id: match.group(1).strip()}
        return step

    def _save_template(self, full: bool) -> str:
        index = 1 if full else 2
        name = "engmem-save" if full else "engmem-save-quick"
        if self.mcp is not None:
            text, seconds = self.mcp.prompt(name, None)
            self.t.add(Step(SAVE, TEMPLATE, f"prompts/get {name}", seconds, text))
            self.state.template_checks["save prompt names the MCP write"] = _contains_all(
                text, SAVE_MCP_PHRASES[self.state.mode]
            )
            return text
        started = time.perf_counter()
        text = _copy(self.sandbox, self.profile, self.checkout, index).read_text(encoding="utf-8")
        self.t.add(Step(SAVE, TEMPLATE, "installed copy", time.perf_counter() - started, text))
        self.state.template_checks["save copy names the shell write"] = _contains_all(
            text, SAVE_SHELL_PHRASES[self.state.mode]
        )
        return text

    def _save_unavailable(self) -> None:
        """A profile that cannot save still reads the save template it is handed, then stops."""
        text = self._save_template(full=False)
        self.state.template_checks["save template says what to do without a write path"] = NO_WRITE_FALLBACK in text
        self.state.save_note = "no write path: the template's fallback publishes nothing"

    def _git(self, *args: str) -> str:
        if not self.sandbox.has_git:
            return ""
        step = self._shell(SAVE, "git " + " ".join(args), ["git", *args])
        return step.text.strip() if step.ok else ""

    def _save(self, saver: dict | None) -> None:
        full = saver is not None
        self._save_template(full)
        first_clock = self._clock() if self.profile.shell else None
        session = self.state.draft_id
        assert session is not None
        if self.profile.shell:
            self._cli(SAVE, "engmem feedback summary", "feedback", "summary", "--session", session)
        sha = author = branch = ""
        if self.profile.shell:
            sha = self._git("rev-parse", "HEAD")
            author = self._git("config", "user.name")
            branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
            self._cli(SAVE, "engmem cost summary", "cost", "summary", "--session", session)
        second_clock = self._clock() if first_clock else None
        minutes = "0" if second_clock else ""
        row = "" if saver else self._reuse_row()
        self.state.reuse_row = row
        content = compose_save(self, saver, row, sha=sha, author=author, branch=branch, minutes=minutes)
        accepted = self._complete(content)
        if not accepted and row:
            self.state.save_refusals += 1
            self.state.row_refused = True
            content = compose_save(self, saver, "", sha=sha, author=author, branch=branch, minutes=minutes)
            accepted = self._complete(content)
        if not accepted:
            self.state.save_refusals += 1
        self.state.saved = accepted

    def _git_facts(self) -> tuple[str, str, str]:
        """The daily save's one shell call for the front matter: the sha, the author, the branch."""
        if not self.sandbox.has_git:
            return "", "", ""
        facts = self.sandbox.run(["git", "rev-parse", "--show-toplevel", "HEAD", "--abbrev-ref", "HEAD"], cwd=self.checkout)
        author = self.sandbox.run(["git", "config", "user.name"], cwd=self.checkout)
        text = facts.stdout + facts.stderr + author.stdout + author.stderr
        self.t.add(Step(SAVE, TOOL, GIT_FACTS, facts.seconds + author.seconds, text, facts.returncode == 0))
        lines = facts.stdout.splitlines() if facts.returncode == 0 else []
        sha, branch = (lines[1], lines[2]) if len(lines) == 3 else ("", "")
        return sha, author.stdout.strip() if author.returncode == 0 else "", branch

    def _save_daily(self, saver: dict | None) -> None:
        """The daily save as its template says: one git call, then create and complete."""
        self._save_template(saver is not None)
        slug = saver["doc_slug"] if saver else self.task.id
        doc_id = f"{self.state.today.replace('-', '')}-{slug}"
        sha, author, branch = self._git_facts() if self.profile.shell else ("", "", "")
        related = "" if saver else used_document(self.t, self.task.quote)
        self.state.related = related
        content = compose_daily_save(self, doc_id, saver, related, sha=sha, author=author, branch=branch)
        front = content[: content.index("\n---\n", 4) + 5].replace("status: active", "status: draft", 1)
        if self.mcp is not None:
            step = self._tool(SAVE, "engmem_create_draft", {"id": doc_id, "content": front})
        else:
            step = self._cli(SAVE, "engmem draft create", "draft", "create", doc_id, stdin=front)
        match = VERSION.search(step.text)
        if not (step.ok and match):
            self.state.save_refusals += 1
            self.state.save_text = step.text
            return
        self.state.draft_id, self.state.version = doc_id, match.group(1)
        self.state.saved = self._complete(content)
        if not self.state.saved:
            self.state.save_refusals += 1

    def _complete(self, content: str) -> bool:
        session = self.state.draft_id
        assert session is not None and self.state.version is not None
        if self.mcp is not None:
            step = self._tool(
                SAVE, "engmem_complete_draft",
                {"id": session, "content": content, "expected_version": self.state.version},
            )
        else:
            step = self._cli(
                SAVE, "engmem draft complete", "draft", "complete", session,
                "--expected-version", self.state.version, stdin=content,
            )
        self.state.save_text = step.text
        return step.ok and "completed" in step.text

    def _reuse_row(self) -> str:
        """A row only when the task's quote reached the context, cited by the response it came from."""
        quote = self.task.quote
        if not quote:
            return ""
        reference = quoting_reference(self.t, quote)
        if reference is None:
            return ""
        return f'| {reference} | {self.task.artifact}: "{quote}" | changed the decision | reuse |'


def used_document(transcript: Transcript, quote: str) -> str:
    """The id of the prior document the task's quote came from, which a daily save lists in
    `related`; empty when the quote never reached the context."""
    if not quote:
        return ""
    reference = quoting_reference(transcript, quote)
    return reference.split("@", 1)[0] if reference else ""


def quoting_reference(transcript: Transcript, quote: str) -> str | None:
    """The `id@version` of the response the quote came from: a read before a search."""
    reads = [s for s in transcript.steps if s.phase == LOAD and quote in s.text and s.cite]
    for step in reads:
        return next(iter(step.cite.values()))
    for step in transcript.steps:
        if step.phase != SEARCH or quote not in step.text or not step.cite:
            continue
        view = policy.read_search(step.text)
        for doc_id, block in view.blocks.items():
            if quote in block and doc_id in step.cite:
                return step.cite[doc_id]
    return None


def _entities(task: Task) -> list[str]:
    """The touched file's name, else the task's first key term: a save needs one entity."""
    return (policy.component_terms(task.files) or policy.key_terms(task.description) or ["task"])[:1]


def compose_save(agent: Agent, saver: dict | None, row: str, *, sha: str, author: str, branch: str, minutes: str) -> str:
    """The document the save template asks for: quick shape, or the full seventeen for a saver."""
    task, state = agent.task, agent.state
    verified = f"{{{task.repo}: {sha}}}" if sha else ""
    entities = saver["entities"] if saver else _entities(task)
    title = saver["title"] if saver else task.description[:80]
    front = "\n".join([
        "---",
        f"id: {state.draft_id}",
        f"title: {title}",
        f"date: {state.today}",
        f"task_date: {state.today}",
        "status: active",
        "superseded_by:",
        "backfilled: false",
        "tags: [sim]",
        f"entities: [{', '.join(entities)}]",
        "related: []",
        f"covers_files: [{', '.join(task.files)}]",
        f"verified_at: {verified}",
        f"capture_minutes: {minutes}",
        f"author: {author}",
        f"repos: [{task.repo}]",
        f"branch: {branch if branch != 'HEAD' else ''}",
        "pr:",
        f"mode: {state.mode}",
        "---",
        "",
    ])
    reuse = f"| prior-doc | taken | impact | classification |\n|---|---|---|---|\n{row}" if row else REUSE_NONE
    trace = "shell" if state.searched else "miss"
    if saver:
        return front + _full_body(saver, reuse, trace)
    reached = bool(row) or (task.quote and task.quote in agent.t.context())
    decision = task.decision_found if reached else task.decision_naive
    return front + "\n".join([
        "## Decision Log",
        "",
        f"- Decision: {decision}",
        f"- Reason: {NOT_STATED if not reached else 'the prior decision recorded the same constraint.'}",
        f"- Rejected alternative: {NOT_STATED}",
        f"- Source: {NOT_STATED}",
        "",
        "## Future LLM Context (cold-start primer)",
        "",
        decision,
        "",
        "## Reuse Log",
        "",
        reuse,
        "",
        "## Search Trace",
        "",
        trace,
        "",
    ])


def compose_daily_save(
    agent: Agent, doc_id: str, saver: dict | None, related: str, *, sha: str, author: str, branch: str
) -> str:
    """The daily document: the example's front matter, and the product sections only."""
    task, state = agent.task, agent.state
    entities = saver["entities"] if saver else _entities(task)
    title = saver["title"] if saver else task.description[:80]
    front = "\n".join([
        "---",
        f"id: {doc_id}",
        f"title: {title}",
        f"date: {state.today}",
        f"task_date: {state.today}",
        "status: active",
        "superseded_by:",
        "backfilled: false",
        "tags: [sim]",
        f"entities: [{', '.join(entities)}]",
        f"related: [{related}]",
        f"covers_files: [{', '.join(task.files)}]",
        f"verified_at: {f'{{{task.repo}: {sha}}}' if sha else ''}",
        f"author: {author}",
        f"repos: [{task.repo}]",
        f"branch: {branch if branch != 'HEAD' else ''}",
        "pr:",
        f"mode: {DAILY}",
        "---",
        "",
    ])
    if saver:
        return front + _full_body(saver, None, None)
    reached = bool(related) or bool(task.quote and task.quote in agent.t.context())
    decision = task.decision_found if reached else task.decision_naive
    return front + "\n".join([
        "## Decision Log",
        "",
        f"- Decision: {decision}",
        f"- Reason: {NOT_STATED if not reached else 'the prior decision recorded the same constraint.'}",
        f"- Rejected alternative: {NOT_STATED}",
        f"- Source: {NOT_STATED}",
        "",
        "## Future LLM Context (cold-start primer)",
        "",
        decision,
        "",
    ])


def _full_body(saver: dict, reuse: str | None, trace: str | None) -> str:
    tried, failed, fixed, files = saver["lesson"]
    sections = {
        "1. Executive Summary": "Order intake retries now carry an idempotency key.",
        "2. Business Context": "- Intake retry: a repeated order intake call after a timeout.",
        "3. End-to-End Business Flow": "```\nclient -> gateway -> intake\n```",
        "4. Functional Requirements": "- [Verified] A retried call creates at most one order.",
        "5. Acceptance Criteria Analysis": "- [Verified] Two identical calls leave one order.",
        "6. System Architecture": "The gateway client calls the intake service over HTTP.",
        "7. Code Implementation": "- Retry: reads the request token and sends it as the key.",
        "8. Decision Log": saver["decision"],
        "9. Production Considerations": "Keys expire after 24 hours.",
        "10. Testing Knowledge": "- [Verified] A unit test repeats one call twice.",
        "11. Lessons Learned": (
            f"- Tried: {tried}\n  Failed because: {failed}\n  Fixed by: {fixed}\n  Files: {files}"
        ),
        "12. Knowledge Graph": "IntakeRetry -> IdempotencyKey -> deduplicates orders",
        "13. Future LLM Context (cold-start primer)": saver["primer"],
        "14. Search Keywords": "duplicate orders, retry safety, idempotency",
        "15. One-Page Cheat Sheet": "Retries reuse the request token as the idempotency key.",
        "16. Reuse Log": reuse,
        "17. Status at a Glance": "- Done.",
        "Search Trace": trace,
    }
    return "\n".join(f"## {heading}\n\n{text}\n" for heading, text in sections.items() if text is not None)


class GrepAgent:
    """No engmem: `rg` over the same documents as plain files, then whole-file reads."""

    def __init__(self, sandbox: Sandbox, task: Task, transcript: Transcript):
        self.sandbox = sandbox
        self.task = task
        self.t = transcript

    def run(self) -> None:
        if not policy.engineering_task(self.task.description):
            return
        terms = policy.key_terms(self.task.description)
        opened = self._round(terms, [], policy.RELEVANT_TERMS)
        found = any(policy.relevant(terms, self.t.steps[i].text) for i in opened)
        if self.task.always_second_search or not found:
            components = policy.component_terms(self.task.files)
            self._round(components, opened, policy.RELEVANT_COMPONENTS)

    def _round(self, terms: list[str], opened: list[int], minimum: int) -> list[int]:
        ranked = self._rg(terms)
        for path, _ in ranked[: policy.RESULTS_SHOWN]:
            self._open(path, opened, terms, minimum)
        return opened

    def _rg(self, terms: list[str]) -> list[tuple[Path, int]]:
        started = time.perf_counter()
        pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
        counts = []
        for path in sorted(self.sandbox.sessions.glob("*.md")):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            count = sum(1 for line in lines if pattern.search(line))
            if count:
                counts.append((count, path))
        counts.sort(key=lambda item: (-item[0], item[1].name))
        top = counts[: policy.GREP_TOP_LINES]
        output = "".join(f"sessions/{path.name}:{count}\n" for count, path in top)
        argv = "rg -i -c " + " ".join(f"-e {t}" for t in terms) + f" sessions | sort -t: -k2 -nr | head -{policy.GREP_TOP_LINES}"
        step = Step(SEARCH, TOOL, argv, time.perf_counter() - started, output)
        step.detail = {"files_matched": len(counts)}
        self.t.add(step)
        return [(path, count) for count, path in top]

    def _open(self, path: Path, opened: list[int], terms: list[str], minimum: int, followed: bool = False) -> None:
        if len(opened) >= policy.MAX_DOCS_LOADED or any(self.t.steps[i].action == f"Read {path.name}" for i in opened):
            return
        started = time.perf_counter()
        text = path.read_text(encoding="utf-8", errors="replace")
        step = Step(LOAD, TOOL, f"Read {path.name}", time.perf_counter() - started, text, source=path.stem)
        successor = re.search(r"^status: superseded\s*\nsuperseded_by: (\S+)", text, re.MULTILINE)
        if followed or successor:
            step.detail = {"superseded_chain": 1}
        self.t.add(step)
        opened.append(len(self.t.steps) - 1)
        if successor and policy.worth_following(terms, text, minimum):
            target = path.parent / f"{successor.group(1)}.md"
            if target.is_file():
                self._open(target, opened, terms, minimum, followed=True)
