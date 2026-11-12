"""Classes 10-13 as corpus generators, and the facts every fact-bearing class rests on (contracts/e2e.md)."""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from dataclasses import asdict, dataclass
from pathlib import Path

from agentsim import corpus
from agentsim.corpus import Doc, _full_sections, _lesson
from agentsim.gitfixture import Commit, commit_ids

FACTS_FORMAT = "engmem-e2e-facts/1"
CLASS_NAMES = {
    1: "answer exists, keyword-findable",
    2: "answer exists, wording gap",
    3: "answer in a non-primer section",
    4: "stale trap: the old decision was superseded",
    6: "cross-repository answer: the task is worked in another repository than the fact's",
    7: "larger store (the scale store)",
    10: "hard stale: a supersede chain of depth 1, 3 or 6; the stale value sits in the Decision Log",
    11: "freshness: the note's covered file changed in a later commit (variant a) or did not (variant b)",
    12: "near-duplicates: many notes on one topic over time, some not superseded; the newest is canonical",
    13: "exact identifier: a config key or error string verbatim in one body",
}
FACT_CLASSES = tuple(sorted(CLASS_NAMES))


@dataclass(frozen=True)
class Fact:
    """One answer-key fact: where it lives, its value, and the values that would be stale."""

    id: str
    scenario_class: int
    section: str
    value: str
    stale_values: tuple[str, ...]
    entities: tuple[str, ...]
    repo: str
    doc: str
    topic: str
    store: str = "base"
    variant: str = ""
    identifiers: tuple[str, ...] = ()
    covers_files: tuple[str, ...] = ()
    source_file: str = ""
    stale_docs: tuple[str, ...] = ()


HARD_STALE = (
    ("LedgerSealer", "seal batch size", "entries per seal", (900,), 400, None, "ledger-core"),
    ("PayoutRouter", "fallback window", "minutes", (25,), 4, ("DisbursalSwitch", "failover hold"), "intake-gateway"),
    ("QuoteCache", "refresh period", "seconds", (10, 20, 45), 95, None, "tile-render"),
    ("TariffLoader", "reload interval", "minutes", (30, 50, 70), 140, ("PriceSheetSync", "sync cadence"), "report-hub"),
    ("FraudScreen", "score threshold", "points", (55, 60, 65, 70, 72, 75), 81, None, "ledger-core"),
    ("StatementMailer", "send window", "hours", (2, 3, 5, 7, 9, 11), 13, ("DocketCourier", "courier slot"), "report-hub"),
)

METER_REPO = "meter-service"
METER_FILES = (
    ("src/meter/FlushPolicy.kt", "MeterFlusher", "flush lag limit", "milliseconds", 750, 300),
    ("src/meter/ReadingWindow.kt", "ReadingCollector", "reading window", "minutes", 15, 5),
    ("src/meter/DedupWindow.kt", "DedupFilter", "dedup horizon", "hours", 6, None),
    ("src/meter/RetryBudget.kt", "UploadRetrier", "retry budget", "percent", 12, None),
    ("src/meter/SealInterval.kt", "BatchSealer", "seal interval", "seconds", 40, 25),
    ("src/meter/SkewGuard.kt", "SkewGuard", "skew tolerance", "milliseconds", 200, None),
)
FIRST_METER_FILES = 4

NEAR_FIRST_DAY = date(2024, 1, 8)
NEAR_SPAN_DAYS = 880
NEAR_DUPLICATES = (
    ("SettlementClock", "cutoff time", "UTC", 24, "ledger-core", None, None),
    ("ExportThrottle", "burst ceiling", "requests", 40, "report-hub", 120, 15),
    ("LedgerArchiver", "retention window", "days", 20, "ledger-core", 30, 3),
    ("TileWarmer", "warm set size", "tiles", 30, "tile-render", 210, 20),
    ("GatewayBreaker", "trip threshold", "failures", 32, "intake-gateway", 11, 2),
)
FIRST_NEAR_GROUPS = 2

IDENTIFIERS = (
    ("IngestGate", "backpressure tuning", "intake-gateway", "production",
     "ingest.backpressure.high_watermark", "1800 messages",
     "The key `ingest.backpressure.high_watermark` is set to 1800 messages; above it the gateway sheds load."),
    ("SealVerifier", "journal replay", "ledger-core", "lessons",
     "ERR_SEAL_DIGEST_MISMATCH", "rebuild the seal digest index before replaying the journal",
     None),
    ("PageComposer", "parallel rendering", "report-hub", "production",
     "report.render.max_parallel_pages", "6 pages at a time",
     "The key `report.render.max_parallel_pages` is set to 6 pages at a time; more starved the font cache."),
    ("CursorExporter", "cursor expiry", "report-hub", "lessons",
     "E4417 cursor token expired", "restart the export from the last committed cursor",
     None),
    ("QuotaLedger", "overdraft handling", "ledger-core", "production",
     "quota.overdraft.grace_seconds", "90 seconds of grace",
     "The key `quota.overdraft.grace_seconds` allows 90 seconds of grace before a tenant is throttled."),
    ("BundleSigner", "signature failures", "tile-render", "lessons",
     "SIG_CHAIN_TOO_SHORT", "re-issue the intermediate certificate before signing again",
     None),
)
FIRST_IDENTIFIERS = 4

PLAIN_FACTS = {
    1: (
        ("NightlyCompactor", "compaction window", "ledger-core",
         "the compaction window opens at 02:30 UTC and closes by 04:00 UTC"),
        ("WebhookSigner", "signature algorithm", "intake-gateway",
         "webhook payloads are signed with HMAC-SHA256 over the raw body"),
        ("TileSharder", "shard key", "tile-render", "tiles are sharded by zoom level and then by quadkey prefix"),
        ("ReportScheduler", "run concurrency", "report-hub", "at most 4 report jobs run at once per tenant"),
    ),
    2: (
        ("SessionReaper", "idle eviction", "intake-gateway",
         "sessions idle for 45 minutes are evicted by the reaper sweep"),
        ("GlyphAtlas", "font atlas reuse", "tile-render",
         "the glyph atlas is built once per style version and shared across workers"),
        ("PostingJournal", "journal fsync", "ledger-core", "the posting journal calls fsync after every batch of 32 entries"),
        ("ExportPacker", "archive compression", "report-hub", "exports are packed as zstd level 6 archives"),
    ),
    3: (
        ("FeeCalculator", "line rounding", "ledger-core",
         "rounding each line item separately drifted the invoice total by a cent"),
        ("ConsentRecorder", "consent writes", "intake-gateway",
         "a retry wrote the consent record twice with different timestamps"),
        ("StyleCompiler", "style hot reload", "tile-render",
         "a hot reload swapped styles mid-render and mixed two palettes on one tile"),
        ("DigestMailer", "digest batching", "report-hub",
         "the digest job resent every mail after a crash because the cursor was saved last"),
    ),
    6: (
        ("CurrencyTable", "currency precision", "ledger-core",
         "amounts are stored as integer minor units with the currency's own exponent"),
        ("TraceHeader", "trace propagation", "intake-gateway", "the trace id travels in the traceparent header and nowhere else"),
        ("TenantResolver", "tenant lookup", "report-hub", "the tenant id comes from the token's audience claim, never from the path"),
        ("ErrorEnvelope", "error format", "tile-render", "errors return a problem+json body with a stable type URI"),
    ),
    7: (
        ("LedgerSnapshotter", "snapshot cadence", "ledger-core", "ledger snapshots are taken every 6 hours and kept for 14 days"),
        ("IntakeDeduper", "dedup key", "intake-gateway", "the dedup key is the client id plus the request token"),
        ("TilePurger", "purge batch", "tile-render", "the purger deletes stale tiles in batches of 5000 keys"),
        ("ReportArchiver", "cold storage move", "report-hub", "reports move to cold storage 90 days after their last read"),
    ),
}
STALE_PAIRS = (
    ("WalletLocker", "lock timeout", "ledger-core", "lock timeout of 30 seconds", "lock timeout of 8 seconds"),
    ("FormValidator", "upload limit", "intake-gateway", "upload limit of 25 megabytes", "upload limit of 10 megabytes"),
    ("RasterPool", "worker count", "tile-render", "raster pool of 16 workers", "raster pool of 6 workers"),
    ("ChartRenderer", "render timeout", "report-hub", "render timeout of 120 seconds", "render timeout of 50 seconds"),
)


def _slug(text: str) -> str:
    return "-".join(text.lower().replace(".", " ").replace("_", " ").split())


def hard_stale_docs(rng: random.Random) -> tuple[list[Doc], list[Fact]]:
    """Class 10: six supersede chains; every stale value sits in a link's Decision Log only."""
    docs: list[Doc] = []
    facts: list[Fact] = []
    for index, (component, topic, unit, old_values, final, renamed, repo) in enumerate(HARD_STALE):
        new_component, new_topic = renamed or (component, topic)
        chain_ids = [f"2025{index + 1:02d}{link + 10:02d}-{component.lower()}-{_slug(topic)}-r{link + 1}"
                     for link in range(len(old_values))]
        successor = f"2026{index + 4:02d}20-{new_component.lower()}-{_slug(new_topic)}"
        for link, (doc_id, value) in enumerate(zip(chain_ids, old_values)):
            nxt = chain_ids[link + 1] if link + 1 < len(chain_ids) else successor
            docs.append(Doc(
                id=doc_id,
                title=f"{component} {topic}",
                date=f"2025-{index + 1:02d}-{link + 10:02d}",
                repos=(repo,),
                tags=(_slug(topic).split("-")[0],),
                entities=(component,),
                status="superseded",
                superseded_by=nxt,
                sections=_full_sections(
                    rng, component, repo, topic,
                    decisions=(
                        f"- Decision: the {component} {topic} is {value} {unit}.\n"
                        f"- Rejected: leaving the {topic} unset, because the default drifted between releases."
                    ),
                    primer=f"{component} {topic}: the setting and its reason are in the Decision Log.",
                ),
            ))
        words = f"{new_component} {new_topic}"
        docs.append(Doc(
            id=successor,
            title=words,
            date=f"2026-{index + 4:02d}-20",
            repos=(repo,),
            tags=(_slug(new_topic).split("-")[0],),
            entities=(new_component,),
            sections=_full_sections(
                rng, new_component, repo, new_topic,
                decisions=(
                    f"- Decision: the {new_component} {new_topic} is {final} {unit}.\n"
                    f"- Rejected: the earlier settings, because load moved to the morning peak."
                ),
                primer=f"The {new_component} {new_topic} is {final} {unit}; it replaces every earlier setting.",
            ),
        ))
        facts.append(Fact(
            id=f"f10-{component.lower()}-{_slug(topic)}",
            scenario_class=10,
            section="decisions",
            value=f"{final} {unit}",
            stale_values=tuple(f"{value} {unit}" for value in old_values),
            entities=tuple(dict.fromkeys((component, new_component))),
            repo=repo,
            doc=successor,
            topic=f"the {component} {topic}",
            variant=f"chain depth {len(old_values)}, successor {'renamed' if renamed else 'with the same words'}",
            stale_docs=tuple(chain_ids),
        ))
    return docs, facts


def _meter_source(path: str, component: str, topic: str, unit: str, value: int) -> str:
    name = path.rsplit("/", 1)[-1].split(".", 1)[0]
    constant = _slug(topic).replace("-", "_").upper()
    return (
        "package meter\n\n"
        f"object {name} {{\n"
        f"    const val {constant} = {value} // {topic} of {value} {unit}\n"
        f"    val owner = \"{component}\"\n"
        "}\n"
    )


def meter_history() -> tuple[Commit, ...]:
    """Class 11's checkout: commit A as the notes saw it, commit B changing the variant-a files."""
    readme = "# meter-service\n"
    first = [("README.md", readme)]
    second = [("README.md", readme + "\nWindows tuned after the spring load test.\n")]
    for path, component, topic, unit, old, new in METER_FILES:
        first.append((path, _meter_source(path, component, topic, unit, old)))
        second.append((path, _meter_source(path, component, topic, unit, new if new is not None else old)))
    return (
        Commit(tuple(first), "initial meter service", 1767225600),
        Commit(tuple(second), "tune flush and reading windows", 1772323200),
    )


def freshness_docs(rng: random.Random, first: int = 0, last: int = FIRST_METER_FILES) -> tuple[list[Doc], list[Fact]]:
    """Class 11: notes anchored at commit A whose covered file changed at HEAD (a) or did not (b)."""
    anchor = commit_ids(meter_history())[0]
    docs: list[Doc] = []
    facts: list[Fact] = []
    for index, (path, component, topic, unit, old, new) in enumerate(METER_FILES[first:last], start=first):
        doc_id = f"202601{index + 2:02d}-{component.lower()}-{_slug(topic)}"
        docs.append(Doc(
            id=doc_id,
            title=f"{component} {topic}",
            date=f"2026-01-{index + 2:02d}",
            repos=(METER_REPO,),
            tags=("meter",),
            entities=(component,),
            covers_files=(path,),
            verified_at=f"{{{METER_REPO}: {anchor}}}",
            sections=_full_sections(
                rng, component, METER_REPO, topic,
                decisions=(
                    f"- Decision: {component} keeps a {topic} of {old} {unit}.\n"
                    f"- Rejected: no {topic}, because readings piled up during network loss."
                ),
                primer=f"{component} keeps a {topic} of {old} {unit}; the constant lives in {path}.",
            ),
        ))
        changed = new is not None
        facts.append(Fact(
            id=f"f11-{component.lower()}-{_slug(topic)}",
            scenario_class=11,
            section="code" if changed else "decisions",
            value=f"{topic} of {new if changed else old} {unit}",
            stale_values=(f"{topic} of {old} {unit}",) if changed else (),
            entities=(component,),
            repo=METER_REPO,
            doc=doc_id,
            topic=f"the {component} {topic}",
            variant="a: the covered file changed after the note" if changed else "b: the covered file is unchanged",
            covers_files=(path,),
            source_file=path if changed else "",
        ))
    return docs, facts


def near_duplicate_docs(rng: random.Random, first: int = 0, last: int = FIRST_NEAR_GROUPS) -> tuple[list[Doc], list[Fact]]:
    """Class 12: notes on one topic over time; every third is left active, the newest is canonical."""
    docs: list[Doc] = []
    facts: list[Fact] = []
    for group, (component, topic, unit, count, repo, base, step) in enumerate(NEAR_DUPLICATES[first:last], start=first):
        values = [_near_value(base, step, i, unit) for i in range(count)]
        days = [NEAR_FIRST_DAY + timedelta(days=(i * 7 + 3 * group) % count * NEAR_SPAN_DAYS // count) for i in range(count)]
        dates = [day.isoformat() for day in days]
        ids = [f"{day:%Y%m%d}-{component.lower()}-{_slug(topic)}-{i + 1}" for i, day in enumerate(days)]
        order = sorted(range(count), key=lambda i: (dates[i], ids[i]))
        canonical = order[-1]
        for rank, i in enumerate(order):
            newer = order[rank + 1] if rank + 1 < len(order) else None
            superseded = newer is not None and rank % 3 != 0
            last = i == canonical
            primer = (
                f"The {component} {topic} is {values[i]}. This is the current setting."
                if last else f"The {component} {topic} is {values[i]}."
            )
            docs.append(Doc(
                id=ids[i],
                title=f"{component} {topic}",
                date=dates[i],
                repos=(repo,),
                tags=(_slug(topic).split("-")[0],),
                entities=(component,),
                status="superseded" if superseded else "active",
                superseded_by=ids[newer] if superseded else "",
                full=rank % 2 == 0,
                sections=_near_sections(rng, component, repo, topic, values[i], primer, rank % 2 == 0),
            ))
        facts.append(Fact(
            id=f"f12-{component.lower()}-{_slug(topic)}",
            scenario_class=12,
            section="decisions",
            value=values[canonical],
            stale_values=tuple(values[i] for i in order[:-1]),
            entities=(component,),
            repo=repo,
            doc=ids[canonical],
            topic=f"the {component} {topic}",
            variant=f"{count} notes, the newest canonical",
            stale_docs=tuple(ids[i] for i in order[:-1]),
        ))
    return docs, facts


def _near_value(base: int | None, step: int | None, i: int, unit: str) -> str:
    if base is None or step is None:
        minutes = 13 * 60 + i * 7
        return f"{minutes // 60:02d}:{minutes % 60:02d} {unit}"
    return f"{base + i * step} {unit}"


def _near_sections(rng, component, repo, topic, value, primer, full) -> dict[str, str]:
    decision = (
        f"- Decision: the {component} {topic} is {value}.\n"
        f"- Rejected: no {topic}, because the downstream service rejected bursts."
    )
    if full:
        return _full_sections(rng, component, repo, topic, decisions=decision, primer=primer)
    return {"decisions": decision, "primer": primer, "reuse": "Prior docs used: none."}


def identifier_docs(rng: random.Random, first: int = 0, last: int = FIRST_IDENTIFIERS) -> tuple[list[Doc], list[Fact]]:
    """Class 13: a config key or an error string verbatim in one body, outside the primer."""
    docs: list[Doc] = []
    facts: list[Fact] = []
    for index, (component, topic, repo, section, identifier, value, sentence) in enumerate(IDENTIFIERS[first:last], start=first):
        doc_id = f"202605{index + 3:02d}-{component.lower()}-{_slug(topic)}"
        given = {
            "decisions": (
                f"- Decision: {component} {topic} follows the measured load profile.\n"
                "- Rejected: copying the settings of the old service, because its traffic shape differed."
            ),
            "primer": f"{component} {topic}: see Production Considerations and Lessons Learned for the details.",
        }
        if section == "production":
            given["production"] = sentence
        else:
            given["lessons"] = _lesson(
                f"replaying the journal after `{identifier}` appeared.",
                f"the replay stopped again with `{identifier}`.",
                f"{value}.",
                "none recorded",
            )
        docs.append(Doc(
            id=doc_id,
            title=f"{component} {topic}",
            date=f"2026-05-{index + 3:02d}",
            repos=(repo,),
            tags=(_slug(topic).split("-")[0],),
            entities=(component,),
            sections=_full_sections(rng, component, repo, topic, **given),
        ))
        facts.append(Fact(
            id=f"f13-{_slug(identifier)}",
            scenario_class=13,
            section=section,
            value=value,
            stale_values=(),
            entities=(component,),
            repo=repo,
            doc=doc_id,
            topic=f"what `{identifier}` means for {component}",
            identifiers=(identifier,),
        ))
    return docs, facts


CORE_FACT_TASKS = {1: "keyword", 2: "wording-gap", 3: "role", 4: "stale", 6: "cross-repo", 7: "scale"}


def core_facts(core: list[Doc]) -> list[Fact]:
    """The facts the hand-written core already holds, one per class 1-4, 6 and 7."""
    by_id = {doc.id: doc for doc in core}
    by_task = {task.id: task for task in corpus.tasks()}
    facts = []
    for scenario_class, task_id in CORE_FACT_TASKS.items():
        task = by_task[task_id]
        doc = by_id[task.answer_docs[0]]
        facts.append(Fact(
            id=f"f{scenario_class:02d}-{task_id}",
            scenario_class=scenario_class,
            section=task.answer_role,
            value=task.quote,
            stale_values=(task.stale_quote,) if task.stale_quote else (),
            entities=doc.entities,
            repo=doc.repos[0],
            doc=doc.id,
            topic=task.artifact,
            store=task.store,
            covers_files=doc.covers_files,
            stale_docs=(task.stale_doc,) if task.stale_doc else (),
        ))
    return facts


def plain_fact_docs(rng: random.Random) -> tuple[list[Doc], list[Fact]]:
    """Four more facts each for classes 1, 2, 3, 6 and 7, one document per fact: the value in the
    Decision Log and primer, or for class 3 only in a Lessons Learned entry."""
    docs: list[Doc] = []
    facts: list[Fact] = []
    for scenario_class, specs in PLAIN_FACTS.items():
        for index, (component, topic, repo, value) in enumerate(specs):
            doc_id = f"2026{index + 2:02d}{scenario_class + 10:02d}-{component.lower()}-{_slug(topic)}"
            if scenario_class == 3:
                given = {
                    "decisions": (
                        f"- Decision: {component} keeps its {topic} behind one reviewed code path.\n"
                        "- Rejected: a second code path, because two paths drifted apart."
                    ),
                    "lessons": _lesson(f"the first {topic} design.", f"{value}.", f"a reviewed {topic} change.",
                                       "none recorded"),
                    "primer": f"{component} {topic}: the pitfalls are in Lessons Learned.",
                }
                section = "lessons"
            else:
                given = {
                    "decisions": (
                        f"- Decision: {value}.\n"
                        f"- Rejected: leaving the {topic} to each caller, because callers disagreed."
                    ),
                    "primer": f"{component} {topic}: {value}.",
                }
                section = "decisions"
            docs.append(Doc(
                id=doc_id,
                title=f"{component} {topic}",
                date=f"{doc_id[:4]}-{doc_id[4:6]}-{doc_id[6:8]}",
                repos=(repo,),
                tags=(_slug(topic).split("-")[0],),
                entities=(component,),
                sections=_full_sections(rng, component, repo, topic, **given),
            ))
            facts.append(Fact(
                id=f"f{scenario_class:02d}-{component.lower()}-{_slug(topic)}",
                scenario_class=scenario_class,
                section=section,
                value=value,
                stale_values=(),
                entities=(component,),
                repo=repo,
                doc=doc_id,
                topic=f"the {component} {topic}",
                store="scale" if scenario_class == 7 else "base",
            ))
    return docs, facts


def stale_pair_docs(rng: random.Random) -> tuple[list[Doc], list[Fact]]:
    """Four more class 4 facts: an older decision superseded by a newer one in the same words."""
    docs: list[Doc] = []
    facts: list[Fact] = []
    for index, (component, topic, repo, old, new) in enumerate(STALE_PAIRS):
        slug = f"{component.lower()}-{_slug(topic)}"
        old_id, new_id = f"2025{index + 3:02d}14-{slug}", f"2026{index + 3:02d}14-{slug}"
        for doc_id, value, successor in ((old_id, old, new_id), (new_id, new, "")):
            primer = (f"{component} {topic}: {value}." if successor
                      else f"{component} {topic}: {value}; this replaces the earlier setting.")
            docs.append(Doc(
                id=doc_id,
                title=f"{component} {topic}",
                date=f"{doc_id[:4]}-{doc_id[4:6]}-{doc_id[6:8]}",
                repos=(repo,),
                tags=(_slug(topic).split("-")[0],),
                entities=(component,),
                status="superseded" if successor else "active",
                superseded_by=successor,
                related=() if successor else (old_id,),
                sections=_full_sections(
                    rng, component, repo, topic,
                    decisions=(
                        f"- Decision: {component} uses a {value}.\n"
                        f"- Rejected: no {topic}, because stuck work held resources overnight."
                    ),
                    primer=primer,
                ),
            ))
        facts.append(Fact(
            id=f"f04-{slug}",
            scenario_class=4,
            section="decisions",
            value=new,
            stale_values=(old,),
            entities=(component,),
            repo=repo,
            doc=new_id,
            topic=f"the {component} {topic}",
            stale_docs=(old_id,),
        ))
    return docs, facts


def class_docs(rng: random.Random) -> tuple[list[Doc], list[Fact]]:
    """Every generated fact document: classes 10-13 as first released, then the facts added after
    them, so earlier facts keep their ids, values and order."""
    builds = (
        hard_stale_docs,
        freshness_docs,
        near_duplicate_docs,
        identifier_docs,
        plain_fact_docs,
        stale_pair_docs,
        lambda r: freshness_docs(r, FIRST_METER_FILES, len(METER_FILES)),
        lambda r: near_duplicate_docs(r, FIRST_NEAR_GROUPS, len(NEAR_DUPLICATES)),
        lambda r: identifier_docs(r, FIRST_IDENTIFIERS, len(IDENTIFIERS)),
    )
    docs: list[Doc] = []
    facts: list[Fact] = []
    for build in builds:
        more_docs, more_facts = build(rng)
        docs += more_docs
        facts += more_facts
    return docs, facts


def subjects(docs: list[Doc]) -> dict[str, str]:
    """What a reader takes as one subject: a document's title, lower-cased."""
    return {doc.id: doc.title.lower() for doc in docs}


def write_facts(path: Path, facts: list[Fact], doc_size: str) -> None:
    """The facts file a blind author writes task instances from; the same bytes for one seed and size."""
    payload = {
        "format": FACTS_FORMAT,
        "seed": corpus.SEED,
        "doc_size": doc_size,
        "classes": {str(k): v for k, v in CLASS_NAMES.items()},
        "facts": [fact_record(fact, doc_size) for fact in facts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def fact_record(fact: Fact, doc_size: str) -> dict:
    record = asdict(fact)
    record["class"] = record.pop("scenario_class")
    record["doc_size"] = doc_size
    for key in ("stale_values", "entities", "identifiers", "covers_files", "stale_docs"):
        record[key] = list(record[key])
    return record


def load_facts(path: Path) -> dict[str, Fact]:
    """The facts of a facts file by id; a file of another format raises `ValueError`."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format") != FACTS_FORMAT:
        raise ValueError(f"{path.name} is not a {FACTS_FORMAT} file")
    facts = {}
    for record in payload["facts"]:
        record = dict(record)
        record.pop("doc_size", None)
        record["scenario_class"] = record.pop("class")
        for key in ("stale_values", "entities", "identifiers", "covers_files", "stale_docs"):
            record[key] = tuple(record.get(key, ()))
        facts[record["id"]] = Fact(**record)
    return facts
