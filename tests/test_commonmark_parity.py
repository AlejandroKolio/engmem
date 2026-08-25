"""Our regex section splitter, kept beside a real CommonMark parser.

`sections.py` finds headings with regexes rather than parsing markdown. That is a
deliberate trade — the roles, oversized-section splitting and locators around it are
engmem's own and no parser supplies them — but it is the kind of trade that rots
silently: markdown has more edge cases than anyone holds in their head. Three real
defects were found in one afternoon by running exactly this comparison (a heading
inside a code fence, a closed ATX heading keeping its hashes, setext headings not
recognised at all), so it is a test rather than an afternoon.

The parser is a dev-only dependency: engmem's runtime still requires nothing but
`pyyaml`. When it is absent the check skips loudly rather than passing vacuously.

Two divergences are intentional and asserted as such. If either ever starts agreeing,
that is also news — it means our policy was lost in an edit.
"""

import pytest

from engmem.sections import split_sections

markdown_it = pytest.importorskip(
    "markdown_it",
    reason="dev-only CommonMark parity check; install the dev dependency group to run it",
)

_MD = markdown_it.MarkdownIt("commonmark")


def _parser_h2(text: str) -> list[str]:
    tokens = _MD.parse(text)
    return [
        tokens[i + 1].content
        for i, t in enumerate(tokens)
        if t.type == "heading_open" and t.tag == "h2"
    ]


def _our_h2(text: str) -> list[str]:
    return [s.heading for s in split_sections(text) if s.heading]


AGREE = {
    "plain atx": "## Architecture\n\nProse.\n\n## Testing\n\nMore.\n",
    "heading inside a backtick fence": (
        "## Architecture\n\np\n\n```bash\n## shell output, not a heading\n```\n\ntail\n"
    ),
    "heading inside a tilde fence": "## Architecture\n\np\n\n~~~\n## fenced\n~~~\n",
    "fence nested in a longer fence": "## A\n\n````\n```\n## deep\n```\n````\n",
    "four-space indented code": "## Architecture\n\np\n\n    ## indented code\n\ntail\n",
    "closed atx heading": "## Architecture ##\n\np\n",
    "hash inside the heading text": "## Migrating to C#\n\np\n",
    "setext heading": "Architecture\n------------\n\np\n",
    "thematic break after a blank line": "## Architecture\n\np\n\n---\n\ntail\n",
}


@pytest.mark.parametrize("name", sorted(AGREE))
def test_our_splitter_agrees_with_commonmark(name):
    text = AGREE[name]
    assert _our_h2(text) == _parser_h2(text)


def test_unclosed_fence_diverges_on_purpose():
    """CommonMark runs an unclosed fence to end of document, so the parser never emits
    the headings after it. Here that would mean one stray ``` silently hides the rest
    of a document — the whole-document loss this tool exists to catch. We suppress only
    between matched fences, so an unpaired one costs nothing."""
    text = "## Architecture\n\n```\nstray fence, never closed\n\n## Testing\n\nc\n"

    assert _our_h2(text) == ["Architecture", "Testing"]
    assert _parser_h2(text) == ["Architecture"]


def test_blockquoted_heading_diverges_on_purpose():
    """A heading inside a blockquote is quoted material — another document's heading,
    cited. Opening a section there would file our prose under someone else's title."""
    text = "## Architecture\n\n> ## quoted from elsewhere\n\ntail\n"

    assert _our_h2(text) == ["Architecture"]
    assert _parser_h2(text) == ["Architecture", "quoted from elsewhere"]
