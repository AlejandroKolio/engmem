from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import yaml

from engmem.sections import sections_by_locator, split_sections
from engmem.spine import Doc, _parse_one, _split_front_matter, _stated


class _NoAliasDumper(yaml.SafeDumper):
    """`date` and `task_date` propose the same object; without this, PyYAML
    would render that as a YAML anchor/alias valid, but unexpected in a diff."""

    def ignore_aliases(self, data):
        return True


def _dump_front_matter(data: dict) -> str:
    return yaml.dump(
        data, Dumper=_NoAliasDumper, default_flow_style=None, sort_keys=False,
        allow_unicode=True,
    )


# mirrors spine._PREAMBLE_SCAN_LINES
_PREAMBLE_SCAN_LINES = 40

_PREAMBLE_STATUS_RE = re.compile(
    r"^[-*]\s*\*{0,2}Status\*{0,2}\s*:\s*\*{0,2}\s*(.+?)\s*\*{0,2}\s*$",
    re.MULTILINE,
)
_PREAMBLE_REPOS_RE = re.compile(
    r"^[-*]\s*\*{0,2}Repos?\*{0,2}\s*:\s*\*{0,2}\s*(.+?)\s*$",
    re.MULTILINE,
)

# `*` is a list marker only when it is not doubled: `**Classes:**` is a bold label,
# and eating its first asterisk hid the label and sent `*Classes:**` to `entities`
_BULLET_RE = re.compile(r"^\s*(?:[-+•]\s*|\*(?!\*)\s*)")
_BOLD_LABEL_RE = re.compile(r"^\*\*([^*\n]+?)\*\*\s*:?\s*")
_PLAIN_LABEL_RE = re.compile(r"^([A-Z][A-Za-z0-9 /&\-]{0,40}?)\s*:\s+")
_TERM_SPLIT_RE = re.compile(r"[,;·|]")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_BACKTICKED_RE = re.compile(r"^`([^`]+)`$")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

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
    """`(text, was_backtick_quoted)`, or None once a trailing parenthetical,
    quote marks, and trailing punctuation leave nothing usable."""
    text = raw_token.strip()
    if not text:
        return None
    text = _TRAILING_PAREN_RE.sub("", text).strip()
    if not text:
        return None
    m = _BACKTICKED_RE.match(text)
    backticked = bool(m)
    if m:
        text = m.group(1).strip()
    else:
        text = text.strip("\"").strip("'").strip()
    text = text.rstrip(".,;:").strip()
    if not text:
        return None
    return text, backticked


def _looks_like_entity(text: str) -> bool:
    if not text or len(text) > 60:
        return False
    if not any(ch.isalnum() for ch in text):
        return False  # a bare separator like "--" would pass the punctuation check below
    words = text.split()
    if not words or len(words) > 4:
        return False
    if any(ch.isdigit() for ch in text):
        return True
    if any(ch in "/_.-" for ch in text):
        return True
    if any(ch.isupper() for ch in text[1:]):
        return True  # CamelCase, ALLCAPS, or Title Case beyond the first word
    return False


def _extract_entities(section_body: str) -> list[str]:
    """Entity terms from a `Search Keywords` section's body. Group labels
    ("Classes", "Endpoints") are scaffolding and never become tags. Acceptance
    rule and its named false positives/negatives: `contracts/backfill.md`."""
    entities: list[str] = []
    seen_entities: set[str] = set()

    for raw_line in section_body.splitlines():
        line = _strip_bullet(raw_line)
        if not line.strip():
            continue
        # the label names the group's shape ("Endpoints"), not the document's
        # subject, so it is discarded rather than promoted to a tag
        _label, rest = _extract_label_and_rest(line)
        for raw_token in _TERM_SPLIT_RE.split(rest):
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
    """`(status, raw_source_text_or_None)`: a stated `- Status:` line maps to
    `superseded` only when it says so; every other spelling, and no line at
    all, maps to `active`."""
    head = "\n".join(body.splitlines()[:_PREAMBLE_SCAN_LINES])
    match = _PREAMBLE_STATUS_RE.search(head)
    if not match:
        return "active", None
    raw = match.group(1).strip()
    return ("superseded" if "supersed" in raw.casefold() else "active"), raw


# a repo name is a lowercase-hyphenated slug; anything after it on the line
# (parenthetical, em-dash aside, prose) is commentary, not part of the name
_REPO_NAME_RE = re.compile(r"^([a-z0-9]+(?:-[a-z0-9]+)*)")


def _derive_repo_tags(body: str) -> list[str]:
    head = "\n".join(body.splitlines()[:_PREAMBLE_SCAN_LINES])
    match = _PREAMBLE_REPOS_RE.search(head)
    if not match:
        return []
    tags: list[str] = []
    seen: set[str] = set()
    # an em-dash opens a trailing aside about the entry before it; its own
    # commas would otherwise split into phantom entries, so cut before splitting
    value = re.split(r"\s[—–]\s", match.group(1))[0]
    for part in re.split(r"[,;·]", value):
        candidate = part.strip().strip("`").strip().casefold()
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


def _extract_related_ids(body: str, self_id: str) -> list[str]:
    """Ids of sibling documents linked via `[text](sibling.md)`. External URLs
    and same-document anchor links never qualify; a self-link is dropped."""
    ids: list[str] = []
    seen: set[str] = set()
    for match in _MD_LINK_RE.finditer(body):
        target = match.group(1).strip()
        if "://" in target:
            continue
        target = target.split("#", 1)[0].split(" ", 1)[0].strip()
        if not target.endswith(".md"):
            continue
        stem = Path(target).stem
        if not stem or stem == self_id or stem in seen:
            continue
        seen.add(stem)
        ids.append(stem)
    return ids


_ENTITIES_MISSING_NOTE = (
    "no 'Search Keywords' section in this document — entities left empty "
    "rather than guessed from free-form body prose. Add one by hand, or run "
    "/engmem.save to have an agent draft one, then re-run `engmem backfill`."
)


def propose_backfill(doc: Doc) -> BackfillProposal:
    """What `engmem backfill` would write for `doc`, without writing it. A
    document with `doc.spine_complete` proposes nothing. Otherwise each field
    (table: `contracts/backfill.md`) is proposed unless already stated in the
    document's own front matter (`spine._stated`)."""
    if doc.spine_complete:
        return BackfillProposal(doc.id, doc.path, True, [], [])

    text = doc.path.read_text(encoding="utf-8-sig")
    front_matter_text, _ = _split_front_matter(text)
    raw = yaml.safe_load(front_matter_text) if front_matter_text else None
    if not isinstance(raw, dict):
        raw = {}

    body = doc.body

    candidates: list[FieldProposal] = [
        FieldProposal("id", doc.id, "filename stem"),
        FieldProposal(
            "title", doc.title,
            "first '# H1' heading ('Knowledge Base — ' prefix stripped)",
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
        else "no '- Status:' preamble line found — defaulted to active",
    ))
    candidates.append(FieldProposal(
        "backfilled", True,
        "always true for a document engmem did not itself author",
    ))

    sections = split_sections(body)
    by_role = sections_by_locator(sections)
    keywords_section = by_role.get("keywords")

    notes: list[str] = []
    if keywords_section is not None:
        entities = _extract_entities(keywords_section.body)
        entities_source = (
            f"'Search Keywords' section ({keywords_section.locator}), "
            f"{len(entities)} term(s) extracted"
        )
    else:
        entities = []
        entities_source = "none — no 'Search Keywords' section in this document"
        notes.append(_ENTITIES_MISSING_NOTE)

    repo_tags = _derive_repo_tags(body)
    tags = list(repo_tags)
    tag_source_parts = []
    if repo_tags:
        tag_source_parts.append(f"'- Repos:' preamble line ({', '.join(repo_tags)})")
    tags_source = "; ".join(tag_source_parts) if tag_source_parts else "none found"

    candidates.append(FieldProposal("tags", tags, tags_source))
    # repos also ride in `tags`, which is scored; this field is the declared answer to
    # "which code is this about", and stays blank on every backfilled document otherwise
    if repo_tags:
        candidates.append(
            FieldProposal("repos", repo_tags, "'- Repos:' preamble line")
        )
    candidates.append(FieldProposal("entities", entities, entities_source))

    related = _extract_related_ids(body, doc.id)
    candidates.append(FieldProposal(
        "related", related,
        "markdown link(s) to sibling .md file(s) in the body" if related
        else "no markdown links to sibling .md files found",
    ))

    fields = [f for f in candidates if not _stated(raw, f.name)]
    return BackfillProposal(doc.id, doc.path, False, fields, notes)


class BackfillWriteError(Exception):
    """Raised when a staged write cannot be safely committed (body mismatch,
    or the staged content fails to re-parse). The temp file is always cleaned
    up before this raises; `doc.path` itself is never touched."""


def _atomic_replace(tmp_path: Path, target: Path) -> None:
    os.replace(tmp_path, target)  # atomic on POSIX — never a half-written document


def apply_backfill(doc: Doc, proposal: BackfillProposal) -> str:
    """Writes `proposal`'s fields into `doc.path`'s front matter; returns a
    short summary. Staged-write, body-preserving pattern: `contracts/backfill.md`.
    Missing fields are re-checked against the on-disk front matter (not just
    `proposal.fields`) in case the file changed since the proposal was made."""
    if proposal.already_complete or not proposal.fields:
        return "nothing to backfill"

    original_text = doc.path.read_text(encoding="utf-8-sig")
    front_matter_text, expected_body = _split_front_matter(original_text)
    raw = yaml.safe_load(front_matter_text) if front_matter_text else None
    if not isinstance(raw, dict):
        raw = {}

    missing = {f.name: f.value for f in proposal.fields if not _stated(raw, f.name)}
    if not missing:
        return "nothing to backfill (fields already present)"

    addition = _dump_front_matter(missing)
    prefix = (
        front_matter_text
        if not front_matter_text or front_matter_text.endswith("\n")
        else front_matter_text + "\n"
    )
    new_content = "---\n" + prefix + addition + "---\n" + expected_body

    tmp_path = doc.path.with_name(f".{doc.path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(new_content, encoding="utf-8")
    try:
        staged_text = tmp_path.read_text(encoding="utf-8-sig")
        _, staged_body = _split_front_matter(staged_text)
        if staged_body != expected_body:
            raise BackfillWriteError(
                f"{doc.path.name}: staged body did not match the original "
                "byte-for-byte — refusing to write"
            )
        try:
            _parse_one(tmp_path)
        except (yaml.YAMLError, ValueError, OSError) as exc:
            raise BackfillWriteError(
                f"{doc.path.name}: staged content does not parse ({exc}) — "
                "refusing to write"
            ) from exc
        _atomic_replace(tmp_path, doc.path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    field_names = ", ".join(missing)
    return f"backfilled {len(missing)} field(s): {field_names}"
