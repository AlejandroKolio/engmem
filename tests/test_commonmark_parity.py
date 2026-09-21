"""The regex splitter kept beside a real CommonMark parser, which found three defects in one
afternoon."""

import pytest
from markdown_it import MarkdownIt

from engmem.sections import split_sections

_MD = MarkdownIt("commonmark")


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
    "one-space indented atx heading": "## Architecture\n\np\n\n ## Testing\n\nc\n",
    "three-space indented atx heading": "## Architecture\n\np\n\n   ## Testing\n\nc\n",
    "indented setext text line": " Architecture\n------------\n\np\n",
    "indented setext underline": "Architecture\n ------------\n\np\n",
}


@pytest.mark.parametrize("name", sorted(AGREE))
def test_our_splitter_agrees_with_commonmark(name):
    text = AGREE[name]
    assert _our_h2(text) == _parser_h2(text)


def test_unclosed_fence_diverges_on_purpose():
    """CommonMark runs an unclosed fence to EOF, which here would let one stray fence hide the
    rest of a document."""
    text = "## Architecture\n\n```\nstray fence, never closed\n\n## Testing\n\nc\n"

    assert _our_h2(text) == ["Architecture", "Testing"]
    assert _parser_h2(text) == ["Architecture"]


def test_blockquoted_heading_diverges_on_purpose():
    """A quoted heading is another document's; opening a section there files our prose under
    someone else's title."""
    text = "## Architecture\n\n> ## quoted from elsewhere\n\ntail\n"

    assert _our_h2(text) == ["Architecture"]
    assert _parser_h2(text) == ["Architecture", "quoted from elsewhere"]


def test_tab_indented_heading_diverges_on_purpose():
    """`[ \\t]{0,3}` counts a tab as one character where CommonMark counts a 4-column indent, so a
    tab-indented ATX or setext heading opens a section here and indented code there."""
    atx = "## Architecture\n\np\n\n\t## Testing\n\nc\n"
    setext = "## Architecture\n\np\n\n\tTesting\n---\n\nc\n"

    assert _our_h2(atx) == ["Architecture", "Testing"]
    assert _parser_h2(atx) == ["Architecture"]
    assert _our_h2(setext) == ["Architecture", "Testing"]
    assert _parser_h2(setext) == ["Architecture"]
