"""Two documentation claims are checkable without guessing at prose: a private name must exist, a
quoted constant must match."""

from __future__ import annotations

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
    ("scoring", "FIELD_WEIGHTS", "entities", "returns `{value}.0,", "scoring.md"),
    ("scoring", "FIELD_WEIGHTS", "id", "caps a token at {value}", "scoring.md"),
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
    flags = {o for a in parser._actions for o in a.option_strings}
    for sub in subcommands:
        flags |= {
            o
            for a in next(
                x.choices[sub] for x in parser._actions
                if hasattr(x, "choices") and x.choices and sub in x.choices
            )._actions
            for o in a.option_strings
        }

    anatomy = (ROOT / "docs" / "engmem-anatomy.html").read_text(encoding="utf-8")
    cited_subs = set(re.findall(r"engmem (search|roles|install|uninstall|mcp|telemetry|backfill)\b", anatomy))
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
