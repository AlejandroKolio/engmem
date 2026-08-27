"""Renders search results, the scoreboard, telemetry summaries and backfill proposals."""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from engmem.scoring import SearchOutcome, SectionHit
from engmem.sections import Section
from engmem.spine import Doc

if TYPE_CHECKING:
    # deferred to avoid a circular import (telemetry.py imports surfaced_ids
    # from here); `from __future__ import annotations` makes this safe
    from engmem.telemetry import TelemetrySummary
    from engmem.backfill import BackfillProposal

# 4 KB, not the old 2 KB: sized to also hold section locators, not just
# spine-field hits — ~1000 tokens against a 200K context window
MAX_OUTPUT_BYTES = 4096
TOP_N = 3
ROLE_TOP_N = TOP_N  # role-filtered search shows the same "top matches" cap
PRIMER_MAX_CHARS = 240
RELATED_MAX = 3
SCOREBOARD_RESERVE = 128
TRIM_MARKER = "[output trimmed to fit 4 KB]"
SECTION_DISPLAY_MAX = 3  # sections named per hit; mirrors TOP_N's reasoning
SECTION_SNIPPET_MAX_CHARS = 140

# any heading containing "cold[- ]start[- ]primer" as a standalone phrase;
# "##" only (not "###") keeps subheadings out of scope
_HEADING_RE = re.compile(
    r"^##\s+.*?\bcold[- ]start[- ]primer\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_SENTENCE_RE = re.compile(r"^.*?[.!?](?=\s|$)")


def _escape_newlines(value) -> str:
    # an embedded newline in a related id or path could forge a second "### "
    # header (D2); escape rather than strip, so tampering stays visible
    text = str(value)
    return text.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def _primer_excerpt(doc: Doc, max_lines: int = 2) -> str:
    match = _HEADING_RE.search(doc.body)
    if not match:
        return ""
    rest = doc.body[match.end():]
    lines = [ln.strip() for ln in rest.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln.startswith("##"):
            lines = lines[:i]
            break
    excerpt = " ".join(lines[:max_lines])
    if len(excerpt) > PRIMER_MAX_CHARS:
        excerpt = excerpt[: PRIMER_MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return excerpt


def _section_snippet(section, max_chars: int = SECTION_SNIPPET_MAX_CHARS) -> str:
    # heading or first sentence, capped: lets the agent judge relevance without opening the section
    text = " ".join(section.body.split())
    if not text:
        return section.heading
    match = _SENTENCE_RE.match(text)
    snippet = match.group(0) if match else text
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return snippet


def _section_line(section_hit: SectionHit) -> str:
    section = section_hit.section
    size_kb = section.size_bytes / 1024
    locator = _escape_newlines(section.locator)
    snippet = _escape_newlines(_section_snippet(section))
    return f"  {locator}   {size_kb:.1f} KB   {snippet}"


def _related_line(doc: Doc, docs_by_id: dict[str, Doc]) -> str:
    if not doc.related:
        return ""
    parts = []
    for rid in doc.related[:RELATED_MAX]:
        related_doc = docs_by_id.get(rid)
        safe_rid = _escape_newlines(rid)
        if related_doc is None:
            parts.append(f"{safe_rid} (not in store)")
        elif related_doc.status == "superseded":
            # the search redirects a superseded *hit* to its successor; a related edge
            # pointing at the same document must not quietly hand over its body instead
            successor = docs_by_id.get(related_doc.superseded_by)
            if successor is None:
                parts.append(f"{safe_rid} (superseded)")
            else:
                parts.append(
                    f"{safe_rid} (superseded by {_escape_newlines(successor.id)}, "
                    f"{_escape_newlines(successor.path)})"
                )
        elif related_doc.status == "draft":
            parts.append(f"{safe_rid} (draft, {_escape_newlines(related_doc.path)})")
        else:
            parts.append(f"{safe_rid} ({_escape_newlines(related_doc.path)})")
    if len(doc.related) > RELATED_MAX:
        parts.append(f"+{len(doc.related) - RELATED_MAX} more")
    return "related: " + ", ".join(parts)


def _trim_to_bytes(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    budget = limit - len(TRIM_MARKER.encode("utf-8")) - 1
    cut = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return cut.rsplit("\n", 1)[0] + "\n" + TRIM_MARKER


def _why_matched(hit) -> str:
    parts = [f"{field}={','.join(tokens)}" for field, tokens in hit.matched_fields.items()]
    return "; ".join(parts)


def _hit_block(
    doc: Doc,
    docs_by_id: dict[str, Doc],
    header: str,
    why: str = "",
    section_hits: list[SectionHit] | None = None,
) -> str:
    lines = [header, f"path: {_escape_newlines(doc.path)}"]
    if why:
        lines.append(f"matched: {why}")
    for section_hit in (section_hits or [])[:SECTION_DISPLAY_MAX]:
        lines.append(_section_line(section_hit))
    primer = _primer_excerpt(doc)
    if primer:
        lines.append(primer)
    related = _related_line(doc, docs_by_id)
    if related:
        lines.append(related)
    return "\n".join(lines)


def _selected(outcome: SearchOutcome) -> tuple[list, set[str], list]:
    """Top-N hits plus the superseded redirects outranking them, so the renderers cannot drift."""
    shown_hits = outcome.hits[:TOP_N]
    shown_ids = {h.doc.id for h in shown_hits}
    # a redirect applies only to a doc that would have WON a top-3 place
    cutoff = shown_hits[-1].score if len(shown_hits) == TOP_N else None
    notes = [n for n in outcome.superseded_notes if cutoff is None or n.score > cutoff]
    return shown_hits, shown_ids, notes


def surfaced_ids(outcome: SearchOutcome) -> list[str]:
    """Every document id actually shown to the agent, in render order."""
    shown_hits, _, notes = _selected(outcome)
    ids: list[str] = [h.doc.id for h in shown_hits]
    for note in notes:
        ids.append(note.doc.id)
        if note.successor is not None:
            ids.append(note.successor.id)
    seen: set[str] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def render_search_results(outcome: SearchOutcome, docs: list[Doc]) -> str:
    docs_by_id = {d.id: d for d in docs}
    blocks: list[str] = []

    shown_hits, shown_ids, notes = _selected(outcome)

    for hit in shown_hits:
        tag = "  [ambiguous]" if hit.ambiguous else ""
        header = f"### {_escape_newlines(hit.doc.id)} (score: {hit.score:.1f}){tag}"
        blocks.append(
            _hit_block(hit.doc, docs_by_id, header, _why_matched(hit), hit.section_hits)
        )

    for note in notes:
        safe_id = _escape_newlines(note.doc.id)
        safe_superseded_by = _escape_newlines(note.doc.superseded_by)
        if not note.doc.superseded_by:
            blocks.append(f"{safe_id}: superseded (no successor recorded)")
            continue
        if note.successor is None:
            blocks.append(
                f"{safe_id}: superseded by {safe_superseded_by} "
                f"(successor not in store)"
            )
            continue
        if note.successor.status == "superseded":
            # known-wrong, and the reader is not told the chain continues. `_related_line`
            # already refuses to render a superseded target for the same reason; it collapses
            # "absent" and "not in store" into one tail, which this splits — and an absent id
            # must never render as "None", which is what `_escape_newlines(None)` gives
            onward = note.successor.superseded_by
            if not onward:
                tail = "itself superseded, no successor recorded"
            elif onward not in docs_by_id:
                tail = f"itself superseded by {_escape_newlines(onward)}, not in store"
            else:
                tail = f"itself superseded by {_escape_newlines(onward)}"
            blocks.append(f"{safe_id}: superseded by {safe_superseded_by} ({tail})")
            continue
        if note.successor.status == "draft":
            # a draft is excluded from search results (data-model.md), and a redirect is
            # still a search result. Its id comes from the superseded document's own front
            # matter and is named; the draft's own content is not
            blocks.append(
                f"{safe_id}: superseded by {safe_superseded_by} (successor is still a draft)"
            )
            continue
        blocks.append(f"{safe_id}: superseded by {safe_superseded_by}")
        if note.successor.id not in shown_ids:
            header = f"### {_escape_newlines(note.successor.id)} (successor)"
            blocks.append(_hit_block(note.successor, docs_by_id, header))

    # names how many were left out, so a tie cut mid-list doesn't look decisive
    withheld = len(outcome.hits) - len(shown_hits)
    withheld_line = (
        f"({withheld} more document(s) matched below the top {TOP_N} — "
        f"narrow the query to see them.)"
        if withheld
        else ""
    )

    body = "\n\n".join(blocks)
    limit = MAX_OUTPUT_BYTES - SCOREBOARD_RESERVE
    if not withheld_line:
        return _trim_to_bytes(body, limit)

    # reserve its bytes up front so it survives the trim, which cuts from the end (D3)
    separator = "\n\n"
    reserve = len((separator + withheld_line).encode("utf-8"))
    trimmed = _trim_to_bytes(body, limit - reserve)
    return trimmed + separator + withheld_line


def render_scoreboard(docs: list[Doc], failed: int = 0) -> str:
    total = len(docs)
    drafts = sum(1 for d in docs if d.status == "draft")
    partial = sum(1 for d in docs if not d.spine_complete)
    notes = []
    if partial:
        notes.append(f"{partial} partial spine")
    if failed:
        notes.append(f"{failed} failed to load")
    count = f"{total} ({', '.join(notes)})" if notes else str(total)
    if not docs:
        return f"docs: {count} | drafts: {drafts} | last doc: never"
    # clamp negative "Nd ago" from a future-dated document (typo, tz skew, D13)
    days_ago = max(0, (datetime.date.today() - max(d.date for d in docs)).days)
    return f"docs: {count} | drafts: {drafts} | last doc: {days_ago}d ago"


def render_no_match() -> str:
    return "prior context: none found"


@dataclass
class RoleHit:
    doc: Doc
    score: float
    section: Section


def select_role_hits(
    outcome: SearchOutcome, role_map: dict[str, dict[str, Section]], role: str
) -> tuple[list[RoleHit], int]:
    """`(kept, n_with_role)`, where `n_with_role` counts past the cap so a withheld count can be
    reported."""
    kept: list[RoleHit] = []
    n_with_role = 0
    for hit in outcome.hits:
        section = role_map.get(hit.doc.id, {}).get(role)
        if section is None:
            continue
        n_with_role += 1
        if len(kept) < ROLE_TOP_N:
            kept.append(RoleHit(doc=hit.doc, score=hit.score, section=section))
    return kept, n_with_role


def _role_hit_block(role_hit: RoleHit, role: str) -> str:
    # tagged on the header itself so a lone block (after a 4 KB trim) still
    # can't be mistaken for an ordinary best-word-match result
    header = (
        f"### {_escape_newlines(role_hit.doc.id)} (score: {role_hit.score:.1f})"
        f"  [role: {role}]"
    )
    section_hit = SectionHit(section=role_hit.section, score=0.0, matched_tokens=[])
    lines = [
        header,
        f"path: {_escape_newlines(role_hit.doc.path)}",
        _section_line(section_hit),
    ]
    return "\n".join(lines)


def render_role_search_results(
    outcome: SearchOutcome, role_map: dict[str, dict[str, Section]], role: str
) -> str:
    """A `--role` result: "nothing matched" and "matched, but none has this role" stay distinct."""
    kept, n_with_role = select_role_hits(outcome, role_map, role)
    lead = f"role: {role}"

    if not kept:
        if not outcome.hits:
            return f"{lead}\nprior context: none found — no document matched the query"
        return (
            f"{lead}\nprior context: none found — {len(outcome.hits)} document(s) "
            f"matched the query, but none has a '{role}' section"
        )

    blocks = [lead] + [_role_hit_block(rh, role) for rh in kept]
    withheld = n_with_role - len(kept)
    withheld_line = (
        f"({withheld} more document(s) have a '{role}' section but rank below the "
        f"top {ROLE_TOP_N} — narrow the query to see them.)"
        if withheld
        else ""
    )

    body = "\n\n".join(blocks)
    limit = MAX_OUTPUT_BYTES - SCOREBOARD_RESERVE
    if not withheld_line:
        return _trim_to_bytes(body, limit)

    # same reservation trick as render_search_results, same reason (D3)
    separator = "\n\n"
    reserve = len((separator + withheld_line).encode("utf-8"))
    trimmed = _trim_to_bytes(body, limit - reserve)
    return trimmed + separator + withheld_line


def _telemetry_channel_line(bucket: "TelemetrySummary", label: str | None = None) -> str:
    name = label or bucket.channel
    hit_rate = (bucket.hits / bucket.total * 100) if bucket.total else 0.0
    kb = bucket.context_bytes / 1024
    return (
        f"  {name:<10} {bucket.total:>5} row(s)   hit-rate {hit_rate:5.1f}%   "
        f"misses {bucket.misses}   ambiguous {bucket.ambiguous}   "
        f"context {kb:.1f} KB (~{bucket.context_tokens_estimate} tokens est.)"
    )


def _navigation_miss_line(count: int) -> str:
    # only when there are any: a standing "0" would read as a finding
    if not count:
        return ""
    return f"\nnavigation misses recorded in documents: {count}"


def render_telemetry_summary(summary: "TelemetrySummary", navigation_misses: int = 0) -> str:
    """`engmem telemetry`'s reading surface: totals, hit rate and context spent."""
    unreadable_note = (
        f" ({summary.unreadable} unreadable line(s) skipped)" if summary.unreadable else ""
    )
    if summary.total == 0:
        nav = _navigation_miss_line(navigation_misses)
        return f"telemetry: 0 row(s) recorded{unreadable_note}" + nav

    lines = [f"telemetry: {summary.total} row(s){unreadable_note}"]
    for bucket in summary.by_channel:
        lines.append(_telemetry_channel_line(bucket))
    lines.append(_telemetry_channel_line(summary.overall, label="overall"))
    return "\n".join(lines) + _navigation_miss_line(navigation_misses)


# a whole `Search Keywords` section can yield hundreds of terms; the preview is what a human
# reads before typing yes, and one 5000-character line is not something anyone reads
PREVIEW_LIST_MAX = 20


def _display_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        shown = ", ".join(str(v) for v in value[:PREVIEW_LIST_MAX])
        if len(value) > PREVIEW_LIST_MAX:
            shown += f", … (+{len(value) - PREVIEW_LIST_MAX} more)"
        return "[" + shown + "]"
    return str(value)


def render_backfill_proposal(proposal: "BackfillProposal") -> str:
    """`engmem backfill`'s preview: one line per proposed field plus its evidence."""
    if proposal.already_complete:
        return f"{proposal.doc_id}: spine already complete — nothing to backfill"

    lines = [f"{proposal.doc_id} ({proposal.path.name}):"]
    if not proposal.fields:
        lines.append("  (nothing left to write — see the note below)")
    for f in proposal.fields:
        lines.append(f"  {f.name}: {_display_value(f.value)}")
        lines.append(f"    <- {f.source}")
    for note in proposal.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)
