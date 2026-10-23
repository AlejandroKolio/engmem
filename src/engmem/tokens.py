"""How text becomes search tokens: one rule set for spine fields, section text and queries
(contracts/scoring.md, "Tokenisation")."""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterable


# Runs over a per-character class string, not the token itself: "A" upper/title-case letter,
# "a" any other letter, "0" number, "m" combining mark. Same shape as the ASCII-era
# `[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+`, with a mark riding on its base character.
_CAMEL_CLASS_RE = re.compile(r"(?:Am*)+(?=Am*a)|(?:Am*)?(?:am*)+|(?:Am*)+|(?:0m*)+")


# planes holding category M today; `test_the_mark_scan_covers_every_plane_with_marks` fails
# when a Unicode version adds a mark elsewhere. Scanning 3 planes, not 17, is ~6x cheaper.
_MARK_PLANES = (0, 1, 14)


# modifier letters (category Lm, so inside `\w`) used as apostrophes: Ukrainian `обʼєкт` is
# written with U+02BC as often as with `'` or `’`, and all forms must split alike
_APOSTROPHE_LETTERS = "\u02bc\u02bb\u02bd\u02ee"


def _mark_ranges(code_points: Iterable[int]) -> list[tuple[int, int]]:
    """Inclusive code-point ranges of the combining marks (category M) among `code_points`."""
    ranges: list[list[int]] = []
    for cp in code_points:
        if not unicodedata.category(chr(cp)).startswith("M"):
            continue
        if ranges and ranges[-1][1] == cp - 1:
            ranges[-1][1] = cp
        else:
            ranges.append([cp, cp])
    return [(lo, hi) for lo, hi in ranges]


@functools.cache
def _token_run_re() -> re.Pattern[str]:
    """Letters, numbers and combining marks; built once, from this Python's Unicode database."""
    # `\w` covers letters and numbers in every plane but not marks, and splitting on a mark cuts
    # a word that has no precomposed form in two; `_` is excluded so snake_case still splits
    planes = (range(plane << 16, (plane + 1) << 16) for plane in _MARK_PLANES)
    ranges = _mark_ranges(cp for plane in planes for cp in plane)
    # as ranges, not 2,000-odd single characters: the regex engine scans a range set 4x faster
    marks = "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in ranges)
    return re.compile(rf"(?:[^\W_{_APOSTROPHE_LETTERS}]|[{marks}])+")


def normalize_token(token: str) -> str:
    return unicodedata.normalize("NFKC", token).casefold()


def tokenize_raw(text: str) -> list[str]:
    # NFKC before splitting: it composes `e` + U+0301 into `é` and folds fullwidth forms and
    # ligatures, so the split sees one spelling of each word
    normalized = unicodedata.normalize("NFKC", text)
    return _token_run_re().findall(normalized)


def _char_class(char: str) -> str:
    category = unicodedata.category(char)
    if category in ("Lu", "Lt"):
        return "A"
    if category[0] == "L":
        return "a"
    if category[0] == "N":
        return "0"
    return "m"


def camel_fragments(raw_token: str) -> list[str] | None:
    # fast path for the common prose word: every character would classify as "a", one part
    if raw_token.isalpha() and raw_token.islower():
        return None
    classes = "".join(_char_class(c) for c in raw_token)
    parts = [raw_token[m.start():m.end()] for m in _CAMEL_CLASS_RE.finditer(classes)]
    if len(parts) <= 1:
        return None
    return parts


def camel_expansion(raw_token: str) -> tuple[list[str], str] | None:
    # normalized fragments + initial-letter acronym, e.g. "WidgetCache" ->
    # (["widget", "cache"], "wc"); one shared rule so field/body/query agree
    frac = camel_fragments(raw_token)
    if not frac:
        return None
    parts_norm = [normalize_token(p) for p in frac]
    acronym = "".join(p[0] for p in parts_norm if p)
    return parts_norm, acronym


def is_restricted(token: str) -> bool:
    # too short/generic to trust past an exact, unsplit match (§7); shared by
    # the spine and body indexes so restricted-ness never disagrees between them
    return len(token) <= 3 or token.isdigit()
