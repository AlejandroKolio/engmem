"""Two documentation claims are checkable without guessing at prose: a private name must exist, a
quoted constant must match."""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "engmem"
DOCS = (
    [ROOT / "README.md", ROOT / "ENGMEM-SPEC.md", ROOT / "docs" / "engmem-anatomy.html"]
    + sorted((ROOT / "docs" / "design").rglob("*.md"))
)

_CODE_SPAN = re.compile(r"<code>([^<]+)</code>|`([^`\n]+)`")
_PRIVATE = re.compile(r"^_[A-Za-z_][A-Za-z0-9_]*$")


def _defined_names() -> set[str]:
    names: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return names


def _cited_private_names(text: str) -> set[str]:
    spans = (a or b for a, b in _CODE_SPAN.findall(text))
    return {s.strip().removesuffix("()") for s in spans if _PRIVATE.match(s.strip().removesuffix("()"))}


DEFINED = _defined_names()


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_private_names_in_the_docs_still_exist(doc):
    """Prose cannot produce a leading underscore, so anything carrying one is a claim about the
    code."""
    stale = sorted(_cited_private_names(doc.read_text(encoding="utf-8")) - DEFINED)

    assert not stale, (
        f"{doc.relative_to(ROOT)} names {stale}, which no longer exist in engmem. "
        "Rename in the docs, or the reader is sent looking for something gone."
    )


def _constant(module: str, name: str):
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{module}.{name} not found")


def _kb(value: int) -> list[str]:
    """How a byte count is spelled in prose, in both languages."""
    return [str(value), f"{value // 1024} KB", f"{value // 1024} КБ"]


# constant -> the documents that state its value and must keep matching it
# A constant whose value a contract states in prose, with the exact spelling that document uses.
# Prose cannot be diffed, and a document arguing for a tuned value the code no longer holds sends
# the reader to a conclusion the engine does not implement.
STATED_VALUES = [
    ("scoring", "BM25_K1", None, "`k1 = {value}`", "scoring.md"),
    ("scoring", "BM25_B", None, "`b = {value}`", "scoring.md"),
    ("scoring", "MIN_COVERED_WORDS", None, "`MIN_COVERED_WORDS` ({value})", "scoring.md"),
    ("scoring", "SHORT_WORD_MAX_CHARS", None, "`SHORT_WORD_MAX_CHARS` ({value})", "scoring.md"),
    ("scoring", "FIELD_WEIGHTS", "entities", "returns `{value}.0,", "scoring.md"),
    ("scoring", "FIELD_WEIGHTS", "id", "caps a token at {value}", "scoring.md"),
    ("staging", "_LOCK_WAIT_SECONDS", None, "`_LOCK_WAIT_SECONDS` ({value})", "mcp-server.md"),
    ("staging", "_VERSION_HEX_DIGITS", None, "first {value} hex digits", "mcp-server.md"),
    ("doctor", "_PROBE_SECONDS", None, "`_PROBE_SECONDS` ({value})", "doctor.md"),
    ("provenance", "GIT_LOOKUP_SECONDS", None, "`GIT_LOOKUP_SECONDS` ({value})", "provenance.md"),
    ("provenance", "COMMIT_DISPLAY_CHARS", None, "`COMMIT_DISPLAY_CHARS` characters ({value}",
     "provenance.md"),
    ("output", "COVERED_DISPLAY_MAX", None, "`COVERED_DISPLAY_MAX` ({value})", "provenance.md"),
    ("provenance", "MAX_COVERED_FILES", None, "`MAX_COVERED_FILES` ({value})", "provenance.md"),
    ("provenance", "_COVERED_OUTPUT_BYTES", None, "`_COVERED_OUTPUT_BYTES` ({value} bytes",
     "provenance.md"),
    ("feedback", "TEXT_MAX", None, "≤ {value} characters", "data-model.md"),
    ("feedback", "TEXT_MAX", None, "a text over {value} characters", "ENGMEM-SPEC.md"),
]


@pytest.mark.parametrize(
    "module,name,key,template,doc_name",
    STATED_VALUES,
    ids=[f"{row[1]}{'[' + row[2] + ']' if row[2] else ''}" for row in STATED_VALUES],
)
def test_a_contract_that_argues_for_a_value_states_the_one_the_code_holds(
    module, name, key, template, doc_name
):
    value = _constant(module, name)
    if key is not None:
        value = value[key]
    doc = next(d for d in DOCS if d.name == doc_name)

    stated = template.format(value=value)

    assert stated in doc.read_text(encoding="utf-8"), (
        f"{doc_name} no longer states {stated!r} for {module}.{name}"
        f"{'[' + key + ']' if key else ''}, which the code now sets to {value}"
    )


QUOTED_CONSTANTS = [
    ("output", "MAX_OUTPUT_BYTES", ["README.md", "ENGMEM-SPEC.md", "engmem-anatomy.html", "data-model.md"]),
]


@pytest.mark.parametrize("module,name,doc_names", QUOTED_CONSTANTS, ids=lambda a: a if isinstance(a, str) else "")
def test_documents_that_state_a_constant_state_the_current_one(module, name, doc_names):
    """Asserted positively: banning only the old spelling left the docs quoting a dead number
    while staying green."""
    value = _constant(module, name)
    spellings = _kb(value)

    for doc in DOCS:
        if doc.name not in doc_names:
            continue
        text = doc.read_text(encoding="utf-8")
        assert any(s in text for s in spellings), (
            f"{doc.relative_to(ROOT)} documents {module}.{name} but states none of "
            f"{spellings} — the module says {value}"
        )
        # and no other plausible cap is left lying around from a previous value
        for other in (1024, 2048, 8192, 16384):
            if other == value:
                continue
            for stale in _kb(other):
                hits = list(re.finditer(rf"(?<![\d.]){re.escape(stale)}", text))
                assert not hits, (
                    f"{doc.relative_to(ROOT)} still states {stale!r} for {module}.{name}, "
                    f"which is now {value}"
                )


def test_the_role_vocabulary_the_docs_quote_is_the_one_the_code_defines():
    """Nineteen is asserted in several places, and the number moved once already."""
    from engmem.sections import CANONICAL_ROLES

    n = len(CANONICAL_ROLES)
    anatomy = (ROOT / "docs" / "engmem-anatomy.html").read_text(encoding="utf-8")
    spelled = {19: ["nineteen", "девятнадцат"]}.get(n)

    assert spelled, f"CANONICAL_ROLES is now {n}; teach this test the new spelling"
    for word in spelled:
        assert word in anatomy.lower(), f"the page no longer says {word!r} for {n} roles"

def test_cli_commands_and_flags_named_in_the_docs_exist():
    """A renamed flag leaves the page telling the reader to type something the CLI rejects."""
    import re

    from engmem.cli import build_parser

    parser = build_parser()
    subcommands = set(next(
        a.choices for a in parser._actions if hasattr(a, "choices") and a.choices
    ))
    # nested subcommands (`feedback record`, `cost record-baseline`) own their flags too
    flags: set[str] = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            flags |= set(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(action.choices.values())

    anatomy = (ROOT / "docs" / "engmem-anatomy.html").read_text(encoding="utf-8")
    cited_subs = set(re.findall(r"engmem (search|roles|install|uninstall|mcp|telemetry|backfill)\b", anatomy))
    nested = {
        name: set(action.choices)
        for name, sub in next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        ).choices.items()
        for action in sub._actions
        if isinstance(action, argparse._SubParsersAction)
    }
    # every `<code>engmem word [word]` invocation, minus the prompt line the server prints
    invocations = re.findall(
        r"<code>engmem ([a-z][a-z-]*)(?=[ <])(?: ([a-z][a-z-]*)(?=[ <]))?",
        anatomy.replace("engmem mode for this session", ""),
    )
    cited_subs |= {first for first, _ in invocations}
    unknown_nested = sorted(
        f"{first} {second}"
        for first, second in invocations
        if first in nested and second and second not in nested[first]
    )
    assert not unknown_nested, f"the page names subcommands the CLI lacks: {unknown_nested}"
    # only flags written next to an engmem invocation — the page's CSS custom
    # properties (`--accent`, `--bg`) look identical to a long flag
    cited_flags = {
        flag
        for line in anatomy.splitlines()
        if "engmem " in line
        for flag in re.findall(r"(--[a-z][a-z-]+)", line)
    }

    assert cited_subs <= subcommands, f"the page names subcommands the CLI lacks: {cited_subs - subcommands}"
    unknown = cited_flags - flags
    assert not unknown, f"the page names flags the CLI lacks: {sorted(unknown)}"


_NUMBER_WORDS = {15: ("fifteen", "пятнадцать")}


def test_the_subcommand_count_the_docs_state_is_the_one_the_cli_has():
    """The count moved from ten to thirteen without anyone noticing; a stated number is a claim."""
    from engmem.cli import build_parser

    parser = build_parser()
    count = len(next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices)
    words = _NUMBER_WORDS.get(count)
    assert words, f"the CLI now has {count} subcommands; teach this test the new spelling"
    english, russian = words
    spec = (ROOT / "ENGMEM-SPEC.md").read_text(encoding="utf-8")

    assert f"{english} subcommands" in spec
    assert f"{english} subcommands" in ANATOMY_EN, "the English page states another count"
    assert f"{russian} подкоманд" in ANATOMY_RU, "the Russian page states another count"


def test_the_spec_parking_section_exists_and_is_not_empty():
    """An empty parking section means the next proposal argues against nothing."""
    spec = (ROOT / "ENGMEM-SPEC.md").read_text(encoding="utf-8")
    start = spec.index("## 9. Deliberately cut")
    end = spec.index("## 10.", start)
    body = spec[start:end]

    assert len(body.split()) > 80, "the parking section has lost its content"
    assert "Built after all" in body, "entries that left the list must stay recorded with their reason"



def _start_template() -> str:
    return (ROOT / "src" / "engmem" / "templates" / "engmem.start.md").read_text(
        encoding="utf-8"
    )


def _start_step(heading: str, next_heading: str) -> str:
    template = _start_template()
    return template[template.index(heading) : template.index(next_heading)]


def test_the_start_template_asks_for_the_session_id_inside_each_search_bullet():
    """An agent acts on the bullet matching its runtime and stops reading, so the requirement
    has to be part of the call instruction, not a paragraph after the bullets."""
    step3 = _start_step("## 3. Search for prior context", "## 4.")
    bullets = ["- " + b for b in step3.split("\n- ")[1:]]

    cli = next(b for b in bullets if b.startswith("- If you can run shell commands"))
    mcp = next(b for b in bullets if b.startswith("- If your runtime exposes"))

    assert "--session" in cli and "Always" in cli
    assert "`session_id`" in mcp and "Always" in mcp


def test_the_start_template_does_not_excuse_a_search_without_a_session_id():
    """A draft that could not be created is a thing to report; it is never a sanctioned way to
    log a row nothing can attribute."""
    step2 = _start_step("## 2. Create the draft", "## 3.")
    fallback = step2[step2.index("- If you can do neither") :]

    assert "session_id" not in fallback


def test_the_start_template_tells_an_agent_without_a_draft_not_to_pass_an_id():
    """The id is composed in step 2 before the draft is written, so a failed write leaves the
    agent holding one while step 3 says to always pass it. Passing it then attributes the row to
    a document that does not exist -- what the audit reports as an orphan."""
    from engmem.gate1_audit import AuditReport

    step3 = _start_step("## 3. Search for prior context", "## 4.")
    no_draft = next(
        b for b in ("- " + s for s in step3.split("\n- ")[1:])
        if "could not create the draft" in b
    )

    assert "Never pass" in no_draft, "the no-draft branch must forbid passing an id, not sanction it"
    assert "orphan" in no_draft, "and name what such a row becomes in the audit"
    assert "orphan_session_ids" in AuditReport.__dataclass_fields__, (
        "the template's 'orphan' is a claim about gate1_audit, which must still report it"
    )


def test_the_start_templates_read_step_tells_an_agent_without_a_draft_not_to_pass_an_id():
    """Step 4's read instruction carries the session id like step 3's search does, and the same
    exception: with no draft written, an id names a document that does not exist (US-16)."""
    step4 = _start_step("## 4. Load what was found", "## 5.")

    assert "engmem read" in step4 and "--session" in step4
    assert "could not create the" in step4 and "never pass an id" in step4


def test_the_save_templates_cite_a_quote_by_the_response_it_came_from():
    """A read can deliver a later version than the search showed; citing the search's version for
    a quote taken from the read fails its check (US-16, ARCH-001)."""
    root = ROOT / "src" / "engmem" / "templates"
    for name in ("engmem.save.md", "engmem.save.quick.md"):
        text = " ".join((root / name).read_text(encoding="utf-8").split())
        assert "read the quote came from" in text or "quote a read" in text, name


def test_the_save_template_asks_about_a_superseded_citation():
    """A row citing a superseded document is indistinguishable from honest reuse once the session
    is over."""
    template = (ROOT / "src" / "engmem" / "templates" / "engmem.save.md").read_text(
        encoding="utf-8"
    )
    rules_start = template.index("### Reuse Log rules")
    rules = template[rules_start : template.index("## 3.", rules_start)]

    assert "superseded" in rules, "the Reuse Log rules must address citing a stale document"
    assert "harmful" in rules, "and say how such a row is classified"


def test_the_save_template_defines_anti_reuse():
    """The discriminator must be agreement-vs-departure, not impact-cell polarity, which
    inverts when the prior document's own decision was itself a rejection."""
    template = (ROOT / "src" / "engmem" / "templates" / "engmem.save.md").read_text(
        encoding="utf-8"
    )
    rules_start = template.index("### Reuse Log rules")
    rules = template[rules_start : template.index("## 3.", rules_start)]
    normalized = " ".join(rules.split())

    assert "deliberately go the other way" in normalized, (
        "anti-reuse must be defined, not just listed alongside reuse and harmful"
    )
    assert "followed the prior document or went against it" in normalized, (
        "the discriminator must be agreement-vs-departure, stated explicitly"
    )
    assert 'never "so we did X"' not in normalized, (
        "the impact-cell polarity rule inverts when the prior document's own decision was "
        "a rejection -- it must not reappear"
    )


# ---------------------------------------------------------------------------
# claims an agent or contributor acts on literally
# ---------------------------------------------------------------------------

TEMPLATES = ROOT / "src" / "engmem" / "templates"
ANATOMY = (ROOT / "docs" / "engmem-anatomy.html").read_text(encoding="utf-8")
ANATOMY_RU = ANATOMY[ANATOMY.index('<div class="lang-ru"'):ANATOMY.index('<div class="lang-en"')]
ANATOMY_EN = ANATOMY[ANATOMY.index('<div class="lang-en"'):]


@pytest.mark.parametrize(
    "doc", [ROOT / "SECURITY.md", ROOT / ".github" / "pull_request_template.md", ROOT / "README.md"],
    ids=lambda p: p.name,
)
def test_the_runtime_dependencies_named_are_the_two_the_wheel_requires(doc):
    text = doc.read_text(encoding="utf-8")

    assert "pyyaml" in text and "markdown-it-py" in text, doc.name
    assert "one runtime dependency" not in text.lower() and "pyyaml` only" not in text, doc.name


def test_the_anatomy_page_states_two_dependencies_in_both_languages():
    assert "Две зависимости" in ANATOMY_RU and "Одна зависимость" not in ANATOMY_RU
    assert "Two dependencies" in ANATOMY_EN


@pytest.mark.parametrize("template", sorted(TEMPLATES.glob("*.md")), ids=lambda p: p.name)
def test_a_template_naming_a_gate1_tool_says_it_lives_in_the_checkout(template):
    """`tools/` is not in the wheel: a bare `tools/verify_citations.py` resolves nowhere from the
    repository an agent is working in."""
    text = template.read_text(encoding="utf-8")
    for tool in ("tools/verify_citations.py", "tools/gate1_report.py"):
        for line in (line for line in text.splitlines() if tool in line):
            assert f"<engmem-checkout>/{tool}" in line, f"{template.name}: {line.strip()}"


@pytest.mark.parametrize(
    "doc",
    [TEMPLATES / "engmem.save.md", TEMPLATES / "engmem.save.quick.md",
     ROOT / "docs" / "design" / "data-model.md"],
    ids=lambda p: p.name,
)
def test_the_section_list_cites_the_spec_and_no_stale_count(doc):
    text = doc.read_text(encoding="utf-8")

    assert "ENGMEM-SPEC.md` §4" in text, f"{doc.name} must cite ENGMEM-SPEC.md §4 for its sections"
    for count in ("16-section", "six-section", "all 17"):
        assert count not in text, f"{doc.name} restates a section count: {count!r}"


@pytest.mark.parametrize(
    "template", ["engmem.start.md", "engmem.save.md", "engmem.save.quick.md"]
)
def test_the_search_trace_instructions_show_a_bare_value_line(template):
    """The audit reader matches a whole line equal to `shell`, `paste` or `miss`; `- shell` or
    `Trace: shell` escapes it, so the writer has to be shown the exact shape."""
    text = (TEMPLATES / template).read_text(encoding="utf-8")

    # the example may sit indented inside a list item; the lines themselves carry nothing else
    assert re.search(r"^( *)```\n\1## Search Trace\n\n\1shell\n\1```$", text, re.MULTILINE), template


# ---------------------------------------------------------------------------
# US-17..US-21: walkthroughs, the ChatGPT bridge, export, the cache key
# ---------------------------------------------------------------------------


def _flat(text: str) -> str:
    """Prose with its line breaks folded, so a phrase wrapped across two lines still reads whole."""
    return " ".join(text.split())


README = _flat((ROOT / "README.md").read_text(encoding="utf-8"))
SPEC = _flat((ROOT / "ENGMEM-SPEC.md").read_text(encoding="utf-8"))
STORE_README = _flat((ROOT / "docs" / "store-readme.md").read_text(encoding="utf-8"))
_PROSE = {
    "README.md": README, "ENGMEM-SPEC.md": SPEC,
    "anatomy RU": _flat(ANATOMY_RU), "anatomy EN": _flat(ANATOMY_EN),
}


def test_every_walkthrough_the_docs_name_is_a_demo_the_cli_runs():
    """The demo is a positional argument, not a subparser, so the subcommand check above cannot
    see `engmem walkthrough <demo>` naming one the CLI refuses."""
    from engmem.walkthrough import DEMOS

    for name, text in _PROSE.items():
        cited = set(re.findall(r"engmem walkthrough ([a-z][a-z-]*)", text))
        assert cited <= set(DEMOS), f"{name} names walkthroughs the CLI lacks: {cited - set(DEMOS)}"
        missing = {demo for demo in DEMOS if f"engmem walkthrough {demo} --dir" not in text}
        assert not missing, f"{name} does not show how to start {sorted(missing)}"


def test_the_demo_paths_the_docs_name_are_the_ones_the_walkthrough_builds():
    from engmem import handoff, walkthrough

    root = Path("demo")
    codex = walkthrough.Layout.under(root)
    pair = handoff.Layout.under(root)
    in_spec = [
        f"`{codex.store.name}/`", f"`{codex.codex_home.name}/`", f"`{pair.orders.name}/`",
        f"`{pair.storefront.name}/`", f"`{handoff.MCP_CONFIG}`", f"`{handoff.CLAUDE_SETTINGS}`",
    ]
    in_readme = [f"`{pair.storefront.name}/`", f"`{handoff.MCP_CONFIG}`", f"`{handoff.CLAUDE_SETTINGS}`"]

    assert [p for p in in_spec if p not in SPEC] == []
    assert [p for p in in_readme if p not in README] == []
    for half in (ANATOMY_RU, ANATOMY_EN):
        assert f"<code>{pair.storefront.name}/</code>" in half


def _export_parser() -> argparse.ArgumentParser:
    from engmem.cli import build_parser

    subparsers = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return subparsers.choices["export"]


def test_the_export_flags_the_docs_show_are_the_ones_the_cli_takes():
    """Two steps the docs teach by heart: a renamed flag leaves every reader typing a refusal."""
    actions = {
        a.option_strings[-1]: a for a in _export_parser()._actions if a.option_strings
    }
    out, confirm = actions["--out"], actions["--confirm"]

    assert set(actions) == {"--help", "--out", "--confirm", "--store"}
    assert f"engmem export DOC_ID [--out {out.metavar} --confirm {confirm.metavar}]" in SPEC
    assert re.search(r"engmem export \S+ --out \S+ --confirm <fingerprint>", README)
    assert "--out FILE --confirm <fingerprint>" in STORE_README
    assert "--out &lt;файл&gt; --confirm &lt;отпечаток&gt;" in ANATOMY_RU
    assert "--out &lt;file&gt; --confirm &lt;fingerprint&gt;" in ANATOMY_EN


def test_the_export_preview_ends_the_way_the_docs_say(tmp_path):
    """The spec quotes the preview's last lines and the fingerprint's length; both are output."""
    from engmem.export import MARKER, prepare_export, render_preview
    from engmem.spine import load_store

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1000001-widget-cache.md").write_text(
        "---\nid: 1000001-widget-cache\ntitle: Widget cache\ndate: 2026-09-01\nstatus: active\n"
        "related: [1000002-widget-warmup]\n---\n\n## Decision Log\n\nKept the cache.\n",
        encoding="utf-8",
    )
    preview = render_preview(prepare_export(tmp_path, load_store(sessions), "1000001-widget-cache"))
    lines = preview.splitlines()
    fingerprint = next(line for line in lines if line.startswith("fingerprint: ")).split()[1]

    assert lines[-1] == "nothing was written."
    assert "It ends `nothing was written.`" in SPEC and "a `fingerprint:` line" in SPEC
    assert f"the first {len(fingerprint)} hex digits of the SHA-256" in SPEC
    for name, text in _PROSE.items():
        assert MARKER in text, f"{name} no longer names the marker a cut leaves"


def test_no_mcp_tool_takes_a_document_out_of_the_store_as_the_docs_say(tmp_path):
    from engmem.mcp_server import _handle_tools_list

    names = [tool["name"] for tool in _handle_tools_list({}, tmp_path)["tools"]]

    assert not [n for n in names if "export" in n or "walkthrough" in n], names
    assert "There is no MCP tool for it" in README
    assert "There is no MCP tool, so an agent connected over MCP cannot take a document" in SPEC
    assert "There is no MCP tool for export" in ANATOMY_EN
    assert "MCP-инструмента для экспорта нет" in ANATOMY_RU


def test_the_bridge_lines_the_docs_describe_are_the_ones_install_and_doctor_print(tmp_path, capsys):
    """The docs promise that engmem names the public bridge unprotected and `--read-only` not
    privacy; that promise is only as true as the lines install and doctor print."""
    from engmem.doctor import Status, _bridge_findings
    from engmem.install import _print_chatgpt_steps

    _print_chatgpt_steps(tmp_path)
    printed = capsys.readouterr().out.splitlines()
    bridge = next(line for line in printed if line.startswith("bridge: "))
    public = next(line for line in printed if line.startswith("public bridge: "))
    findings = {f.subject: f for f in _bridge_findings()}

    assert "engmem did not verify" in bridge
    assert "is not protected" in public and "only refuses writes" in public
    assert set(findings) == {"tunnel", "public bridge"}
    assert {f.status for f in findings.values()} == {Status.UNVERIFIED}
    assert "Install then prints a `bridge:` line" in SPEC and "`public bridge:` line" in SPEC
    assert "two `unverified` lines" in SPEC and "`tunnel`" in SPEC
    for name, text in (("README.md", README), ("anatomy EN", ANATOMY_EN), ("store-readme", STORE_README)):
        assert "not protected" in text, f"{name} no longer says a public bridge is not protected"
    assert "не защищён" in ANATOMY_RU
    assert "it is not privacy" in README and "is not privacy" in ANATOMY_EN
    assert "приватности он не даёт" in ANATOMY_RU


def test_the_cache_key_the_docs_describe_is_the_one_the_search_uses():
    """US-21 keyed an entry by its body's sha256; a doc still keying it by size and mtime would
    send a reader hunting for a staleness the code no longer has."""
    import hashlib

    from engmem import cache

    scoring = ast.parse((PACKAGE / "scoring.py").read_text(encoding="utf-8"))
    entries_for_doc = next(
        n for n in ast.walk(scoring) if isinstance(n, ast.FunctionDef) and n.name == "_entries_for_doc"
    )
    keyed_by = {
        ast.unparse(call.args[0])
        for call in ast.walk(entries_for_doc)
        if isinstance(call, ast.Call) and ast.unparse(call.func) == "cache.content_key"
    }

    assert keyed_by == {"doc.body"}
    assert cache.content_key("body") == hashlib.sha256(b"body").hexdigest()
    for name, text in _PROSE.items():
        assert "sha256" in text, f"{name} no longer says what the cache is keyed by"
        for stale in ("st_mtime_ns", "st_size", "(mtime_ns, size)"):
            assert stale not in text, f"{name} still keys the cache by {stale!r}"


def test_the_shared_cache_the_docs_warn_about_is_the_one_every_demo_client_uses(tmp_path, monkeypatch):
    """No demo setting names a cache of its own, so any search on the demo store prunes the one
    your store uses; the docs state that residual, and it holds only while this does."""
    import json

    from engmem import cache, handoff, walkthrough

    root = tmp_path / "demo"
    pair = handoff.Layout.under(root)
    project = handoff._claude_project_files(pair.store)
    environments = [
        walkthrough.Layout.under(root).env, pair.codex_env, pair.claude_env,
        json.loads(project[handoff.CLAUDE_SETTINGS])["env"],
        json.loads(project[handoff.MCP_CONFIG])["mcpServers"]["engmem"].get("env", {}),
    ]
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    roots = set()
    for store in (tmp_path / "yours", pair.store):
        monkeypatch.setenv("ENGMEM_HOME", str(store))
        roots.add(cache.cache_root())

    assert [env for env in environments if "XDG_CACHE_HOME" in env] == []
    assert len(roots) == 1
    assert "uses the same `~/.cache/engmem` as your store" in README
    assert "Кэш разбора у демо общий с вашим хранилищем" in _flat(ANATOMY_RU)
    assert "The demo shares the parse cache with your store" in _flat(ANATOMY_EN)
