"""`engmem walkthrough handoff` — Codex and Claude Code pass one task to each other through one
demo store: each client saves a record the other finds. Which client did what is read from the
workspace a record links, `orders` for Codex and `storefront` for Claude Code. Why it is shaped
this way: docs/walkthrough-handoff.md and contracts/install.md, "Walkthrough: a handoff"."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from engmem.demo import (
    ORDER_BOOK,
    SESSION_1_DECISION,
    SESSION_1_START,
    Check,
    claim,
    codex_home_paths,
    commit_once,
    decisions,
    doctor_findings,
    env_command,
    marker_of,
    missing_labels,
    not_ready,
    wire_codex_home,
    workspace_paths,
    write_workspace,
)
from engmem.install import (
    _engmem_mcp_server,
    _ensure_store,
    _load_json_object,
    _parse_toml,
    _read_user_file,
    _SetupError,
    _stdio_mcp_entry,
    _store_argument,
    shell_argument,
)
from engmem.output import _snapshot_line
from engmem.provenance import SessionCheckout
from engmem.runtime import fail, store_path_of
from engmem.scoring import repo_key
from engmem.sections import sections_for_role, split_sections
from engmem.spine import Doc, linked_repos, load_store
from engmem.telemetry import SessionRow, read_session_rows

MARKER_TEXT = "engmem walkthrough handoff\n"
CODEX = "Codex"
CLAUDE = "Claude Code"
CODEX_WORKSPACE = "orders"
CLAUDE_WORKSPACE = "storefront"
WORKSPACE_CLIENTS = {repo_key(CODEX_WORKSPACE): CODEX, repo_key(CLAUDE_WORKSPACE): CLAUDE}
FIXTURE_KEY = "request_id"

CODEX_FILES = {
    "README.md": "# orders\n\nThe order service of the engmem handoff walkthrough.\n",
    "demo_orders/__init__.py": '"""The order service of the engmem handoff walkthrough."""\n',
    "demo_orders/orders.py": ORDER_BOOK,
}
CLAUDE_FILES = {
    "README.md": (
        "# storefront\n\nThe storefront of the engmem handoff walkthrough. It sends orders to the\n"
        "order service, whose repository is not here.\n"
    ),
    "demo_storefront/__init__.py": '"""The storefront of the engmem handoff walkthrough."""\n',
    "demo_storefront/submit.py": (
        '"""Order submission from the storefront to the order service."""\n\n'
        "import uuid\n\n\n"
        "def submit_order(service, item):\n"
        "    return service.place_order(str(uuid.uuid4()), item)\n"
    ),
}
MCP_CONFIG = ".mcp.json"
CLAUDE_SETTINGS = ".claude/settings.json"

SESSION_1_LINK = " Link this record to the orders repository only."
CLAUDE_TASK = (
    "/engmem add retrying the submission: submit_order(service, item, attempts=3) in "
    "demo_storefront/submit.py retries service.place_order when it raises TimeoutError"
)
CLAUDE_DECISION = (
    "Decision: submit_order sends every attempt of one submission with the same request_id. "
    "Reason: the order service returns the existing order for a repeated request_id (the Codex "
    "record about OrderBook.place_order), so a retry after a lost reply gets the first order back "
    "instead of placing a second one. Rejected alternative: a new request_id per attempt, because "
    "each attempt would then place its own order. Source: demo_storefront/submit.py, "
    "submit_order. Link this record to the storefront repository only; the orders checkout is "
    "not here."
)
CODEX_TASK = (
    "$engmem check OrderBook.place_order against the storefront record <the id from step 2>: "
    "search engmem for <the id from step 2> first"
)
CODEX_DECISION = (
    "Decision: OrderBook.place_order keeps request_id as the only key of an order. Reason: "
    "storefront's submit_order sends every attempt of one submission with the same request_id "
    "(the Claude Code record), and its retries rely on getting the first order back. Rejected "
    "alternative: keying on request_id and item together, because a retry carrying a corrected "
    "item would then place a second order. Source: demo_orders/orders.py, OrderBook.place_order. "
    "Link this record to the orders repository only."
)


@dataclass(frozen=True)
class Layout:
    root: Path
    store: Path
    codex_home: Path
    orders: Path
    storefront: Path

    @classmethod
    def under(cls, root: Path) -> Layout:
        return cls(
            root, root / "store", root / "codex-home", root / CODEX_WORKSPACE,
            root / CLAUDE_WORKSPACE,
        )

    @property
    def mcp_config(self) -> Path:
        return self.storefront / MCP_CONFIG

    @property
    def claude_settings(self) -> Path:
        return self.storefront / CLAUDE_SETTINGS

    @property
    def written_through(self) -> list[Path]:
        """Every path the set-up writes into or through, as in the Codex walkthrough, plus Claude
        Code's two project files in the storefront."""
        paths = [
            self.store, self.store / "sessions", *codex_home_paths(self.codex_home),
            *workspace_paths(self.orders, list(CODEX_FILES)),
            *workspace_paths(self.storefront, [*CLAUDE_FILES, MCP_CONFIG, CLAUDE_SETTINGS]),
        ]
        return list(dict.fromkeys(paths))

    @property
    def codex_env(self) -> dict[str, str]:
        return {"CODEX_HOME": str(self.codex_home), "ENGMEM_HOME": str(self.store)}

    @property
    def claude_env(self) -> dict[str, str]:
        return {"ENGMEM_HOME": str(self.store)}

    @property
    def claude_command(self) -> str:
        return f"claude --strict-mcp-config --mcp-config {shell_argument(str(self.mcp_config))}"


def _rerun(layout: Layout) -> str:
    return f"engmem walkthrough handoff --dir {shell_argument(str(layout.root))}"


def _claude_project_files(store: Path) -> dict[str, str]:
    """Claude Code's MCP entry and shell environment for the storefront, both naming the demo
    store; the engineer's ~/.claude and ~/.claude.json are never opened."""
    mcp = {"mcpServers": {"engmem": _stdio_mcp_entry(store)}}
    settings = {"env": {"ENGMEM_HOME": str(store)}}
    return {
        MCP_CONFIG: json.dumps(mcp, indent=2) + "\n",
        CLAUDE_SETTINGS: json.dumps(settings, indent=2) + "\n",
    }


def set_up(root: Path) -> int:
    layout = Layout.under(root)
    claim(layout.root, layout.written_through, MARKER_TEXT)
    _ensure_store(layout.store)
    claude_files = _claude_project_files(layout.store)
    kept = [name for name in claude_files if (layout.storefront / name).exists()]
    created = {
        layout.orders: write_workspace(layout.orders, CODEX_FILES),
        layout.storefront: write_workspace(layout.storefront, {**CLAUDE_FILES, **claude_files}),
    }
    for workspace in created:
        commit_once(workspace)
    wiring = wire_codex_home(layout.codex_home, layout.store)
    print(f"engmem walkthrough handoff: {layout.root}")
    print(
        f"demo store: {layout.store} — Codex and Claude Code both use it; your own store, your "
        f"own Codex settings and your own Claude Code settings are not changed"
    )
    for workspace, was_created in created.items():
        client = WORKSPACE_CLIENTS[repo_key(workspace.name)]
        print(
            f"{client} workspace: {workspace} ({'created' if was_created else 'kept as it is'})"
        )
    for line in wiring:
        print(line)
    for name in claude_files:
        state = "kept as it is" if name in kept else "written"
        print(f"claude code project file: {layout.storefront / name} ({state})")
    reasons = doctor_findings(
        "step 0 — check the Codex wiring:", "codex", layout.store, layout.codex_env, layout.root
    ) + doctor_findings(
        "step 0 — check the Claude Code wiring:", "claude", layout.store, layout.claude_env,
        layout.root,
    )
    if reasons:
        not_ready(reasons, _rerun(layout), "Neither client has been started on the demo")
        return 1
    _print_steps(layout)
    return 0


def _print_steps(layout: Layout) -> None:
    print("ready: the demo wiring has no blocking finding. In a new terminal:")
    print("step 1 — Codex saves the decision, in the orders workspace:")
    print(f"  cd {shell_argument(str(layout.orders))}")
    for line in env_command(layout.codex_env, "codex"):
        print(f"  {line}")
    print(
        "  the first start with this CODEX_HOME asks you to sign in and to trust the folder; "
        "the walkthrough does not copy your Codex login"
    )
    print(f"  type: {SESSION_1_START}")
    print(f"  then: {SESSION_1_DECISION}.{SESSION_1_LINK}")
    print("  then: $engmem-save-quick   and answer the preview with: ok")
    print("step 2 — Claude Code continues the task in the storefront, in another terminal:")
    print(f"  cd {shell_argument(str(layout.storefront))}")
    for line in env_command(layout.claude_env, layout.claude_command):
        print(f"  {line}")
    print(
        "  the first start in this folder asks you to trust it; --strict-mcp-config makes "
        "Claude Code use only the demo's engmem server"
    )
    print(f"  type: {CLAUDE_TASK}")
    print(
        "  expect: before it plans, Claude Code names the Codex record's id, its primer and its "
        "Source, and the record's snapshot line says the orders checkout is not here"
    )
    print(f"  then: {CLAUDE_DECISION}")
    print("  then: /engmem.save.quick   and answer the preview with: ok; note the id it names")
    print("step 3 — Codex finds the Claude Code record by its id, in a new Codex session:")
    print("  type /new in step 1's Codex, or quit it and start it again as step 1 says")
    print(f"  type: {CODEX_TASK}")
    print(f"  then: {CODEX_DECISION}")
    print("  then: $engmem-save-quick   and answer the preview with: ok")
    print("step 4 — confirm the handoff:")
    print(f"  engmem walkthrough handoff --verify --dir {shell_argument(str(layout.root))}")


@dataclass(frozen=True)
class _Route:
    client: str
    source: str
    store: Path | None
    problem: str | None = None


def _codex_routes(config: Path) -> list[_Route]:
    mcp_source = f"--store of [mcp_servers.engmem] in {config}"
    shell_source = f"ENGMEM_HOME in [shell_environment_policy] set, {config}"
    try:
        parsed = _parse_toml(config, _read_user_file(config)[0]) if config.is_file() else None
    except _SetupError as exc:
        return [_Route(CODEX, str(config), None, str(exc))]
    if parsed is None:
        return [_Route(CODEX, str(config), None, "the file is missing")]
    entry = _engmem_mcp_server(parsed)
    mcp_store = _store_argument(entry.get("args")) if isinstance(entry, dict) else None
    policy = parsed.get("shell_environment_policy")
    variables = policy.get("set") if isinstance(policy, dict) else None
    shell = variables.get("ENGMEM_HOME") if isinstance(variables, dict) else None
    return [
        _route(CODEX, mcp_source, mcp_store),
        _route(CODEX, shell_source, store_path_of(shell) if isinstance(shell, str) else None),
    ]


def _claude_routes(mcp_config: Path, settings: Path) -> list[_Route]:
    routes: list[_Route] = []
    try:
        servers = _load_json_object(mcp_config).get("mcpServers") if mcp_config.is_file() else None
        entry = servers.get("engmem") if isinstance(servers, dict) else None
        store = _store_argument(entry.get("args")) if isinstance(entry, dict) else None
        routes.append(_route(CLAUDE, f"--store of mcpServers.engmem in {mcp_config}", store))
    except _SetupError as exc:
        routes.append(_Route(CLAUDE, str(mcp_config), None, str(exc)))
    try:
        env = _load_json_object(settings).get("env") if settings.is_file() else None
        value = env.get("ENGMEM_HOME") if isinstance(env, dict) else None
        store = store_path_of(value) if isinstance(value, str) else None
        routes.append(_route(CLAUDE, f"ENGMEM_HOME in env, {settings}", store))
    except _SetupError as exc:
        routes.append(_Route(CLAUDE, str(settings), None, str(exc)))
    return routes


def _route(client: str, source: str, store: Path | None) -> _Route:
    if store is None:
        return _Route(client, source, None, "it names no store")
    if not store.is_absolute():
        return _Route(client, source, None, f"{store} is a relative path, with no fixed base")
    return _Route(client, source, store)


def _same(store: Path, other: Path) -> bool:
    return os.path.realpath(store) == os.path.realpath(other)


def _wiring_check(layout: Layout) -> Check:
    """AC-18.4: both clients' settings name one store, or the report names each store and where
    it is set, and the handoff is not judged."""
    routes = _codex_routes(layout.codex_home / "config.toml") + _claude_routes(
        layout.mcp_config, layout.claude_settings
    )
    unclear = [route for route in routes if route.problem is not None]
    elsewhere = [
        route for route in routes
        if route.store is not None and not _same(route.store, layout.store)
    ]
    if not unclear and not elsewhere:
        return Check("same store", True, f"Codex and Claude Code are both set to {layout.store}")
    used = []
    for client in (CODEX, CLAUDE):
        stores: dict[str, list[str]] = {}
        for route in routes:
            if route.client == client and route.store is not None:
                stores.setdefault(os.path.realpath(route.store), []).append(route.source)
        used += [
            f"{client} uses {store} ({'; '.join(sources)})" for store, sources in stores.items()
        ]
    used += [
        f"{route.client}: cannot tell the store from {route.source}: {route.problem}"
        for route in unclear
    ]
    return Check(
        "same store", False,
        f"{'; '.join(used)} — the two clients are not set to one store. Make every file above "
        f"name {layout.store}, or delete it and re-run `{_rerun(layout)}`, then repeat the steps",
        failed_as="mismatch",
    )


def _client_of(doc: Doc) -> str | None:
    """The client whose workspace `doc` links; when it links both, the one its anchors name."""
    clients = _clients_of(linked_repos(doc))
    if len(clients) != 1:
        clients = _clients_of(name for name, anchor in (doc.verified_at or {}).items() if anchor)
    return next(iter(clients)) if len(clients) == 1 else None


def _clients_of(names: Iterable[str]) -> set[str]:
    return {WORKSPACE_CLIENTS[key] for key in map(repo_key, names) if key in WORKSPACE_CLIENTS}


def _has_primer(doc: Doc) -> bool:
    primers = sections_for_role(split_sections(doc.body), "primer")
    return any(section.body.strip() for section in primers)


def _records(docs: list[Doc], client: str, key: str | None) -> tuple[list[Doc], list[str]]:
    """Published records `client` saved with a Decision (naming `key`, when given), a stated
    Reason and Source and a primer; and why each near miss does not count."""
    records: list[Doc] = []
    near: list[str] = []
    for doc in docs:
        owner = _client_of(doc)
        both = owner is None and len(_clients_of(linked_repos(doc))) > 1
        if owner != client and not both:
            continue
        groups = [g for g in decisions(doc) if key is None or key in g["decision"]]
        if not groups:
            continue
        if both:
            near.append(
                f"{doc.id} links both {CODEX_WORKSPACE} and {CLAUDE_WORKSPACE}, and its anchors "
                f"do not tell which client saved it"
            )
            continue
        absent = min((missing_labels(g) for g in groups), key=len)
        if doc.status == "draft":
            near.append(f"{doc.id} is still a draft")
        elif absent:
            near.append(f"{doc.id} states no {' and no '.join(absent)}")
        elif not _has_primer(doc):
            near.append(f"{doc.id} has no cold-start primer")
        else:
            records.append(doc)
    return records, near


def _names_id(query: str, doc_id: str) -> bool:
    """Whether `query` holds `doc_id` whole: a longer id that starts or ends with it is not it."""
    pattern = rf"(?<![a-z0-9-]){re.escape(doc_id.casefold())}(?![a-z0-9-])"
    return re.search(pattern, query.casefold()) is not None


def _found_by(
    rows: list[SessionRow], sessions: dict[str, str | None], records: list[Doc], client: str,
    *, by_id: bool = False,
) -> tuple[tuple[str, Doc] | None, list[str]]:
    """The first (session, record) where one search of a session `client` saved showed that
    record as a find (with `by_id`, a search whose query names that same record's id); and the
    sessions that did so but cannot be told apart as `client`'s. A find is what `_finds` counts:
    another session's search that did not show only weak candidates."""
    found: list[tuple[str, str]] = []
    unattributed: set[str] = set()
    for row in rows:
        if row.weak_only:
            continue
        for record in records:
            if record.id == row.session_id or record.id not in row.surfaced:
                continue
            if by_id and not _names_id(row.query, record.id):
                continue
            owner = sessions.get(row.session_id)
            if owner == client:
                found.append((row.session_id, record.id))
            elif owner is None:
                unattributed.add(row.session_id)
    if not found:
        return None, sorted(unattributed)
    session, record_id = min(found)
    record = next(record for record in records if record.id == record_id)
    return (session, record), sorted(unattributed)


def _unattributed_note(unattributed: list[str]) -> str:
    if not unattributed:
        return ""
    return (
        f" (searches of {', '.join(unattributed)} showed it, but which client ran them is "
        f"unknown: a session belongs to a client once its saved record links that client's "
        f"workspace alone, or links both with commit anchors in that one alone)"
    )


def _codex_to_claude(
    docs: list[Doc], rows: list[SessionRow], sessions: dict[str, str | None]
) -> tuple[Doc | None, Check]:
    """AC-18.1: a record saved through Codex was shown to a session Claude Code saved, by a search
    that did not show only weak candidates; weakness is recorded per search, not per result."""
    records, near = _records(docs, CODEX, FIXTURE_KEY)
    if not records:
        why = f" ({'; '.join(near)})" if near else ""
        return None, Check(
            "codex to claude", False,
            f"no published record linked to {CODEX_WORKSPACE} has a Decision naming {FIXTURE_KEY} "
            f"with a stated Reason, Source and a primer{why} — repeat step 1",
        )
    found, unattributed = _found_by(rows, sessions, records, CLAUDE)
    if found is None:
        ids = ", ".join(record.id for record in records)
        return None, Check(
            "codex to claude", False,
            f"no search of a session saved through Claude Code showed {ids} as a find"
            f"{_unattributed_note(unattributed)} — repeat step 2 and finish it with "
            f"/engmem.save.quick: its record linked to {CLAUDE_WORKSPACE} is what names the "
            f"session as Claude Code's",
        )
    session, record = found
    return record, Check(
        "codex to claude", True,
        f"session {session} (Claude Code, {CLAUDE_WORKSPACE}) was shown {record.id} (saved through "
        f"Codex) by a search that did not show only weak candidates, with its primer and Source: "
        f"{_source_of(record)}",
    )


def _claude_to_codex(
    docs: list[Doc], rows: list[SessionRow], sessions: dict[str, str | None]
) -> Check:
    """AC-18.2: a record saved through Claude Code was found, by a search naming its id, in a
    session Codex saved."""
    records, near = _records(docs, CLAUDE, None)
    if not records:
        why = f" ({'; '.join(near)})" if near else ""
        return Check(
            "claude to codex", False,
            f"no published record linked to {CLAUDE_WORKSPACE} has a Decision with a stated "
            f"Reason, Source and a primer{why} — repeat step 2",
        )
    found, unattributed = _found_by(rows, sessions, records, CODEX, by_id=True)
    if found is None:
        ids = ", ".join(record.id for record in records)
        return Check(
            "claude to codex", False,
            f"no search naming {ids} in a session saved through Codex showed it as a find"
            f"{_unattributed_note(unattributed)} — repeat step 3 with the id step 2 named, and "
            f"finish it with $engmem-save-quick: its record linked to {CODEX_WORKSPACE} is what "
            f"names the session as Codex's",
        )
    session, record = found
    return Check(
        "claude to codex", True,
        f"session {session} (Codex, {CODEX_WORKSPACE}) searched for {record.id} (saved through "
        f"Claude Code) and was shown it, by a search that did not show only weak candidates",
    )


def _source_of(doc: Doc) -> str:
    return next(g["source"] for g in decisions(doc) if not missing_labels(g))


def _unavailable_source(layout: Layout, record: Doc) -> str:
    """AC-18.3, as the search's own snapshot line shows the record in Claude Code's workspace,
    which has no checkout of the repository a Codex record links."""
    line = _snapshot_line(record, SessionCheckout(layout.storefront))
    return (
        f"note: unavailable source: in {CLAUDE_WORKSPACE}, {record.id} keeps its Source "
        f"({_source_of(record)}) and its {line}; the current-code check is shown as not performed"
    )


def verify(root: Path) -> int:
    layout = Layout.under(root)
    if marker_of(layout.root) != MARKER_TEXT:
        fail(
            f"engmem walkthrough: {layout.root} was not built by `engmem walkthrough handoff` — "
            f"run `{_rerun(layout)}` first"
        )
        return 2
    print(f"engmem walkthrough handoff --verify: {layout.root}")
    print(f"demo store: {layout.store}")
    wiring = _wiring_check(layout)
    print(wiring)
    if not wiring.passed:
        print(
            "not checked: codex to claude, claude to codex — with the clients set to different "
            "stores, a record one of them cannot see is not knowledge missing from a shared store"
        )
        print("NOT COMPLETE: configuration mismatch — the handoff was not checked")
        return 1
    checks, record = _handoff_checks(layout)
    for check in checks:
        print(check)
    if record is not None:
        print(_unavailable_source(layout, record))
    print(
        f"not checked here: that each client was started as its step says — the client of a "
        f"session is read from the workspace its record links ({CODEX_WORKSPACE}: Codex, "
        f"{CLAUDE_WORKSPACE}: Claude Code); what each agent said is in its transcript"
    )
    failed = sum(not check.passed for check in checks)
    if failed:
        print(
            f"NOT COMPLETE: {failed} of {len(checks)} handoff checks did not pass — the "
            f"walkthrough is not complete"
        )
        return 1
    print(
        "PASS: a record saved through Codex reached Claude Code, and a record saved through "
        "Claude Code reached Codex by its id, through one store"
    )
    return 0


def _handoff_checks(layout: Layout) -> tuple[list[Check], Doc | None]:
    sessions_dir = layout.store / "sessions"
    result = load_store(sessions_dir)
    if result.scan_error is not None:
        reason = f"{result.scan_error.message} — run step 0 again for the reason"
        return _both_missing(reason), None
    telemetry = layout.store / "telemetry.jsonl"
    try:
        rows = read_session_rows(telemetry)
    except (OSError, UnicodeDecodeError) as exc:
        return _both_missing(f"cannot read {telemetry} ({exc})"), None
    sessions = {doc.id: _client_of(doc) for doc in result.docs}
    record, forward = _codex_to_claude(result.docs, rows, sessions)
    return [forward, _claude_to_codex(result.docs, rows, sessions)], record


def _both_missing(reason: str) -> list[Check]:
    return [Check("codex to claude", False, reason), Check("claude to codex", False, reason)]
