"""A stdio MCP server for engmem; stdout carries JSON-RPC frames and nothing else."""

from __future__ import annotations

import json
import os
import re
import sys
from importlib import resources
from pathlib import Path
from typing import Any, TextIO

from engmem import __version__, cost, feedback
from engmem.cache import KeptByPath
from engmem.mcp_documents import (
    handle_complete_draft,
    handle_create_draft,
    handle_mark_superseded,
)
from engmem.mcp_tools import (
    COMPLETE_DRAFT_TOOL_NAME,
    CREATE_DRAFT_TOOL_NAME,
    MARK_SUPERSEDED_TOOL_NAME,
    READ_TOOL_NAME,
    RECORD_BASELINE_TOOL_NAME,
    RECORD_FEEDBACK_TOOL_NAME,
    ROLE_TOOL_NAME,
    TOOL_NAME,
    tools_list,
)
from engmem.read_report import ReadError, compose_read
from engmem.settings import ModeSettingError, mode_setting_file, saved_mode
from engmem.scoring import Scope
from engmem.search_report import compose
from engmem.sections import CANONICAL_ROLES
from engmem.spine import (
    Doc,
    LoadResult,
    load_store,
    load_yaml,
    sessions_dir_unreadable,
    stray_documents,
)

PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 reserved error codes (https://www.jsonrpc.org/specification#error_object)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# prompt name -> source template filename under src/engmem/templates/ — the
# same files install.py's _install_templates copies out; a second reader of
# that one source of truth, not a fork of it
START_PROMPT_NAME = "engmem"
PROMPT_TEMPLATES = {
    START_PROMPT_NAME: "engmem.start.md",
    "engmem-save": "engmem.save.md",
    "engmem-save-quick": "engmem.save.quick.md",
}

_WHITESPACE_RE = re.compile(r"\s+")
_ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"


class _ProtocolError(Exception):
    """Raised to produce a JSON-RPC error response, never for a tool-level failure."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# template loading — shared by prompts/list and prompts/get
# ---------------------------------------------------------------------------


def _load_template(source_name: str) -> tuple[str, Any, str]:
    """`(description, argument_hint, body)`, with the front matter removed from body."""
    content = (
        (resources.files("engmem") / "templates" / source_name).read_text(encoding="utf-8")
    )
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", None, content

    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return "", None, content

    front_matter_text = "".join(lines[1:closing])
    body = "".join(lines[closing + 1 :]).lstrip("\n")

    front = load_yaml(front_matter_text) or {}
    if not isinstance(front, dict):
        front = {}
    description = str(front.get("description", ""))
    argument_hint = front.get("argument-hint")
    return description, argument_hint, body


def _argument_name_from_hint(hint: str) -> str:
    # strips engmem's own <angle-bracket> wrapping, then turns the words into
    # an identifier a JSON-RPC client can pass by name
    cleaned = hint.strip()
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1].strip()
    return _WHITESPACE_RE.sub("_", cleaned) or "argument"


# ---------------------------------------------------------------------------
# store access for the tool — the text itself is search_report.compose's, shared with the CLI
# ---------------------------------------------------------------------------


class _StoreLoadError(Exception):
    """Raised when the store itself cannot be read at all."""


# the server answers many calls from one process; a document is parsed again only when its file
# reads differently — contracts/cache.md, "Kept between searches"
_KEPT_DOCS: KeptByPath[Doc] = KeptByPath()


def _load_store_for_tool(store: Path) -> tuple[LoadResult, list[Path], list[str]]:
    """Loads `sessions/`, raising `_StoreLoadError` rather than returning on a broken store."""
    sessions_dir = store / "sessions"

    unreadable = sessions_dir_unreadable(sessions_dir)
    if unreadable:
        raise _StoreLoadError(f"error: {unreadable}")
    if not sessions_dir.is_dir():
        raise _StoreLoadError(
            f"error: store not found: {sessions_dir} does not exist "
            "(run `engmem install` to create the store, or check the configured "
            "store path)."
        )

    result = load_store(sessions_dir, _KEPT_DOCS)
    for problem in result.errors:
        print(f"engmem-mcp: {problem.message}", file=sys.stderr)
    for problem in result.warnings:
        print(f"engmem-mcp: {problem.message}", file=sys.stderr)

    try:
        strays, stray_scan_errors = stray_documents(store)
    except OSError as exc:
        # distinct from load_store's handling above: the store root itself
        # being unscannable, not sessions/
        raise _StoreLoadError(
            f"error: could not scan {store} for stray markdown files: {exc}"
        ) from exc

    return result, strays, stray_scan_errors


def _run_search_for_tool(
    store: Path,
    query: str,
    session_id: str | None = None,
    role: str | None = None,
    scope: Scope | None = None,
    read_only: bool = False,
) -> tuple[str, bool]:
    """`(text, is_error)`: the composed result the CLI prints, carried in the tool result."""
    try:
        result, strays, stray_scan_errors = _load_store_for_tool(store)
    except _StoreLoadError as exc:
        return str(exc), True

    for scan_error in stray_scan_errors:
        print(f"engmem-mcp: {scan_error}", file=sys.stderr)
    # channel="mcp" keeps a Desktop search and a terminal search distinguishable in
    # telemetry.jsonl; compose writes the row, so the MCP path is never invisible to Gate 1
    text = compose(
        store, result, strays, stray_scan_errors, query, role, session_id, "mcp", scope,
        retain_versions=not read_only, record_cost=not read_only, name_drafts=not read_only,
    )
    return text, False


def _handle_record_feedback(arguments: object, store: Path) -> tuple[str, bool]:
    try:
        result, _, _ = _load_store_for_tool(store)
        entry = feedback.record(
            store, {d.id for d in result.docs},
            session_id=arguments["session_id"], doc_id=arguments["doc_id"],
            assessment=arguments["assessment"], decision=arguments.get("decision"),
            source=arguments.get("source"), channel="mcp",
        )
    except (_StoreLoadError, feedback.FeedbackError) as exc:
        return str(exc), True
    return feedback.confirmation(entry), False


def _optional_number(arguments: dict, field_name: str, *, integer: bool) -> object:
    value = arguments.get(field_name)
    allowed = (int,) if integer else (int, float)
    if value is not None and (isinstance(value, bool) or not isinstance(value, allowed)):
        kind = "an integer" if integer else "a number"
        raise _ProtocolError(INVALID_PARAMS, f"invalid params: {field_name!r} must be {kind}")
    return value


def _handle_record_baseline(arguments: object, store: Path) -> tuple[str, bool]:
    tokens = _optional_number(arguments, "tokens", integer=True)
    seconds = _optional_number(arguments, "seconds", integer=False)
    try:
        result, _, _ = _load_store_for_tool(store)
        entry = cost.record_baseline(
            store, {d.id for d in result.docs}, session_id=arguments["session_id"],
            tokens=tokens, seconds=seconds, call_id=arguments.get("call_id"), channel="mcp",
        )
    except (_StoreLoadError, cost.CostError) as exc:
        return str(exc), True
    return cost.confirmation(entry), False


def _handle_read(arguments: object, store: Path) -> tuple[str, bool]:
    role = arguments.get("role")
    if role is not None and role not in CANONICAL_ROLES:
        raise _ProtocolError(
            INVALID_PARAMS,
            f"invalid params: 'role' must be one of: {', '.join(CANONICAL_ROLES)} (got {role!r})",
        )
    session_id = (arguments.get("session_id") or "").strip() or None
    try:
        result, _, _ = _load_store_for_tool(store)
        return compose_read(
            store, result.docs, arguments["id"], role, session_id, "mcp", result.errors
        ), False
    except (_StoreLoadError, ReadError) as exc:
        return str(exc), True


# ---------------------------------------------------------------------------
# method handlers — each takes (params, store) and returns a JSON-serializable
# result, or raises _ProtocolError for a protocol-level fault
# ---------------------------------------------------------------------------


def _handle_ping(params: dict, store: Path) -> dict:
    # spec 2025-06-18 Utilities/Ping: respond promptly with an empty result,
    # unconditionally — used by clients (Claude Desktop included) as a liveness probe
    return {}


def _handle_initialize(params: dict, store: Path) -> dict:
    # we support exactly one protocol version, so we always answer with it
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}, "prompts": {}},
        "serverInfo": {"name": "engmem", "version": __version__},
    }


def _handle_initialized_notification(params: dict, store: Path) -> dict:
    return {}  # never actually sent; present so the method isn't method-not-found


def _handle_tools_list(params: dict, store: Path) -> dict:
    return tools_list()


# every tool but the two searches, each one outside `_SEARCH_TOOL_NAMES` and so off a --read-only
# server; `engmem_read` writes no document, only its row in `cost.jsonl`
_WRITE_TOOL_REQUIRED_STRING_ARGS = {
    CREATE_DRAFT_TOOL_NAME: ("id", "content"),
    COMPLETE_DRAFT_TOOL_NAME: ("id", "content"),
    MARK_SUPERSEDED_TOOL_NAME: ("id", "superseded_by"),
    RECORD_FEEDBACK_TOOL_NAME: ("session_id", "doc_id", "assessment"),
    READ_TOOL_NAME: ("id",),
    RECORD_BASELINE_TOOL_NAME: ("session_id",),
}

# `expected_version` is required by both schemas, yet checked here only for its type: missing, it
# is refused by the handler with the document's current version, which a protocol error lacks
_WRITE_TOOL_OPTIONAL_STRING_ARGS = {
    COMPLETE_DRAFT_TOOL_NAME: ("expected_version",),
    MARK_SUPERSEDED_TOOL_NAME: ("expected_version",),
    RECORD_FEEDBACK_TOOL_NAME: ("decision", "source"),
    READ_TOOL_NAME: ("role", "session_id"),
    RECORD_BASELINE_TOOL_NAME: ("call_id",),
}

_WRITE_TOOL_HANDLERS = {
    CREATE_DRAFT_TOOL_NAME: handle_create_draft,
    COMPLETE_DRAFT_TOOL_NAME: handle_complete_draft,
    MARK_SUPERSEDED_TOOL_NAME: handle_mark_superseded,
    RECORD_FEEDBACK_TOOL_NAME: _handle_record_feedback,
    READ_TOOL_NAME: _handle_read,
    RECORD_BASELINE_TOOL_NAME: _handle_record_baseline,
}


def _scope_names(args: dict) -> list[str]:
    """`repo` and `repos` together, refused rather than read as absent when blank or empty."""
    repo = args.get("repo")
    repos = args.get("repos")
    if repo is not None and not isinstance(repo, str):
        raise _ProtocolError(INVALID_PARAMS, "invalid params: 'repo' must be a string")
    if repos is not None and not (
        isinstance(repos, list) and all(isinstance(name, str) for name in repos)
    ):
        raise _ProtocolError(INVALID_PARAMS, "invalid params: 'repos' must be a list of strings")
    if repo is not None and not repo.strip():
        raise _ProtocolError(
            INVALID_PARAMS,
            "invalid params: 'repo' needs a repository name (got a blank value) — omit it "
            "to search the whole store",
        )
    if repos is not None and not repos:
        raise _ProtocolError(
            INVALID_PARAMS,
            "invalid params: 'repos' needs at least one repository name (got an empty list) "
            "— omit it to search the whole store",
        )
    if repos is not None and not all(name.strip() for name in repos):
        raise _ProtocolError(
            INVALID_PARAMS,
            "invalid params: 'repos' needs repository names (got a blank value) — omit it "
            "to search the whole store",
        )
    return ([repo] if repo is not None else []) + (repos or [])


def _search_scope(arguments: object) -> Scope | None:
    """The scope a search tool call asks for; `null` and `false` are absent
    (contracts/mcp-server.md)."""
    args = arguments if isinstance(arguments, dict) else {}
    unscoped = args.get("unscoped")
    all_repos = args.get("all_repos")
    names = _scope_names(args)
    if unscoped is not None and not isinstance(unscoped, bool):
        raise _ProtocolError(INVALID_PARAMS, "invalid params: 'unscoped' must be a boolean")
    if all_repos is not None and not isinstance(all_repos, bool):
        raise _ProtocolError(INVALID_PARAMS, "invalid params: 'all_repos' must be a boolean")
    chosen = []
    if names:
        given = [key for key in ("repo", "repos") if args.get(key) is not None]
        chosen.append("/".join(f"'{key}'" for key in given))
    if unscoped:
        chosen.append("'unscoped'")
    if all_repos:
        chosen.append("'all_repos'")
    if len(chosen) > 1:
        raise _ProtocolError(
            INVALID_PARAMS,
            f"invalid params: {' and '.join(chosen)} are different scopes — pass one",
        )
    if unscoped:
        return Scope(unscoped=True)
    if all_repos:
        return Scope(all_repos=True)
    if not names:
        return None
    return Scope(repos=tuple(names))


def _handle_tools_call(params: dict, store: Path, read_only: bool = False) -> dict:
    if "name" not in params:
        raise _ProtocolError(INVALID_PARAMS, "invalid params: missing required 'name' field")
    name = params.get("name")
    if name not in (TOOL_NAME, ROLE_TOOL_NAME, *_WRITE_TOOL_HANDLERS):
        raise _ProtocolError(INVALID_PARAMS, f"unknown tool: {name!r}")

    arguments = params.get("arguments")

    if name in _WRITE_TOOL_HANDLERS:
        # the inputSchema declares each of these required, so a missing or blank value is a
        # protocol-level fault; a valid-but-refused write (bad id shape, escaping sessions/,
        # overwrite refusal) is an ordinary isError result from the handler instead
        for field_name in _WRITE_TOOL_REQUIRED_STRING_ARGS[name]:
            value = arguments.get(field_name) if isinstance(arguments, dict) else None
            if not isinstance(value, str) or not value.strip():
                raise _ProtocolError(
                    INVALID_PARAMS,
                    f"invalid params: {field_name!r} must be a non-empty string",
                )
        for field_name in _WRITE_TOOL_OPTIONAL_STRING_ARGS.get(name, ()):
            value = arguments.get(field_name) if isinstance(arguments, dict) else None
            if value is not None and not isinstance(value, str):
                raise _ProtocolError(
                    INVALID_PARAMS, f"invalid params: {field_name!r} must be a string"
                )
        text, is_error = _WRITE_TOOL_HANDLERS[name](arguments, store)
        result: dict = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        return result

    query = arguments.get("query") if isinstance(arguments, dict) else None
    if not isinstance(query, str) or not query.strip():
        # schema declares query required, so a missing/blank value is a protocol fault, not
        # isError (reserved for a schema-conforming call that fails at runtime)
        raise _ProtocolError(
            INVALID_PARAMS, "invalid params: 'query' must be a non-empty string"
        )

    session_id = arguments.get("session_id") if isinstance(arguments, dict) else None
    if session_id is not None and not isinstance(session_id, str):
        # wrong JSON type is malformed, not something to coerce — str() on a
        # dict would write Python repr syntax into the log
        raise _ProtocolError(
            INVALID_PARAMS, "invalid params: 'session_id' must be a string"
        )
    if session_id is not None:
        # blank == absent; mirrors commands.py's _session_id so both paths log the same key
        session_id = session_id.strip() or None
    scope = _search_scope(arguments)

    if name == ROLE_TOOL_NAME:
        role = arguments.get("role") if isinstance(arguments, dict) else None
        if not isinstance(role, str) or role not in CANONICAL_ROLES:
            raise _ProtocolError(
                INVALID_PARAMS,
                "invalid params: 'role' must be one of: "
                + ", ".join(CANONICAL_ROLES)
                + (f" (got {role!r})" if role is not None else " (missing)"),
            )
        text, is_error = _run_search_for_tool(
            store, query, session_id, role, scope, read_only
        )
    else:
        text, is_error = _run_search_for_tool(
            store, query, session_id, scope=scope, read_only=read_only
        )

    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _prompt_summary(name: str, source_name: str) -> dict:
    description, argument_hint, _ = _load_template(source_name)
    arguments = []
    if argument_hint:
        arguments.append(
            {
                "name": _argument_name_from_hint(str(argument_hint)),
                "description": (
                    f"Substituted for {_ARGUMENTS_PLACEHOLDER} in the prompt body "
                    f"({argument_hint})"
                ),
                "required": False,
            }
        )
    return {
        "name": name,
        "title": f"/{name}",
        "description": description,
        "arguments": arguments,
    }


def _handle_prompts_list(params: dict, store: Path) -> dict:
    return {
        "prompts": [_prompt_summary(name, source) for name, source in PROMPT_TEMPLATES.items()]
    }


def _mode_line() -> str:
    """The answer to the start template's step 0, read on every call so a server that outlives
    a `engmem mode set` hands the next session the new mode."""
    setting = mode_setting_file()
    try:
        mode = saved_mode()
    except ModeSettingError as exc:
        return (
            f"engmem mode for this session: daily (the saved mode cannot be read: {exc} — "
            "tell the user, as step 0 says)"
        )
    if mode is None:
        return f"engmem mode for this session: daily (default — no saved choice in {setting})"
    return f"engmem mode for this session: {mode} (saved choice in {setting})"


def _handle_prompts_get(params: dict, store: Path) -> dict:
    if "name" not in params:
        raise _ProtocolError(INVALID_PARAMS, "invalid params: missing required 'name' field")
    name = params.get("name")
    # the isinstance guard is not redundant: a dict or list `name` is unhashable, so looking it
    # up would raise TypeError and leave as a -32603 internal error — a malformed request
    # reported as a server bug. Same treatment `tools/call` gives a wrongly typed tool name
    if not isinstance(name, str) or name not in PROMPT_TEMPLATES:
        raise _ProtocolError(INVALID_PARAMS, f"unknown prompt: {name!r}")
    source_name = PROMPT_TEMPLATES[name]

    description, argument_hint, body = _load_template(source_name)

    if argument_hint:
        supplied = params.get("arguments")
        arg_name = _argument_name_from_hint(str(argument_hint))
        value = supplied.get(arg_name) if isinstance(supplied, dict) else None
        # missing/None substitutes to empty; a supplied non-string value is a malformed request
        # (the spec types these as string), not coerced — str()/repr() would leak Python syntax,
        # and a truthiness check would wrongly blank out a legitimate "0"
        if value is not None and not isinstance(value, str):
            raise _ProtocolError(
                INVALID_PARAMS, f"invalid params: argument {arg_name!r} must be a string"
            )
        body = body.replace(_ARGUMENTS_PLACEHOLDER, value if value is not None else "")

    if name == START_PROMPT_NAME:
        body = f"{_mode_line()}\n\n{body}"

    return {
        "description": description,
        "messages": [{"role": "user", "content": {"type": "text", "text": body}}],
    }


_METHODS = {
    "ping": _handle_ping,
    "initialize": _handle_initialize,
    "notifications/initialized": _handle_initialized_notification,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "prompts/list": _handle_prompts_list,
    "prompts/get": _handle_prompts_get,
}

_SEARCH_TOOL_NAMES = (TOOL_NAME, ROLE_TOOL_NAME)


def _handle_read_only_tools_list(params: dict, store: Path) -> dict:
    tools = _handle_tools_list(params, store)["tools"]
    return {"tools": [tool for tool in tools if tool["name"] in _SEARCH_TOOL_NAMES]}


def _handle_read_only_tools_call(params: dict, store: Path) -> dict:
    # an allowlist, like tools/list: a tool added later stays unreachable here until named above
    name = params.get("name")
    if "name" in params and name not in _SEARCH_TOOL_NAMES:
        raise _ProtocolError(
            INVALID_PARAMS, f"unknown tool: {name!r} — this server runs with --read-only"
        )
    # D2: a read-only server keeps no version copies and offers no reference — a client behind a
    # bridge cannot save a Reuse Log, and the store is not written for it — and, for the same
    # reason, records no delivery in cost.jsonl (contracts/mcp-server.md)
    return _handle_tools_call(params, store, read_only=True)


# `engmem mcp --read-only`: the two search tools and nothing that writes a document
_READ_ONLY_METHODS = {
    **_METHODS,
    "tools/list": _handle_read_only_tools_list,
    "tools/call": _handle_read_only_tools_call,
}

# methods legitimately invoked with no id — their handler still runs even
# though no response can be sent; every other method is request/response, and
# _dispatch skips the handler entirely for a non-conforming id-less message
_NOTIFICATION_METHODS = {"notifications/initialized"}


# ---------------------------------------------------------------------------
# JSON-RPC envelope handling
# ---------------------------------------------------------------------------


def _error_response(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _has_unpaired_surrogate(message: Any) -> bool:
    """True when any string anywhere in the decoded message cannot be encoded as UTF-8."""
    # an explicit stack rather than recursion: the decoder already accepted this nesting depth,
    # and a recursive walk of the same structure could raise RecursionError where it did not
    stack: list[Any] = [message]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _dispatch(message: Any, store: Path, read_only: bool = False) -> dict | None:
    """The response dict to write, or None when nothing should be written."""
    if not isinstance(message, dict):
        return _error_response(None, INVALID_REQUEST, "invalid request: expected a JSON object")

    # rejected here, once, rather than at each field: a lone surrogate reaches every consumer
    # downstream as a different failure — `content.encode` raises, `json.dumps` echoes an
    # escape strict clients reject — and none of them can be UTF-8, which the MCP stdio
    # transport requires. The id is null because the id itself may be the offending string
    if _has_unpaired_surrogate(message):
        return _error_response(
            None,
            INVALID_REQUEST,
            "invalid request: message contains an unpaired surrogate, which is not UTF-8",
        )

    has_id = "id" in message
    msg_id = message.get("id")

    # id must be string/number/null; on failure the error's own id is null,
    # never an echo of something that couldn't be trusted in the first place.
    # `isinstance(True, int)`, so booleans are excluded by name or `id: true` echoes back
    if (
        has_id
        and msg_id is not None
        and (isinstance(msg_id, bool) or not isinstance(msg_id, (str, int, float)))
    ):
        return _error_response(None, INVALID_REQUEST, "invalid request: 'id' must be a string, number, or null")

    # enforced, not warn-and-served: every real client sends exactly "2.0"
    if message.get("jsonrpc") != "2.0":
        return (
            _error_response(msg_id, INVALID_REQUEST, "invalid request: 'jsonrpc' must be exactly \"2.0\"")
            if has_id
            else None
        )

    method = message.get("method")

    if not isinstance(method, str) or not method:
        return (
            _error_response(msg_id, INVALID_REQUEST, "invalid request: missing 'method'")
            if has_id
            else None
        )

    handler = (_READ_ONLY_METHODS if read_only else _METHODS).get(method)
    if handler is None:
        return (
            _error_response(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")
            if has_id
            else None
        )

    if not has_id and method not in _NOTIFICATION_METHODS:
        # unanswerable — skip the handler rather than do possibly expensive
        # work (e.g. a full store scan) whose result would be discarded
        return None

    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    try:
        result = handler(params, store)
    except _ProtocolError as exc:
        return _error_response(msg_id, exc.code, exc.message) if has_id else None
    except Exception as exc:  # last-resort guard: the stdio loop must never die
        print(f"engmem-mcp: unhandled error in {method}: {exc!r}", file=sys.stderr)
        return (
            _error_response(msg_id, INTERNAL_ERROR, f"internal error: {exc}")
            if has_id
            else None
        )

    if not has_id:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _write(stdout: TextIO, message: dict) -> None:
    # json.dumps escapes embedded newlines, keeping a multi-line tool result to one line
    stdout.write(json.dumps(message) + "\n")
    stdout.flush()


def _mute_broken_stdout(stdout: TextIO) -> None:
    """Points the dead stdout fd at os.devnull so the interpreter's shutdown flush of the frame
    still sitting in its buffer cannot fail — see contracts/mcp-server.md."""
    devnull_fd = None
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, stdout.fileno())
    except (AttributeError, OSError, ValueError):
        return
    finally:
        if devnull_fd is not None:
            os.close(devnull_fd)


def _use_utf8_transport(stdin: TextIO) -> None:
    """Pins stdin to UTF-8, decoding an undecodable byte to a surrogate instead of raising."""
    reconfigure = getattr(stdin, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="surrogateescape")


def serve(
    store: Path, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, read_only: bool = False
) -> int:
    """Runs the MCP stdio loop until stdin closes; the only writer to stdout."""
    _use_utf8_transport(stdin)
    try:
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except (json.JSONDecodeError, RecursionError) as exc:
                # RecursionError: the stdlib json decoder has no depth cap, so a
                # pathologically nested line blows the stack before JSONDecodeError —
                # same fate as any other unparseable line
                _write(stdout, _error_response(None, PARSE_ERROR, f"parse error: {exc}"))
                continue

            response = _dispatch(message, store, read_only)
            if response is not None:
                _write(stdout, response)
    except BrokenPipeError:
        # client already gone — a clean teardown, not a crash; stderr only
        _mute_broken_stdout(stdout)
        print("engmem-mcp: downstream pipe closed — stopping", file=sys.stderr)
        return 0

    return 0
