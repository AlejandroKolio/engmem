import pytest

from engmem.sections import (
    CANONICAL_ROLES,
    split_sections,
    sections_by_locator,
    sections_for_role,
)

MAX_SECTION_BYTES = 4096


def test_empty_body_returns_no_sections():
    assert split_sections("") == []
    assert split_sections("   \n\n  ") == []


def test_body_with_no_h2_heading_becomes_one_section():
    sections = split_sections("Just some prose with no headings at all.")
    assert len(sections) == 1
    assert sections[0].body == "Just some prose with no headings at all."
    assert sections[0].level == 2


def test_split_on_h2_only_when_under_threshold():
    body = """## Pre-reg

Add caching in front of the read endpoints.

## Decision Log

Reused the existing headers instead of a new table.

## Landmines

Lookups must be memoized per request.
"""
    sections = split_sections(body)

    assert [s.heading for s in sections] == ["Pre-reg", "Decision Log", "Landmines"]
    assert [s.level for s in sections] == [2, 2, 2]
    assert sections[0].body == "Add caching in front of the read endpoints."
    assert sections[1].body == "Reused the existing headers instead of a new table."
    assert all(s.size_bytes == len(s.body.encode("utf-8")) for s in sections)


def test_h3_headings_are_not_split_when_section_is_small():
    body = """## Reference

### Sub A

Small.

### Sub B

Also small.
"""
    sections = split_sections(body)
    # under 4096 bytes: the ## section stays whole, ### is not a split point
    assert len(sections) == 1
    assert sections[0].heading == "Reference"
    assert sections[0].level == 2
    assert "### Sub A" in sections[0].body


def _big_paragraph(label: str, repeats: int = 400) -> str:
    return " ".join(f"{label} sentence number {i} about widget cache internals." for i in range(repeats))


def test_oversized_section_splits_on_h3():
    # each chunk alone stays under the cap; only their sum (as one "##" section)
    # crosses it, so the test actually exercises the split rather than merely
    # reproducing "stays whole because nothing fits regardless"
    sub_a_text = _big_paragraph("subA", 70)
    sub_b_text = _big_paragraph("subB", 70)
    assert len(sub_a_text.encode("utf-8")) <= MAX_SECTION_BYTES
    assert len(sub_b_text.encode("utf-8")) <= MAX_SECTION_BYTES
    combined_size = len((sub_a_text + sub_b_text).encode("utf-8"))
    assert combined_size > MAX_SECTION_BYTES

    body = f"""## Reference

### Sub A

{sub_a_text}

### Sub B

{sub_b_text}
"""
    sections = split_sections(body)

    # the oversized ## section is replaced by its ### children — there is no text
    # before the first ### here, so no lead-in section carries the parent heading
    headings = [s.heading for s in sections]
    assert headings == ["Sub A", "Sub B"]
    sub_a = next(s for s in sections if s.heading == "Sub A")
    sub_b = next(s for s in sections if s.heading == "Sub B")
    assert sub_a.level == 3
    assert sub_b.level == 3
    assert sub_a.body == sub_a_text
    assert sub_b.body == sub_b_text
    for s in sections:
        assert s.size_bytes <= MAX_SECTION_BYTES, (
            f"section {s.heading!r} is {s.size_bytes} bytes, still over the cap"
        )


def test_oversized_section_with_lead_in_text_keeps_parent_heading_for_the_lead():
    lead_text = _big_paragraph("lead", 78)
    sub_a_text = "Small piece A."
    body = f"""## Reference

{lead_text}

### Sub A

{sub_a_text}
"""
    sections = split_sections(body)

    headings = [s.heading for s in sections]
    assert headings == ["Reference", "Sub A"]
    lead_section, sub_a = sections
    assert lead_section.level == 2
    assert lead_section.body == lead_text
    assert sub_a.level == 3
    assert sub_a.body == sub_a_text


def test_oversized_section_without_h3_stays_whole():
    big = _big_paragraph("lonely", 400)
    body = f"""## Reference

{big}
"""
    sections = split_sections(body)

    assert len(sections) == 1
    assert sections[0].heading == "Reference"
    assert sections[0].level == 2
    assert sections[0].size_bytes > MAX_SECTION_BYTES


def test_anchor_is_stable_across_heading_renumbering():
    body_v1 = "## 9. Production Considerations\n\nGuard the job.\n"
    body_v2 = "## 10. Production Considerations\n\nGuard the job.\n"

    s1 = split_sections(body_v1)[0]
    s2 = split_sections(body_v2)[0]

    assert s1.anchor == s2.anchor == "production-considerations"


def test_locator_combines_index_and_anchor():
    body = "## 9. Production Considerations\n\nGuard the job.\n"
    sections = split_sections(body)

    assert sections[0].index == 1
    assert sections[0].locator == "§1-production-considerations"


def test_unmapped_heading_has_no_canonical_alias():
    # "Pre-reg" itself now has a canonical alias ("prereg") — this heading is chosen
    # to be document-specific and outside the measured vocabulary instead.
    body = "## Rollout Plan\n\nSome content.\n"
    section = split_sections(body)[0]
    assert section.canonical is None


def test_both_literal_slug_and_canonical_alias_resolve():
    body = "## 8. Decision Log\n\nRejected the alternative.\n"
    sections = split_sections(body)
    index = sections_by_locator(sections)

    assert index["decision-log"] is sections[0]
    assert index["decisions"] is sections[0]


def test_sections_are_returned_in_document_order_with_sequential_index():
    body = """## First

One.

## Second

Two.

## Third

Three.
"""
    sections = split_sections(body)
    assert [s.index for s in sections] == [1, 2, 3]
    assert [s.heading for s in sections] == ["First", "Second", "Third"]


def test_preamble_before_first_heading_is_captured():
    body = "Some intro text before any heading.\n\n## Pre-reg\n\nBody.\n"
    sections = split_sections(body)

    assert len(sections) == 2
    assert "intro text" in sections[0].body
    assert sections[1].heading == "Pre-reg"


@pytest.mark.parametrize(
    "heading_line,expected_canonical",
    [
        # decisions
        pytest.param("## 8. Decision Log", "decisions", id="decisions-ordinal"),
        pytest.param("## Decision Log & Scope", "decisions", id="decisions-scope-suffix"),
        pytest.param("## Decision Log", "decisions", id="decisions-bare"),
        # summary
        pytest.param("## 1. Executive Summary", "summary", id="summary-ordinal"),
        # testing
        pytest.param("## Testing Knowledge", "testing", id="testing-bare"),
        # keywords
        pytest.param("## 14. Search Keywords", "keywords", id="keywords-ordinal"),
        # cheatsheet
        pytest.param("## 15. One-Page Cheat Sheet", "cheatsheet", id="cheatsheet-ordinal"),
        # production
        pytest.param("## 9. Production Considerations", "production", id="production-ordinal"),
        # requirements
        pytest.param("## Functional Requirements", "requirements", id="requirements-bare"),
        # graph
        pytest.param("## Knowledge Graph", "graph", id="graph-bare"),
        # context
        pytest.param("## Business Context", "context", id="context-business"),
        pytest.param(
            "## Business Context (glossary)", "context", id="context-business-glossary-paren"
        ),
        pytest.param(
            "## Domain & Technical Glossary", "context", id="context-domain-technical-glossary"
        ),
        pytest.param("## Glossary", "context", id="context-glossary-bare"),
        # architecture
        pytest.param("## System Architecture", "architecture", id="architecture-system"),
        pytest.param(
            "## Architecture (the read path)", "architecture", id="architecture-read-path-paren"
        ),
        pytest.param(
            "## System Architecture (data flow)",
            "architecture",
            id="architecture-system-data-flow-paren",
        ),
        # primer
        pytest.param(
            "## 13. Future LLM Context (cold-start primer)",
            "primer",
            id="primer-ordinal-cold-start-paren",
        ),
        pytest.param("## Cold-start primer", "primer", id="primer-cold-start-bare"),
        pytest.param(
            "## Future-LLM Cold-Start Primer", "primer", id="primer-future-llm-cold-start"
        ),
        # acceptance
        pytest.param(
            "## Acceptance Criteria Analysis", "acceptance", id="acceptance-analysis"
        ),
        pytest.param("## Acceptance Criteria Status", "acceptance", id="acceptance-status"),
        pytest.param(
            "## Acceptance Criteria (live status -> overview)",
            "acceptance",
            id="acceptance-live-status-paren",
        ),
        # lessons (including the landmines merge)
        pytest.param("## 11. Lessons Learned", "lessons", id="lessons-ordinal"),
        pytest.param("## Lessons Learned & Traps", "lessons", id="lessons-traps"),
        pytest.param(
            "## Lessons Learned / Defects Caught", "lessons", id="lessons-defects-caught"
        ),
        pytest.param("## Landmines", "lessons", id="lessons-landmines-merge"),
        # flow
        pytest.param("## End-to-End Flow", "flow", id="flow-e2e"),
        pytest.param("## End-to-End Business Flow", "flow", id="flow-e2e-business"),
        pytest.param("## End-to-End Flow (call path)", "flow", id="flow-e2e-call-path-paren"),
        pytest.param(
            "## End-to-End Flow (how a dependency CVE blocks a deploy here)",
            "flow",
            id="flow-e2e-cve-paren",
        ),
        # code
        pytest.param("## Code Implementation", "code", id="code-implementation"),
        pytest.param(
            "## Code Implementation (the actual change - full diff)",
            "code",
            id="code-implementation-full-diff-paren",
        ),
        # status
        pytest.param("## Status at a Glance", "status", id="status-glance-capital"),
        pytest.param("## Status at a glance", "status", id="status-glance-lower"),
        pytest.param("## 0. Status Board & TODO", "status", id="status-board-ordinal"),
        # engmem's own operational sections
        pytest.param("## Pre-reg", "prereg", id="prereg"),
        pytest.param("## Reuse Log", "reuse", id="reuse-log"),
        pytest.param("## Search Trace", "trace", id="search-trace"),
    ],
)
def test_canonical_alias_table_covers_measured_corpus_vocabulary(heading_line, expected_canonical):
    # Every spelling variant listed for the 19 roles measured on the real 9-document
    # corpus (see sections.py's CANONICAL_ALIASES block comment) resolves to its
    # intended canonical role, exactly as the vocabulary-expansion task measured it.
    body = f"{heading_line}\n\nSome content.\n"
    section = split_sections(body)[0]
    assert section.canonical == expected_canonical


@pytest.mark.parametrize(
    "heading_line,expected_canonical",
    [
        pytest.param(
            "## System Architecture (the read path)", "architecture", id="architecture-paren"
        ),
        pytest.param(
            "## End-to-End Flow (how a dependency CVE blocks a deploy here)",
            "flow",
            id="flow-paren",
        ),
        pytest.param(
            "## Code Implementation (the actual change - widget cache rewrite)",
            "code",
            id="code-paren",
        ),
        pytest.param(
            "## Acceptance Criteria (live status -> overview)",
            "acceptance",
            id="acceptance-paren",
        ),
        pytest.param("## Business Context (glossary)", "context", id="context-paren"),
    ],
)
def test_trailing_parenthetical_headings_fold_onto_their_bracket_free_canonical(
    heading_line, expected_canonical
):
    # A heading that differs from a known role's key only by a trailing "(...)" clause
    # resolves to that role without the clause's own text ever being enumerated.
    body = f"{heading_line}\n\nSome content.\n"
    section = split_sections(body)[0]
    assert section.canonical == expected_canonical


@pytest.mark.parametrize(
    "heading_line",
    [
        # extra trailing words, no bracket: not a parenthetical variant of "testing knowledge"
        pytest.param("## Testing Knowledge Gaps", id="testing-knowledge-gaps"),
        # extra trailing words, no bracket: not a parenthetical variant of "decision log"
        pytest.param("## Decision Log Review Notes", id="decision-log-review-notes"),
        # extra trailing words, no bracket: not a variant of "lessons learned"
        pytest.param("## Lessons Learned From Landmines", id="lessons-learned-from-landmines"),
        # has a trailing bracket, but the bracket-free remainder ("status report") was
        # never enumerated as a role key, so the fold has nothing to land on
        pytest.param("## Status Report (Q3)", id="status-report-q3-paren"),
    ],
)
def test_near_miss_headings_do_not_fold_to_a_canonical_role(heading_line):
    # Plausible near-misses that must stay unmapped: extra trailing words (not a
    # bracketed clause) are not folded, and a bracket alone does not guarantee a hit —
    # the bracket-free remainder still has to be a literal, enumerated table key.
    body = f"{heading_line}\n\nSome content.\n"
    section = split_sections(body)[0]
    assert section.canonical is None
    # the literal anchor still works exactly as it does for any unmapped heading
    assert section.anchor


def test_unmapped_near_miss_still_yields_a_working_literal_anchor():
    body = "## Testing Knowledge Gaps\n\nOpen questions about coverage.\n"
    sections = split_sections(body)
    index = sections_by_locator(sections)

    assert sections[0].canonical is None
    assert sections[0].anchor == "testing-knowledge-gaps"
    assert index["testing-knowledge-gaps"] is sections[0]


# ---------------------------------------------------------------------------
# CANONICAL_ROLES — the public vocabulary a caller may address by role (role-addressed retrieval).
# ---------------------------------------------------------------------------


def test_canonical_roles_matches_every_distinct_alias_value():
    from engmem.sections import CANONICAL_ALIASES, CANONICAL_ROLES

    assert set(CANONICAL_ROLES) == set(CANONICAL_ALIASES.values())
    assert len(CANONICAL_ROLES) == 19


def test_canonical_roles_is_sorted_and_has_no_duplicates():
    from engmem.sections import CANONICAL_ROLES

    assert list(CANONICAL_ROLES) == sorted(CANONICAL_ROLES)
    assert len(CANONICAL_ROLES) == len(set(CANONICAL_ROLES))


def test_heading_inside_a_code_fence_is_not_a_section_boundary():
    """A `## ` line inside a fenced block is sample output; splitting there filed prose under a
    phantom section."""
    body = (
        "## Architecture\n\nThe cache warms on boot.\n\n"
        "```bash\n## not a heading: this is shell output\necho hi\n```\n\n"
        "More prose in the same section.\n\n## Testing\n\nCovered.\n"
    )

    sections = split_sections(body)

    assert [s.heading for s in sections] == ["Architecture", "Testing"]
    assert "More prose in the same section." in sections[0].body


def test_tilde_fences_suppress_headings_too():
    body = (
        "## Architecture\n\nProse.\n\n~~~\n## still inside the fence\n~~~\n\n"
        "Tail prose.\n"
    )

    assert [s.heading for s in split_sections(body)] == ["Architecture"]


def test_unclosed_fence_does_not_swallow_the_headings_after_it():
    """CommonMark runs an unclosed fence to EOF, which here would let one stray fence hide every
    later section."""
    body = "## Architecture\n\n```\nstray fence, never closed\n\n## Testing\n\nCovered.\n"

    assert [s.heading for s in split_sections(body)] == ["Architecture", "Testing"]


@pytest.mark.parametrize("role", CANONICAL_ROLES)
def test_every_role_answers_to_its_own_bare_name(role):
    """Other agents write the short form, so `--role testing` used to silently skip a section
    headed plainly `## Testing`."""
    sections = split_sections(f"## {role.title()}\n\nBody text.\n")
    assert sections[0].canonical == role, f"bare heading '## {role.title()}' lost its role"


def test_bare_alias_does_not_displace_an_observed_heading():
    body = "## Decision Log & Scope\n\nWhy we did it.\n\n## Decisions\n\nShort form.\n"

    assert [s.canonical for s in split_sections(body)] == ["decisions", "decisions"]


def test_closed_atx_heading_does_not_keep_its_trailing_hashes():
    """`## Architecture ##` is one heading; the stray hashes survived and the canonical lookup
    missed."""
    sections = split_sections("## Architecture ##\n\nBody.\n")

    assert sections[0].heading == "Architecture"
    assert sections[0].canonical == "architecture"


def test_hash_that_is_part_of_the_heading_text_is_kept():
    """Only a space-separated run of trailing hashes closes a heading — `C#` is text."""
    assert split_sections("## Migrating to C#\n\nBody.\n")[0].heading == "Migrating to C#"


def test_setext_headings_are_sections_too():
    """A document written with underlined headings collapsed into one anonymous section, losing
    every role at once."""
    body = "Architecture\n------------\n\nProse.\n\nTesting\n-------\n\nCovered.\n"

    sections = split_sections(body)

    assert [s.heading for s in sections] == ["Architecture", "Testing"]
    assert sections[0].canonical == "architecture"


def test_thematic_break_after_a_blank_line_is_not_a_heading():
    """`---` after a blank line is a horizontal rule, and reading it as a heading would invent
    sections nobody wrote."""
    body = "## Architecture\n\nProse.\n\n---\n\nMore prose.\n"

    assert [s.heading for s in split_sections(body)] == ["Architecture"]


def test_list_item_above_a_dashed_line_is_not_a_heading():
    body = "## Architecture\n\n- an item\n---\n\ntail\n"

    assert [s.heading for s in split_sections(body)] == ["Architecture"]


@pytest.mark.parametrize(
    "heading,expected_anchor",
    [
        pytest.param("Café deployment notes", "cafe-deployment-notes", id="cafe"),
        pytest.param("Müller edge case", "muller-edge-case", id="muller"),
        pytest.param("Naïve retry loop", "naive-retry-loop", id="naive"),
    ],
)
def test_accented_latin_headings_keep_their_letters_in_the_anchor(heading, expected_anchor):
    """`Müller` used to slug to `m-ller`; NFKD reproduces `python-slugify` exactly without the
    dependency."""
    assert split_sections(f"## {heading}\n\nBody.\n")[0].anchor == expected_anchor


def test_a_heading_with_no_sluggable_characters_still_gets_an_anchor():
    assert split_sections("## ***\n\nBody.\n")[0].anchor == "section"


def _oversized(heading: str, lead: str = "") -> str:
    filler = ", ".join(f"`Widget{i}`" for i in range(400))  # comfortably over 4096 bytes
    return f"## {heading}\n\n{lead}### Classes\n\n{filler}\n\n### Endpoints\n\n`/api/v1/x`\n"


def test_split_subsections_inherit_the_parent_role():
    """Splitting is a size decision. A role that survived only on the lead-in vanished
    entirely whenever the section opened straight onto its first `###`."""
    sections = split_sections(_oversized("Search Keywords"))

    assert [s.heading for s in sections] == ["Classes", "Endpoints"]
    assert [s.canonical for s in sections] == ["keywords", "keywords"]
    assert sections_by_locator(sections)["keywords"].heading == "Classes"


def test_a_lead_in_still_takes_the_role_first():
    sections = split_sections(_oversized("Search Keywords", lead="Terms below.\n\n"))

    keywords = sections_for_role(sections, "keywords")
    assert [s.heading for s in keywords] == ["Search Keywords", "Classes", "Endpoints"]


def test_a_subsection_heading_that_names_its_own_role_keeps_it():
    """Inheritance is a fallback, never an override."""
    sections = split_sections(_oversized("Search Keywords").replace("### Endpoints", "### Lessons Learned"))

    by_heading = {s.heading: s.canonical for s in sections}
    assert by_heading["Classes"] == "keywords"
    assert by_heading["Lessons Learned"] == "lessons"


def test_sections_for_role_returns_document_order_and_nothing_for_an_absent_role():
    sections = split_sections(_oversized("Search Keywords"))

    assert [s.anchor for s in sections_for_role(sections, "keywords")] == ["classes", "endpoints"]
    assert sections_for_role(sections, "decisions") == []


def test_a_section_that_names_the_role_outranks_an_inherited_one_earlier_in_the_document():
    """`CANONICAL_ALIASES` maps several headings onto one role, so two sections can both answer
    for it."""
    filler = ", ".join(f"`Term{i}`" for i in range(400))
    body = (
        f"## Business Context\n\n### Actors\n\n{filler}\n\n### Flows\n\nflows\n\n"
        "## Glossary\n\nthe real glossary\n"
    )
    sections = split_sections(body)

    assert [s.canonical for s in sections] == ["context", "context", "context"]
    assert sections_by_locator(sections)["context"].heading == "Glossary"
    # the inherited pieces are still reachable, both by anchor and as the role's content
    assert sections_by_locator(sections)["actors"].heading == "Actors"
    assert [s.heading for s in sections_for_role(sections, "context")] == [
        "Actors", "Flows", "Glossary",
    ]


def test_an_inherited_role_still_answers_when_nothing_else_claims_it():
    sections = split_sections(_oversized("Search Keywords"))

    assert sections_by_locator(sections)["keywords"].heading == "Classes"
