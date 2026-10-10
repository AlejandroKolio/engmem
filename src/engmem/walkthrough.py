"""`engmem walkthrough codex` — a demo of one capture -> recall cycle in a real Codex session, against
a store, a Codex config and a workspace of its own. Why it is shaped this way:
docs/walkthrough-codex.md and contracts/install.md, "Walkthrough"."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from engmem.feedback import _finds
from engmem.install import (
    TriggerRuleOutcome,
    _append_trigger_rule,
    _CODEX_MCP_NOTES,
    _ensure_directory,
    _ensure_store,
    _git_init,
    _install_codex_mcp,
    _SetupError,
    _toml_string,
    shell_argument,
)
from engmem.runtime import fail
from engmem.sections import sections_for_role, split_sections
from engmem.spine import Doc, load_store
from engmem.telemetry import read_session_rows

CLIENTS = ("codex",)
MARKER = ".engmem-walkthrough"
MARKER_TEXT = "engmem walkthrough codex\n"
ABSENT = "not stated in the available material"
FIXTURE_KEY = "request_id"
DOCTOR_SECONDS = 120.0
CHECK_SECONDS = 60.0
# a warning on these means Codex would write elsewhere or not at all, so the demo cannot run
_BLOCKING_WARNINGS = ("warning: sandbox:", "warning: mcp entry:", "warning: mcp command:")
_LABELLED = re.compile(r"^\s*[-*]\s+\**(Decision|Reason|Source)\**\s*:\s*\**\s*(.*?)\s*$")

SESSION_1_START = "$engmem record why OrderBook.place_order keys orders by request_id"
SESSION_1_DECISION = (
    "Decision: repeating an operation with the same request_id does not create a second order. "
    "Reason: a client that times out retries with the same request_id, and a second order would "
    "charge the customer twice. Rejected alternative: a new request_id per attempt, because each "
    "attempt would then place its own order. Source: demo_orders/orders.py, OrderBook.place_order"
)
SESSION_1_SAVE = "$engmem-save-quick"
SESSION_2_TASK = (
    "$engmem add retrying the operation: place_order_with_retry(book, request_id, item, "
    "attempts=3) in demo_orders/orders.py retries book.place_order when it raises TimeoutError"
)
RECALL_QUERY = "retry order request_id"

WORKSPACE_FILES = {
    "README.md": (
        "# demo_orders\n\n"
        "A small order book used by the engmem walkthrough. Run the acceptance check with\n"
        "`python -m demo_orders.check`.\n"
    ),
    "demo_orders/__init__.py": '"""A small order book used by the engmem walkthrough."""\n',
    "demo_orders/orders.py": (
        '"""Orders placed through OrderBook."""\n\n\n'
        "class OrderBook:\n"
        "    def __init__(self):\n"
        "        self.orders = []\n\n"
        "    def place_order(self, request_id, item):\n"
        "        for order in self.orders:\n"
        '            if order["request_id"] == request_id:\n'
        "                return order\n"
        '        order = {"order_id": len(self.orders) + 1, "request_id": request_id, "item": item}\n'
        "        self.orders.append(order)\n"
        "        return order\n"
    ),
    "demo_orders/check.py": (
        '"""Acceptance check: two identical requests leave one order.\n\n'
        'Run from the workspace: python -m demo_orders.check\n"""\n\n'
        "import sys\n\n"
        "from demo_orders import orders\n\n\n"
        "class _LosesFirstReply(orders.OrderBook):\n"
        '    """Places the first order, then loses the reply, as a timed-out call does."""\n\n'
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self.calls = 0\n\n"
        "    def place_order(self, request_id, item):\n"
        "        self.calls += 1\n"
        "        order = super().place_order(request_id, item)\n"
        "        if self.calls == 1:\n"
        '            raise TimeoutError("the reply was lost after the order was placed")\n'
        "        return order\n\n\n"
        "def _repeated_request():\n"
        "    book = orders.OrderBook()\n"
        '    book.place_order("req-1", "book")\n'
        '    book.place_order("req-1", "book")\n'
        "    return len(book.orders)\n\n\n"
        "def _retried_request():\n"
        '    retry = getattr(orders, "place_order_with_retry", None)\n'
        "    if retry is None:\n"
        '        return "place_order_with_retry is not in demo_orders/orders.py yet (session 2 adds it)"\n'
        "    book = _LosesFirstReply()\n"
        "    try:\n"
        '        retry(book, "req-2", "pen")\n'
        "    except Exception as exc:\n"
        '        return f"place_order_with_retry raised {exc!r}"\n'
        "    return len(book.orders)\n\n\n"
        "def main():\n"
        "    results = {\n"
        '        "two identical requests": _repeated_request(),\n'
        '        "a retry after a lost reply": _retried_request(),\n'
        "    }\n"
        "    passed = True\n"
        "    for name, result in results.items():\n"
        "        if result == 1:\n"
        '            print(f"ok: {name}: 1 order")\n'
        "        elif isinstance(result, int):\n"
        '            print(f"FAIL: {name}: {result} orders, expected 1")\n'
        "            passed = False\n"
        "        else:\n"
        '            print(f"FAIL: {name}: {result}")\n'
        "            passed = False\n"
        '    print("acceptance check: PASS" if passed else "acceptance check: FAIL")\n'
        "    return 0 if passed else 1\n\n\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    ),
}


@dataclass(frozen=True)
class Layout:
    root: Path
    store: Path
    codex_home: Path
    workspace: Path

    @classmethod
    def under(cls, root: Path) -> Layout:
        return cls(root, root / "store", root / "codex-home", root / "workspace")

    @property
    def written_through(self) -> list[Path]:
        """Every path the set-up writes into or through, each of which a link could send
        elsewhere: the demo's parts, their files, and each workspace file with its parents."""
        paths = [
            self.store, self.store / "sessions", self.codex_home,
            self.codex_home / "config.toml", self.codex_home / "AGENTS.md", self.workspace,
        ]
        for name in WORKSPACE_FILES:
            relative = Path(name)
            paths += [self.workspace / parent for parent in reversed(relative.parents[:-1])]
            paths.append(self.workspace / relative)
        return list(dict.fromkeys(paths))

    @property
    def env(self) -> dict[str, str]:
        return {"CODEX_HOME": str(self.codex_home), "ENGMEM_HOME": str(self.store)}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"{'ok' if self.passed else 'missing'}: {self.name}: {self.detail}"


def cmd_walkthrough(args: argparse.Namespace) -> int:
    if args.client not in CLIENTS:
        fail(
            f"engmem walkthrough: no walkthrough for {args.client!r} — one of: "
            f"{', '.join(CLIENTS)}"
        )
        return 2
    # resolved once, so every path the demo config names is the one Codex and doctor compare
    layout = Layout.under(Path(os.path.realpath(os.path.expanduser(args.dir))))
    if args.verify:
        return _verify(layout)
    try:
        return _set_up(layout)
    except _SetupError as exc:
        fail(f"engmem walkthrough: {exc}")
        return 2


def _rerun(layout: Layout) -> str:
    return f"engmem walkthrough codex --dir {shell_argument(str(layout.root))}"


def _set_up(layout: Layout) -> int:
    _claim(layout)
    _ensure_store(layout.store)
    workspace_created = _write_workspace(layout.workspace)
    wiring = _wire_codex_home(layout)
    print(f"engmem walkthrough codex: {layout.root}")
    print(
        f"demo store: {layout.store} — every engmem step below uses it; your own store and "
        f"your own Codex settings are not changed"
    )
    print(f"workspace: {layout.workspace} ({'created' if workspace_created else 'kept as it is'})")
    for line in wiring:
        print(line)
    if not _doctor_passes(layout):
        return 1
    _print_steps(layout)
    return 0


def _claim(layout: Layout) -> None:
    """Uses `root` only when it is new, empty, or a walkthrough built it, and never through a link."""
    root = layout.root
    marker = root / MARKER
    if root.exists() and not root.is_dir():
        raise _SetupError(f"{root} is not a directory — choose a new directory for the walkthrough")
    if root.is_dir() and not marker.is_file() and any(root.iterdir()):
        raise _SetupError(
            f"{root} is not empty and was not built by `engmem walkthrough` — choose a new "
            f"directory; the walkthrough writes only into one of its own"
        )
    _require_contained(layout)
    _ensure_directory(root, "walkthrough directory")
    if not marker.is_file():
        _write_new(marker, MARKER_TEXT)


def _require_contained(layout: Layout) -> None:
    """Refuses a link, or a junction or any other path that resolves outside the demo."""
    root = os.path.realpath(layout.root)
    for part in layout.written_through:
        if part.is_symlink():
            raise _SetupError(
                f"{part} is a symbolic link — the demo must stay inside {layout.root}; remove the "
                f"link and re-run"
            )
        if os.path.commonpath([root, os.path.realpath(part)]) != root:
            raise _SetupError(
                f"{part} resolves to {os.path.realpath(part)}, outside {layout.root} — the demo "
                f"must stay inside it; remove the link or junction and re-run"
            )


def _write_new(path: Path, text: str) -> None:
    """A file the walkthrough creates and owns; it is never written over an existing one."""
    try:
        with open(path, "xb") as f:
            f.write(text.encode("utf-8"))
    except OSError as exc:
        raise _SetupError(f"cannot write {path}: {exc}") from exc


def _write_workspace(workspace: Path) -> bool:
    """Writes only the files that are missing, so a re-run keeps what the agent changed."""
    created = not workspace.exists()
    for name, text in WORKSPACE_FILES.items():
        path = workspace / name
        if path.exists():
            continue
        _ensure_directory(path.parent, "workspace directory")
        _write_new(path, text)
    _git_init(workspace, "the demo workspace")
    return created


def _base_codex_config(store: Path) -> str:
    quoted = _toml_string(str(store))
    return (
        "# Codex settings for the engmem walkthrough, read only when CODEX_HOME names this "
        "directory\n"
        'sandbox_mode = "workspace-write"\n\n'
        "[sandbox_workspace_write]\n"
        f"writable_roots = [{quoted}]\n\n"
        "[shell_environment_policy]\n"
        f"set = {{ ENGMEM_HOME = {quoted} }}\n\n"
    )


def _wire_codex_home(layout: Layout) -> list[str]:
    """The demo's own CODEX_HOME: sandbox settings once, then install's idempotent MCP entry and
    trigger rule; the engineer's own ~/.codex is never opened."""
    config = layout.codex_home / "config.toml"
    if not config.exists():
        _ensure_directory(layout.codex_home, "Codex config directory")
        _write_new(config, _base_codex_config(layout.store))
    outcome = _install_codex_mcp(layout.store, config)
    instructions = layout.codex_home / "AGENTS.md"
    rule = _append_trigger_rule(instructions)
    rule_note = "already present" if rule is TriggerRuleOutcome.PRESENT else str(rule)
    return [
        f"codex config: {_CODEX_MCP_NOTES[outcome]} in {config}",
        f"codex instructions: trigger rule {rule_note} in {instructions}",
    ]


def _powershell_literal(value: str) -> str:
    """A single-quoted PowerShell string: nothing in it is interpolated, `'` is doubled."""
    return "'" + value.replace("'", "''") + "'"


def _env_command(env: dict[str, str], command: str) -> list[str]:
    """`command` run with `env`, as lines to print: one for a POSIX shell; on Windows the
    PowerShell line, what it leaves behind, and the cmd.exe form."""
    if sys.platform != "win32":
        return ["".join(f"{name}={shell_argument(value)} " for name, value in env.items()) + command]
    powershell = "".join(f"$env:{name} = {_powershell_literal(value)}; " for name, value in env.items())
    cmd = "".join(f'set "{name}={value}" && ' for name, value in env.items())
    return [
        powershell + command,
        "  (PowerShell; the variables stay set in that window until you close it)",
        f"  cmd.exe: {cmd}{command}",
    ]


def _doctor_passes(layout: Layout) -> bool:
    """Runs `engmem doctor` against the demo's own CODEX_HOME and store, printing its report."""
    store = shell_argument(str(layout.store))
    print("step 0 — check the demo wiring:")
    for line in _env_command(layout.env, f"engmem doctor --agent codex --store {store}"):
        print(f"  {line}")
    argv = [
        sys.executable, "-m", "engmem.cli", "doctor", "--agent", "codex",
        "--store", str(layout.store),
    ]
    try:
        completed = subprocess.run(
            argv, cwd=layout.root, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=DOCTOR_SECONDS,
            # PYTHONSAFEPATH: an `engmem/` left in the demo directory must not stand in for the
            # installed package
            env={**os.environ, **layout.env, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1"},
        )
    except subprocess.TimeoutExpired:
        return _doctor_failed(layout, f"did not finish within {DOCTOR_SECONDS:g} s")
    except OSError as exc:
        return _doctor_failed(layout, f"cannot run it ({type(exc).__name__}: {exc})")
    report = completed.stdout.splitlines()
    for line in report:
        print(f"  {line}")
    blocking = [
        line for line in report
        if line.startswith("error:") or line.startswith(_BLOCKING_WARNINGS)
    ]
    if completed.returncode != 0 and not blocking:
        said = (completed.stderr.strip().splitlines() or ["no report"])[-1]
        return _doctor_failed(layout, f"exited {completed.returncode}: {said}")
    if blocking:
        return _not_ready(layout, blocking)
    return True


def _doctor_failed(layout: Layout, cause: str) -> bool:
    reason = f"error: engmem doctor: {cause}"
    print(f"  {reason}")
    return _not_ready(layout, [reason])


def _not_ready(layout: Layout, reasons: list[str]) -> bool:
    """`reasons` are `<status>: <subject>: <detail>` lines already printed above."""
    subjects = ", ".join(reason.split(": ", 2)[1] for reason in reasons)
    print(
        f"not ready: {len(reasons)} finding(s) stop the walkthrough ({subjects}) — fix each as "
        f"its line says, then re-run `{_rerun(layout)}`. Codex has not been started on the demo; "
        f"the walkthrough is not complete"
    )
    return False


def _print_steps(layout: Layout) -> None:
    store = shell_argument(str(layout.store))
    print("ready: the demo wiring has no blocking finding. In a new terminal:")
    print("step 1 — start Codex on the demo workspace:")
    print(f"  cd {shell_argument(str(layout.workspace))}")
    for line in _env_command(layout.env, "codex"):
        print(f"  {line}")
    print(
        "  the first start with this CODEX_HOME asks you to sign in and to trust the folder; "
        "the walkthrough does not copy your Codex login"
    )
    print("step 2 — session 1 saves the decision (skills $engmem, then $engmem-save-quick):")
    print(f"  type: {SESSION_1_START}")
    print(f"  then: {SESSION_1_DECISION}")
    print(f"  then: {SESSION_1_SAVE}   and answer the preview with: ok")
    print("step 3 — see the record in the demo store:")
    print(f'  engmem search "{RECALL_QUERY}" --store {store}')
    print("step 4 — session 2 is a new Codex session (type /new, or quit and repeat step 1):")
    print(f"  type: {SESSION_2_TASK}")
    print("  expect: before it plans, the agent names the record's id and its Source")
    print("step 5 — the example's acceptance check, in the workspace:")
    print("  python -m demo_orders.check")
    print("step 6 — confirm the whole cycle:")
    print(f"  engmem walkthrough codex --verify --dir {shell_argument(str(layout.root))}")


def _verify(layout: Layout) -> int:
    if not (layout.root / MARKER).is_file():
        fail(
            f"engmem walkthrough: {layout.root} was not built by the walkthrough — run "
            f"`{_rerun(layout)}` first"
        )
        return 2
    print(f"engmem walkthrough codex --verify: {layout.root}")
    print(f"demo store: {layout.store}")
    records, record_check = _record_check(layout)
    recalled, recall_check = _recall_check(layout, records)
    checks = [record_check, recall_check, _acceptance_check(layout)]
    for check in checks:
        print(check)
    if _check_was_changed(layout):
        print(
            "note: demo_orders/check.py in the workspace differs from the check engmem ships; "
            "--verify ran engmem's own"
        )
    print(
        "note: the acceptance check imports the workspace's demo_orders, code the agent edited, "
        "and runs it with the Python engmem runs on"
    )
    print(
        "not checked here: that the agent named the record's id and Source in session 2 — "
        "read it in the Codex transcript"
    )
    failed = sum(not check.passed for check in checks)
    if failed or recalled is None:
        print(
            f"NOT COMPLETE: {failed} of {len(checks)} checks did not pass — the walkthrough is "
            f"not complete"
        )
        return 1
    print(
        f"PASS: {recalled.id} was saved with its reason and source, found again by another "
        f"session, and the acceptance check passed"
    )
    return 0


def _decisions(doc: Doc) -> list[dict[str, str]]:
    """Each `Decision:` line of the Decision Log with the `Reason:`/`Source:` lines after it."""
    groups: list[dict[str, str]] = []
    for section in sections_for_role(split_sections(doc.body), "decisions"):
        for line in section.body.splitlines():
            match = _LABELLED.match(line)
            if match is None:
                continue
            label, value = match.group(1).casefold(), match.group(2)
            if label == "decision":
                groups.append({"decision": value})
            elif groups:
                groups[-1].setdefault(label, value)
    return groups


def _stated(value: str | None) -> bool:
    return bool(value) and not value.casefold().startswith(ABSENT)


def _record_check(layout: Layout) -> tuple[list[Doc], Check]:
    """Every published record whose Decision names `FIXTURE_KEY` with a stated Reason and
    Source; session 2 may well save one of its own."""
    sessions = layout.store / "sessions"
    recovery = (
        "repeat step 2 and give the decision, reason and source in your message; if Codex "
        "said the sandbox or MCP refused the write, run step 0 for the reason first"
    )
    result = load_store(sessions)
    if result.scan_error is not None:
        return [], Check("record", False, f"{result.scan_error.message} — {recovery}")
    records: list[Doc] = []
    shown: list[str] = []
    incomplete: list[str] = []
    for doc in result.docs:
        for group in _decisions(doc):
            if FIXTURE_KEY not in group["decision"]:
                continue
            absent = [label for label in ("reason", "source") if not _stated(group.get(label))]
            if doc.status == "draft":
                incomplete.append(f"{doc.id} is still a draft")
            elif absent:
                incomplete.append(f"{doc.id} states no {' and no '.join(absent)}")
            else:
                records.append(doc)
                shown.append(f"{doc.id} — Decision: {group['decision']} Source: {group['source']}")
                break
    if records:
        return records, Check("record", True, "; ".join(shown))
    found = f" ({'; '.join(incomplete)})" if incomplete else ""
    return [], Check(
        "record", False,
        f"no published record in {sessions} has a Decision naming {FIXTURE_KEY} with a stated "
        f"Reason and Source{found} — {recovery}",
    )


def _recall_check(layout: Layout, records: list[Doc]) -> tuple[Doc | None, Check]:
    """The first record another session's search showed as a find, by session id."""
    if not records:
        return None, Check("recall", False, "no saved record to look for — complete step 2 first")
    telemetry = layout.store / "telemetry.jsonl"
    try:
        finds = _finds(read_session_rows(telemetry))
    except (OSError, UnicodeDecodeError) as exc:
        return None, Check("recall", False, f"cannot read {telemetry} ({exc})")
    recalls = sorted(
        (session, record.id)
        for record in records
        for session, shown in finds.items()
        if record.id in shown and not shown[record.id]
    )
    if recalls:
        session, record_id = recalls[0]
        recalled = next(record for record in records if record.id == record_id)
        return recalled, Check("recall", True, f"session {session} was shown {record_id} by its search")
    ids = ", ".join(record.id for record in records)
    return None, Check(
        "recall", False,
        f"no search attributed to another session showed {ids} as a find (a search with no "
        f"--session, the record's own session, or a weak candidate does not count) — repeat "
        f"step 4 in a new Codex session; $engmem creates its draft and searches with --session",
    )


def _check_was_changed(layout: Layout) -> bool:
    path = layout.workspace / "demo_orders" / "check.py"
    try:
        return path.read_bytes() != WORKSPACE_FILES["demo_orders/check.py"].encode("utf-8")
    except OSError:
        return True


def _acceptance_check(layout: Layout) -> Check:
    """Runs the check engmem ships, never the workspace copy, which the agent can edit."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(layout.workspace)}
    env.pop("PYTHONSAFEPATH", None)
    argv = [sys.executable, "-c", WORKSPACE_FILES["demo_orders/check.py"]]
    try:
        completed = subprocess.run(
            argv, cwd=layout.workspace, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=CHECK_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        return Check(
            "acceptance check", False,
            f"the acceptance check did not finish within {CHECK_SECONDS:g} s",
        )
    except OSError as exc:
        return Check("acceptance check", False, f"cannot run the check ({type(exc).__name__}: {exc})")
    if completed.returncode == 0:
        return Check("acceptance check", True, "one order after two identical requests, and after a retry")
    failures = [line for line in completed.stdout.splitlines() if line.startswith("FAIL:")]
    said = "; ".join(failures) or (completed.stderr.strip().splitlines() or ["no output"])[-1]
    return Check(
        "acceptance check", False,
        f"the shipped check exited {completed.returncode}: {said} — finish step 4, then run "
        f"step 5 to see the check's report",
    )
