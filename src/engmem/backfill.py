from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from markdown_it import MarkdownIt

from engmem.cache import identity_for
from engmem.sections import sections_for_role, split_sections
from engmem.spine import (
    Doc,
    parse_document,
    preamble_label_value,
    split_front_matter,
    stated,
)
from engmem.staging import commit, discard, newline_of, read_document, stage


class _NoAliasDumper(yaml.SafeDumper):
    """Stops PyYAML rendering the shared `date`/`task_date` object as a YAML anchor."""

    def ignore_aliases(self, data):
        return True


# under `default_flow_style=None` a mapping with no nested collection dumps flow —
# `{status: active, backfilled: true}` — and a flow mapping appended into a block front matter
# is a syntax error, so every document missing only scalar spine fields failed to write at all.
# Sequences keep the heuristic, so `tags: [widget-cache]` renders exactly as before.
_NoAliasDumper.add_representer(
    dict,
    lambda dumper, data: dumper.represent_mapping(
        "tag:yaml.org,2002:map", data, flow_style=False
    ),
)


def _dump_front_matter(data: dict) -> str:
    return yaml.dump(
        data, Dumper=_NoAliasDumper, default_flow_style=None, sort_keys=False,
        allow_unicode=True,
    )


def _front_matter_mapping(front_matter_text: str) -> dict:
    """A document's own front matter; anything that is not a mapping reads as empty."""
    raw = yaml.safe_load(front_matter_text) if front_matter_text else None
    return raw if isinstance(raw, dict) else {}


# the label only; the rest of the line is its value. Every gap is `[^\S\r\n]`, the same
# "whitespace, but not the line break" class `spine._PREAMBLE_DATE_RE` uses — one rule for
# every preamble label. See contracts/backfill.md
_PREAMBLE_STATUS_RE = re.compile(
    r"^[-*][^\S\r\n]*\*{0,2}Status\*{0,2}[^\S\r\n]*:(.*)$",
    re.MULTILINE,
)
_PREAMBLE_REPOS_RE = re.compile(
    r"^[-*][^\S\r\n]*\*{0,2}Repos?\*{0,2}[^\S\r\n]*:(.*)$",
    re.MULTILINE,
)

# `*` is a list marker only when it is not doubled: `**Classes:**` is a bold label,
# and eating its first asterisk hid the label and sent `*Classes:**` to `entities`
_BULLET_RE = re.compile(r"^\s*(?:[-+•]\s*|\*(?!\*)\s*)")
_BOLD_LABEL_RE = re.compile(r"^\*\*([^*\n]+?)\*\*\s*:?\s*")
_PLAIN_LABEL_RE = re.compile(r"^([A-Z][A-Za-z0-9 /&\-]{0,40}?)\s*:\s+")
_TERM_SEPARATORS = frozenset(",;·|")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_BACKTICKED_RE = re.compile(r"^`([^`]+)`$")

# terms that satisfy _looks_like_entity's shape test but are never real entities
# (e.g. "N/A" would otherwise pass on its "/") — see contracts/backfill.md
_ENTITY_DENYLIST = frozenset({
    "n/a", "na", "tbd", "todo", "etc", "etc.", "none", "n / a",
    "and more", "and others", "and so on",
})


@dataclass
class FieldProposal:
    """One field to add, and the evidence it was derived from."""

    name: str
    value: object
    source: str


@dataclass
class BackfillProposal:
    doc_id: str
    path: Path
    already_complete: bool
    fields: list[FieldProposal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # the file as it was when `load_store` read the body every value below was derived from
    # — not as of `propose_backfill`, which runs later. `apply_backfill` refuses to write
    # these values into anything else
    source_identity: tuple[int, int] | None = None


def _split_terms(text: str) -> list[str]:
    """Splits on `,` `;` `·` `|`, except inside a matched pair of parentheses — a parenthetical
    is an aside about the term before it, not a term boundary."""
    protected = [False] * len(text)
    opened: list[int] = []
    for i, char in enumerate(text):
        if char == "(":
            opened.append(i)
        elif char == ")" and opened:
            start = opened.pop()
            protected[start:i + 1] = [True] * (i + 1 - start)

    parts: list[str] = []
    current: list[str] = []
    for i, char in enumerate(text):
        if char in _TERM_SEPARATORS and not protected[i]:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _strip_bullet(line: str) -> str:
    return _BULLET_RE.sub("", line, count=1)


def _extract_label_and_rest(line: str) -> tuple[str | None, str]:
    m = _BOLD_LABEL_RE.match(line)
    if m:
        return m.group(1).rstrip(":").strip(), line[m.end():]
    m = _PLAIN_LABEL_RE.match(line)
    if m:
        return m.group(1).strip(), line[m.end():]
    return None, line


def _clean_candidate(raw_token: str) -> tuple[str, bool] | None:
    """`(text, was_backtick_quoted)`, or None once nothing usable remains."""
    text = _TRAILING_PAREN_RE.sub("", raw_token.strip()).strip()
    m = _BACKTICKED_RE.match(text)
    if m:
        text = m.group(1).strip()
    else:
        text = text.strip("\"").strip("'").strip()
    text = text.rstrip(".,;:").strip()
    if not text:
        return None
    return text, bool(m)


def _looks_like_entity(text: str) -> bool:
    """Shape test only: a digit, one of `/_.-`, or an uppercase letter past the first
    character. Lowercase prose is rejected on purpose — contracts/backfill.md."""
    if not text or len(text) > 60:
        return False
    if not any(ch.isalnum() for ch in text):
        return False  # a bare separator like "--" would pass the punctuation check below
    if len(text.split()) > 4:
        return False
    if any(ch.isdigit() for ch in text):
        return True
    if any(ch in "/_.-" for ch in text):
        return True
    if any(ch.isupper() for ch in text[1:]):
        return True  # CamelCase, ALLCAPS, or Title Case beyond the first word
    return False


def _extract_entities(section_body: str) -> list[str]:
    """Entity terms from a `Search Keywords` body; group labels are scaffolding, never tags."""
    entities: list[str] = []
    seen_entities: set[str] = set()

    for raw_line in section_body.splitlines():
        line = _strip_bullet(raw_line)
        if not line.strip():
            continue
        # the label names the group's shape ("Endpoints"), not the document's
        # subject, so it is discarded rather than promoted to a tag
        _label, rest = _extract_label_and_rest(line)
        for raw_token in _split_terms(rest):
            cleaned = _clean_candidate(raw_token)
            if cleaned is None:
                continue
            text, backticked = cleaned
            if text.casefold() in _ENTITY_DENYLIST:
                continue
            if not (backticked or _looks_like_entity(text)):
                continue
            if text not in seen_entities:
                seen_entities.add(text)
                entities.append(text)

    return entities


def _derive_status(body: str) -> tuple[str, str | None]:
    """`(status, raw_source)` — only a `- Status:` line that says so maps to `superseded`."""
    raw = preamble_label_value(_PREAMBLE_STATUS_RE, body)
    if raw is None:
        return "active", None
    # the past participle only: a stem test ("supersed") also fired on "supersedes" and
    # "superseding", which state the opposite relationship — see contracts/backfill.md
    return ("superseded" if "superseded" in raw.casefold() else "active"), raw


# a repo name is a lowercase-hyphenated slug; anything after it on the line
# (parenthetical, em-dash aside, prose) is commentary, not part of the name
_REPO_NAME_RE = re.compile(r"^([a-z0-9]+(?:-[a-z0-9]+)*)")


def _derive_repo_tags(body: str) -> list[str]:
    line_value = preamble_label_value(_PREAMBLE_REPOS_RE, body)
    if line_value is None:
        return []
    tags: list[str] = []
    seen: set[str] = set()
    # an em-dash opens a trailing aside about the entry before it; its own
    # commas would otherwise split into phantom entries, so cut before splitting
    value = re.split(r"\s[—–]\s", line_value)[0]
    for part in re.split(r"[,;·]", value):
        # `*` and `_` alongside the backticks — CommonMark's code span and both of its
        # emphasis delimiters. A repo name contains none of the three, and emphasis around the
        # entry (`**widget-cache**`, `_widget-cache_`) otherwise left the marker in the
        # candidate, where the slug test rejected it and the entry contributed no tag at all
        candidate = part.strip().strip("`*_").strip().casefold()
        name = _REPO_NAME_RE.match(candidate)
        if not name:
            continue
        # the slug must BE the entry, not merely start it (rejects prose like
        # "shared apis (3rd-party)"); a parenthetical suffix is allowed to follow
        remainder = candidate[name.end():].strip()
        if remainder and not remainder.startswith("("):
            continue
        tag = name.group(1)
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


_MD = MarkdownIt("commonmark")


def _link_destinations(body: str) -> list[str]:
    """Every destination CommonMark recognises as a link, outside block quotes."""
    destinations: list[str] = []
    quote_depth = 0
    for token in _MD.parse(body):
        # a block quote holds another document's text, so its links are that document's
        # edges, not this one's. `sections.py` lands on the same policy for a quoted
        # heading — see test_blockquoted_heading_diverges_on_purpose for the reasoning
        if token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        elif quote_depth == 0:
            destinations.extend(
                child.attrGet("href") or ""
                for child in token.children or []
                if child.type == "link_open"
            )
    return destinations


def _sibling_filename(href: str) -> str | None:
    """The filename `href` names in this document's own directory, else None — a link out of
    `sessions/` has no document id to contribute."""
    split = urlsplit(href)
    # a scheme means this is not a path at all — `https://…`, and also `mailto:a@b.md`, which
    # ends in `.md` and would otherwise contribute the "id" `mailto:a@b`
    if split.scheme or split.netloc:
        return None
    # urlsplit cut the fragment before decoding, never after: an anchor is written literally,
    # so a `#` that survived percent-encoding belongs to the filename. `related` holds stems,
    # so what the parser normalised (` ` -> `%20`) is read back as written
    target = unquote(split.path).strip()
    if not target or "\\" in target:
        # a Windows-style path is not a sibling either, and `\` is not a separator here
        return None
    # normalised first, so `./sibling.md` and `a/../sibling.md` are recognised as siblings;
    # anything still carrying a separator afterwards genuinely points out of the directory
    target = posixpath.normpath(target)
    if "/" in target or not target.endswith(".md"):
        return None
    return target


def _extract_related_ids(body: str, self_id: str) -> list[str]:
    """Ids of siblings linked as `[text](sibling.md)`; URLs, anchors and self-links are dropped."""
    ids: list[str] = []
    seen: set[str] = set()
    for href in _link_destinations(body):
        target = _sibling_filename(href)
        if target is None:
            continue
        stem = Path(target).stem
        # the shape half of `spine.validate_doc_id` is deliberately not applied (see the
        # contract), but its safety half still is: `.md`, `..md` and `...md` yield the stems
        # `.md`, `.` and `..`, which name no document and cannot be filenames either
        if not stem or stem.startswith(".") or "\x00" in stem:
            continue
        if stem == self_id or stem in seen:
            continue
        seen.add(stem)
        ids.append(stem)
    return ids


# the human's way to close this out: `[]` written by hand is the answer "checked, there
# are none", which backfill cannot make on their behalf but must not hide from them
_ENTITIES_CLOSE_HINT = (
    "If this document really names no identifiers, write `entities: []` yourself — "
    "that is the answer engmem will not guess."
)


# "engmem found no", not "there is no": what the code knows is that no section resolved to the
# `keywords` role, which is still a weaker claim than the author reading their own document
# would make — a `### Search Keywords` under a `##` section smaller than MAX_SECTION_BYTES is
# never split out, so it is not seen
_ENTITIES_MISSING_NOTE = (
    "engmem found no 'Search Keywords' section in this document — entities left unset "
    "rather than guessed from free-form body prose. Add a section by hand, or run "
    "/engmem.save to have an agent draft one, then re-run `engmem backfill`. "
    + _ENTITIES_CLOSE_HINT
)


def _entities_empty_note(locator: str) -> str:
    # names backticks explicitly: the population reaching this note is dominated by the
    # documented false negative (lowercase, punctuation-free prose terms), whose author
    # believes they already named the identifiers — the shape rule is what disagrees
    return (
        f"the 'Search Keywords' section ({locator}) yielded no term that reads as an "
        "entity — entities left unset rather than guessed from its prose. Name the "
        "identifiers there (a term in backticks is always accepted), then re-run "
        "`engmem backfill`. " + _ENTITIES_CLOSE_HINT
    )


def propose_backfill(doc: Doc) -> BackfillProposal:
    """What `engmem backfill` would write for `doc`, without writing it."""
    if doc.spine_complete:
        return BackfillProposal(doc.id, doc.path, True, [], [])

    text, _ = read_document(doc.path)
    front_matter_text, _ = split_front_matter(text)
    raw = _front_matter_mapping(front_matter_text)

    body = doc.body

    candidates: list[FieldProposal] = [
        FieldProposal("id", doc.id, "filename stem"),
        FieldProposal(
            "title", doc.title,
            "first '# H1' heading ('Knowledge Base — ' prefix stripped), else filename stem",
        ),
        FieldProposal(
            # a real datetime.date, not its isoformat() string, so PyYAML
            # renders it unquoted like every hand-written document's own `date:`
            "date", doc.date,
            "stated '- Date:'/'- Updated:' preamble line, else file mtime",
        ),
        FieldProposal("task_date", doc.date, "= date"),
    ]

    status, status_source = _derive_status(body)
    candidates.append(FieldProposal(
        "status", status,
        f"preamble line '- Status: {status_source}'" if status_source
        else "no '- Status:' preamble line with a value found — defaulted to active",
    ))
    # a superseded document with no successor is a dead end: `output.py` prints a bare
    # "(superseded)" and `scoring.py` has nothing to redirect the reader to. The line that
    # says so almost always names the successor — "Superseded by [v2](widget-cache-v2.md)".
    # The word test above decides direction; the author's own `status:` gates it — both are
    # required, and neither substitutes for the other. See contracts/backfill.md
    stated_status = doc.status if stated(raw, "status") else None
    if status == "superseded" and status_source and stated_status in (None, "superseded"):
        successors = _extract_related_ids(status_source, doc.id)
        if successors:
            candidates.append(FieldProposal(
                "superseded_by", successors[0],
                f"sibling link on the '- Status:' preamble line ({status_source})",
            ))
    candidates.append(FieldProposal(
        "backfilled", True,
        "always true for a document engmem did not itself author",
    ))

    # every piece, not the first: a `Search Keywords` section over MAX_SECTION_BYTES is split
    # across its `###` subsections, and its terms live in all of them
    keywords_sections = sections_for_role(split_sections(body), "keywords")
    keywords_locators = ", ".join(s.locator for s in keywords_sections)

    notes: list[str] = []
    if not keywords_sections:
        entities = []
        note = _ENTITIES_MISSING_NOTE
    else:
        entities = _extract_entities("\n".join(s.body for s in keywords_sections))
        note = _entities_empty_note(keywords_locators) if not entities else None
    # a note claims this run left `entities` unset; when the author already stated it, the
    # document is degraded on some other field and the claim would be false
    if note is not None and not stated(raw, "entities"):
        notes.append(note)

    repo_tags = _derive_repo_tags(body)
    if repo_tags:
        tags_source = f"'- Repos:' preamble line ({', '.join(repo_tags)})"
    elif preamble_label_value(_PREAMBLE_REPOS_RE, body):
        # a line the author wrote and the parser rejected is not the same evidence as no
        # line at all, and "none found" read as the latter
        tags_source = "'- Repos:' preamble line found, but no entry parsed as a repo name"
    else:
        tags_source = "no '- Repos:' preamble line with a value found"

    # a copy, not `repo_tags` itself: `tags` and `repos` must stay separate objects
    candidates.append(FieldProposal("tags", list(repo_tags), tags_source))
    # repos also ride in `tags`, which is scored; this field is the declared answer to
    # "which code is this about", and stays blank on every backfilled document otherwise
    if repo_tags:
        candidates.append(
            FieldProposal("repos", repo_tags, "'- Repos:' preamble line")
        )
    # only when something was actually derived. `entities: []` is indistinguishable from a
    # human's own "checked, found none", and `stated` counts it as answered — the document
    # would go spine-complete and never be offered to `backfill` again, so the note above
    # ("add a section, then re-run") would be advice that cannot work. Leaving the field
    # unset keeps the document degraded, which is the truth, and keeps the re-run honest.
    if entities:
        candidates.append(FieldProposal(
            "entities", entities,
            f"'Search Keywords' section ({keywords_locators}), "
            f"{len(entities)} term(s) extracted",
        ))

    related = _extract_related_ids(body, doc.id)
    candidates.append(FieldProposal(
        "related", related,
        "markdown link(s) to sibling .md file(s) in the body" if related
        else "no markdown links to sibling .md files found",
    ))

    fields = [f for f in candidates if not stated(raw, f.name)]
    return BackfillProposal(doc.id, doc.path, False, fields, notes, doc.source_identity)


_YAML_NULL_TAG = "tag:yaml.org,2002:null"


def _drop_declared_keys(front_matter_text: str, names: set[str]) -> str:
    """Removes the author's own empty `name:` lines for the fields about to be appended, so the
    document does not end up declaring a key twice. Rules and residuals: contracts/backfill.md.
    """
    # asked of the parser, not a regex: column 0 is not a declaration test (an unindented
    # continuation inside a flow collection sits there too), and emptiness has more spellings
    # than are safe to enumerate. Composing is safe — `_front_matter_mapping` already parsed it
    node = yaml.compose(front_matter_text, Loader=yaml.SafeLoader)
    # a whole line is removed per declaration, which needs one declaration per line. A flow
    # mapping (`{title: T, tags: null}`) puts every pair on one line, so removing it would take
    # the author's other keys with it; leaving it alone means the append fails the staged parse
    # instead, which refuses the write rather than losing anything
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        return front_matter_text

    dropped = {
        key.start_mark.line
        for key, value in node.value
        if key.value in names
        and value.tag == _YAML_NULL_TAG
        # the whole line goes, so nothing on it may belong to anything else
        and value.end_mark.line == key.start_mark.line
    }
    return "".join(
        line for i, line in enumerate(front_matter_text.splitlines(keepends=True))
        if i not in dropped
    )


class BackfillWriteError(Exception):
    """Raised when a staged write cannot be safely committed; `doc.path` is never touched."""


def apply_backfill(doc: Doc, proposal: BackfillProposal) -> str:
    """Writes `proposal`'s fields into the front matter, preserving the body byte-for-byte."""
    if proposal.already_complete or not proposal.fields:
        return "nothing to backfill"

    if proposal.doc_id != doc.id or proposal.path != doc.path:
        # the caller paired the wrong two objects; writing one document's derived values into
        # another would put a foreign `id` in its front matter and duplicate an id in the store
        raise BackfillWriteError(
            f"{doc.path.name}: proposal is for {proposal.doc_id} ({proposal.path.name}) "
            "— refusing to write"
        )

    original_text, bom = read_document(doc.path)
    # everything below `proposal.fields` was derived from the document as it was when
    # `propose_backfill` read it, and the CLI puts a human's confirmation prompt in between.
    # The body is re-read above, so an edit landing in that window is preserved and the
    # byte-for-byte check still passes — the stale part is silent, and it is the values.
    # Stamped *after* that read, so one comparison covers both reads: taken before it, an edit
    # landing in between was certified by a check that had already run. An identity that cannot
    # be taken at all is not a match either — `None != None` is False, so "no evidence" read as
    # "unchanged"
    identity = identity_for(doc.path)
    if identity is None or proposal.source_identity != identity:
        raise BackfillWriteError(
            f"{doc.path.name}: changed on disk since the proposal was made "
            "— refusing to write. Re-run `engmem backfill` to see a current one"
        )

    front_matter_text, expected_body = split_front_matter(original_text)
    raw = _front_matter_mapping(front_matter_text)

    missing = {f.name: f.value for f in proposal.fields if not stated(raw, f.name)}
    if not missing:
        return "nothing to backfill (fields already present)"

    # the lines being added, and the two delimiters, take the document's own line ending. The
    # body is preserved byte-for-byte either way, but emitting LF into a CRLF document left it
    # with both — a whole-file diff under `core.autocrlf` or a `.gitattributes` eol, on exactly
    # the legacy documents this command exists for. The front matter's own lines answer first;
    # a document that has none is having its block created above the body, so the body answers
    newline = newline_of(front_matter_text) or newline_of(expected_body) or "\n"
    addition = _dump_front_matter(missing).replace("\n", newline)
    # no trailing-newline fixup: `split_front_matter` joins `splitlines(keepends=True)` lines
    # that are all followed by the closing `---`, so every line keeps its terminator. Every
    # terminator `splitlines` recognises is either a YAML line break too (CR, NEL, LS) or a
    # character PyYAML refuses to read at all (VT, FF, FS), so such a document never loads
    prefix = _drop_declared_keys(front_matter_text, set(missing))
    new_content = f"---{newline}" + prefix + addition + f"---{newline}" + expected_body

    # a symlinked document is refused, not written — the same rule `mcp_server` states for
    # its write tools. `os.replace` over the link silently detached the document from whatever
    # it pointed at; writing *through* it is worse in both directions. Out of the directory, it
    # makes this the only path here that writes outside the store, and a store is relocatable
    # with --store, so a link committed to a shared one aims the write anywhere. Inside it, the
    # two names share an inode, so the alias's `id` lands in the real document too and
    # `load_store` then reports a duplicate id across the pair
    if doc.path.is_symlink():
        raise BackfillWriteError(
            f"{doc.path.name}: is a symlink to {os.readlink(doc.path)} — refusing to write. "
            "Replace it with the document itself, or back the target up and copy it in"
        )

    tmp_path = stage(doc.path, bom + new_content.encode("utf-8"))
    # BaseException, not Exception: a Ctrl-C landing between the stage and the commit must
    # still take the temp file with it
    try:
        staged_text, _ = read_document(tmp_path)
        staged_front_matter, staged_body = split_front_matter(staged_text)
        if staged_body != expected_body:
            raise BackfillWriteError(
                f"{doc.path.name}: staged body did not match the original "
                "byte-for-byte — refusing to write"
            )
        try:
            parse_document(tmp_path)
        except (yaml.YAMLError, ValueError, OSError) as exc:
            raise BackfillWriteError(
                f"{doc.path.name}: staged content does not parse ({exc}) — "
                "refusing to write"
            ) from exc
        # the returned message is a claim about the file; a staged document that parses but
        # does not state what was appended would make it a false one — and `_drop_declared_keys`
        # has already removed the author's own declarations by then
        staged_raw = _front_matter_mapping(staged_front_matter)
        unwritten = [name for name in missing if not stated(staged_raw, name)]
        if unwritten:
            raise BackfillWriteError(
                f"{doc.path.name}: staged front matter does not state "
                f"{', '.join(unwritten)} — refusing to write"
            )
        commit(tmp_path, doc.path)
    except BaseException:
        discard(tmp_path)
        raise

    field_names = ", ".join(missing)
    return f"backfilled {len(missing)} field(s): {field_names}"
