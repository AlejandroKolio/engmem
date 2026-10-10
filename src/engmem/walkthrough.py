"""`engmem walkthrough` — demos of the capture -> recall cycle in real agent sessions, against a
store, client settings and workspaces of their own. `codex` walks one cycle in Codex; `handoff`
(engmem.handoff) passes a task between Codex and Claude Code. Why they are shaped this way:
docs/walkthrough-codex.md, docs/walkthrough-handoff.md and contracts/install.md, "Walkthrough"."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from engmem import handoff
from engmem.demo import (
    ORDER_BOOK,
    SESSION_1_DECISION,
    SESSION_1_START,
    Check,
    claim,
    codex_home_paths,
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
from engmem.feedback import _finds
from engmem.install import _ensure_store, _SetupError, shell_argument
from engmem.runtime import fail
from engmem.spine import Doc, load_store
from engmem.telemetry import read_session_rows

MARKER_TEXT = "engmem walkthrough codex\n"
FIXTURE_KEY = "request_id"
CHECK_SECONDS = 60.0

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
    "demo_orders/orders.py": ORDER_BOOK,
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
            self.store, self.store / "sessions", *codex_home_paths(self.codex_home),
            *workspace_paths(self.workspace, list(WORKSPACE_FILES)),
        ]
        return list(dict.fromkeys(paths))

    @property
    def env(self) -> dict[str, str]:
        return {"CODEX_HOME": str(self.codex_home), "ENGMEM_HOME": str(self.store)}


def cmd_walkthrough(args: argparse.Namespace) -> int:
    demo = DEMOS.get(args.client)
    if demo is None:
        fail(
            f"engmem walkthrough: no walkthrough for {args.client!r} — one of: "
            f"{', '.join(DEMOS)}"
        )
        return 2
    # resolved once, so every path the demo config names is the one the clients and doctor compare
    root = Path(os.path.realpath(os.path.expanduser(args.dir)))
    set_up, verify = demo
    if args.verify:
        return verify(root)
    try:
        return set_up(root)
    except _SetupError as exc:
        fail(f"engmem walkthrough: {exc}")
        return 2


def _rerun(layout: Layout) -> str:
    return f"engmem walkthrough codex --dir {shell_argument(str(layout.root))}"


def _set_up_codex(root: Path) -> int:
    layout = Layout.under(root)
    claim(layout.root, layout.written_through, MARKER_TEXT)
    _ensure_store(layout.store)
    workspace_created = write_workspace(layout.workspace, WORKSPACE_FILES)
    wiring = wire_codex_home(layout.codex_home, layout.store)
    print(f"engmem walkthrough codex: {layout.root}")
    print(
        f"demo store: {layout.store} — every engmem step below uses it; your own store and "
        f"your own Codex settings are not changed"
    )
    print(f"workspace: {layout.workspace} ({'created' if workspace_created else 'kept as it is'})")
    for line in wiring:
        print(line)
    reasons = doctor_findings(
        "step 0 — check the demo wiring:", "codex", layout.store, layout.env, layout.root
    )
    if reasons:
        not_ready(reasons, _rerun(layout), "Codex has not been started on the demo")
        return 1
    _print_steps(layout)
    return 0


def _print_steps(layout: Layout) -> None:
    store = shell_argument(str(layout.store))
    print("ready: the demo wiring has no blocking finding. In a new terminal:")
    print("step 1 — start Codex on the demo workspace:")
    print(f"  cd {shell_argument(str(layout.workspace))}")
    for line in env_command(layout.env, "codex"):
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


def _verify_codex(root: Path) -> int:
    layout = Layout.under(root)
    if marker_of(layout.root) != MARKER_TEXT:
        fail(
            f"engmem walkthrough: {layout.root} was not built by `engmem walkthrough codex` — run "
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
        for group in decisions(doc):
            if FIXTURE_KEY not in group["decision"]:
                continue
            absent = missing_labels(group)
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


DEMOS: dict[str, tuple[Callable[[Path], int], Callable[[Path], int]]] = {
    "codex": (_set_up_codex, _verify_codex),
    "handoff": (handoff.set_up, handoff.verify),
}
