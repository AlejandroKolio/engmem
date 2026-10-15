r"""The one invariant the test harness itself owes every other module: `write_file` puts the
bytes it was handed on disk. See CONTRIBUTING.md, "What the tests enforce"."""

from __future__ import annotations

import pytest

from support.docs import write_file


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("lf\nlf\n", id="lf"),
        pytest.param("crlf\r\ncrlf\r\n", id="crlf"),
        pytest.param("cr\rcr\r", id="cr"),
        pytest.param("crlf\r\nlf\ncr\r", id="mixed"),
        pytest.param("\ufeffbom\n", id="bom"),
        pytest.param("no line break at all", id="no-line-break"),
        pytest.param("na\u00efve caf\u00e9\n", id="non-ascii"),
    ],
)
def test_write_file_puts_exactly_the_given_bytes_on_disk(tmp_path, text):
    r"""Regression for the Windows CI leg: `write_text` translated every `\n` to `\r\n` there, so
    a test that meant to pin an LF document was silently handed a CRLF one."""
    path = write_file(tmp_path, "widget-cache-warmup.md", text)

    assert path.read_bytes() == text.encode("utf-8")
