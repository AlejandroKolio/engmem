"""Deterministic synthetic store and task file: abstract content only, no real names of any kind."""

from __future__ import annotations

import dataclasses
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPOS = ("ledger-core", "intake-gateway", "tile-render", "report-hub")
DEFAULT_FILLER = 60
SCALE_FILLER = 400
LARGE_FILLER = 3000
SEED = 20261107
SENTENCE_FACTOR = 3

FULL_HEADINGS = (
    ("summary", "1. Executive Summary"),
    ("context", "2. Business Context"),
    ("flow", "3. End-to-End Business Flow"),
    ("requirements", "4. Functional Requirements"),
    ("acceptance", "5. Acceptance Criteria Analysis"),
    ("architecture", "6. System Architecture"),
    ("code", "7. Code Implementation"),
    ("decisions", "8. Decision Log"),
    ("production", "9. Production Considerations"),
    ("testing", "10. Testing Knowledge"),
    ("lessons", "11. Lessons Learned"),
    ("graph", "12. Knowledge Graph"),
    ("primer", "13. Future LLM Context (cold-start primer)"),
    ("keywords", "14. Search Keywords"),
    ("cheatsheet", "15. One-Page Cheat Sheet"),
    ("reuse", "16. Reuse Log"),
    ("status", "17. Status at a Glance"),
)
QUICK_HEADINGS = (
    ("decisions", "Decision Log"),
    ("lessons", "Lessons Learned"),
    ("primer", "Future LLM Context (cold-start primer)"),
    ("reuse", "Reuse Log"),
)

FILLER_COMPONENTS = (
    "ArchiveExporter", "BillingPeriodCloser", "BlobJanitor", "ConfigLoader", "CursorPager",
    "DeltaPublisher", "EventFanout", "FeatureGate", "GraphWalker", "HealthProbe",
    "IndexCompactor", "InvoiceRenderer", "JobScheduler", "KeyRotator", "LockLeaser",
    "MetricRollup", "NoticeMailer", "OrderReconciler", "PartitionMover", "QuotaKeeper",
    "RateLimiter", "SchemaMigrator", "ShardRouter", "SnapshotWriter", "TokenMinter",
    "UploadChunker", "VersionPinner", "WebhookRelay", "WorkQueueDrainer", "ZoneBalancer",
)
FILLER_TOPICS = (
    ("retry policy", "retries", "a fixed delay between attempts"),
    ("timeout budget", "timeouts", "a per-call deadline"),
    ("page size", "pagination", "a bounded page per request"),
    ("batch size", "batching", "a bounded batch per flush"),
    ("schema migration", "migration", "an additive column first"),
    ("rate limit", "throttling", "a token bucket per tenant"),
    ("log volume", "logging", "sampled debug lines"),
    ("feature flag rollout", "rollout", "a percentage ramp"),
    ("cache expiry", "caching", "a short expiry with refresh"),
    ("lock duration", "locking", "a lease renewed by the holder"),
    ("queue depth alarm", "alerting", "an alarm on sustained depth"),
    ("startup order", "startup", "dependencies checked before serving"),
)
FILLER_REASONS = (
    "the downstream service rejected bursts",
    "the nightly window was too short for the old approach",
    "operators could not tell a stuck job from a slow one",
    "the old value hid failures until the next morning",
    "the previous setting doubled the storage bill",
    "a restart lost work that was already acknowledged",
)
FILLER_PROSE = (
    "{c} runs inside {repo} and is owned by the platform group.",
    "The change keeps the public contract of {c} unchanged.",
    "Callers of {c} see no new fields; only the internal {topic} changed.",
    "The work was reviewed against the staging data set before release.",
    "Rollback is a configuration change; no data migration is involved.",
    "Metrics for {c} are tagged by tenant so a single tenant can be traced.",
    "The previous behaviour stays available behind a flag for one release.",
    "Load tests ran at twice the observed peak without errors.",
    "The on-call runbook for {c} names the dashboard to open first.",
    "A dry run in staging replayed one day of recorded traffic through {c}.",
    "The configuration lives next to the service and is reviewed like code.",
    "Each deploy of {repo} runs the {topic} checks before traffic shifts.",
    "Two alternatives were prototyped for a day each before the review.",
    "The team agreed to revisit the {topic} after one quarter of data.",
    "Error budgets for {c} did not change as a result of this work.",
    "A follow-up ticket tracks the clean-up of the old code path.",
    "Ownership of {c} moved to the platform group earlier in the year.",
    "The numbers above come from the weekly capacity report.",
)


@dataclass(frozen=True)
class Doc:
    """One session document as the generator writes it."""

    id: str
    title: str
    date: str
    repos: tuple[str, ...]
    tags: tuple[str, ...]
    entities: tuple[str, ...]
    sections: dict[str, str]
    covers_files: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    status: str = "active"
    superseded_by: str = ""
    full: bool = True

    def render(self) -> str:
        front = [
            "---",
            f"id: {self.id}",
            f"title: {self.title}",
            f"date: {self.date}",
            f"task_date: {self.date}",
            f"status: {self.status}",
            f"superseded_by: {self.superseded_by}",
            "backfilled: false",
            f"tags: [{', '.join(self.tags)}]",
            f"entities: [{', '.join(self.entities)}]",
            f"related: [{', '.join(self.related)}]",
            f"covers_files: [{', '.join(self.covers_files)}]",
            f"repos: [{', '.join(self.repos)}]",
            "verified_at:",
            "capture_minutes: 20",
            "author: Sim Author",
            "branch:",
            "pr:",
            "mode: daily",
            "---",
            "",
        ]
        headings = FULL_HEADINGS if self.full else QUICK_HEADINGS
        body = []
        for role, heading in headings:
            text = self.sections.get(role)
            if text is None:
                if not self.full:
                    continue
                text = "Nothing to report for this section."
            body.append(f"## {heading}\n\n{text.strip()}\n")
        body.append("## Search Trace\n\nshell\n")
        return "\n".join(front) + "\n".join(body)


@dataclass(frozen=True)
class Task:
    """One scenario: the task, the knowledge that solves it, and what is declared about it."""

    id: str
    scenario_class: int
    class_name: str
    description: str
    repo: str
    files: tuple[str, ...]
    expected_winner: str
    without_history: str
    decision_found: str
    decision_naive: str
    answer_docs: tuple[str, ...] = ()
    answer_role: str = ""
    quote: str = ""
    artifact: str = ""
    pitfall: str = ""
    stale_doc: str = ""
    stale_quote: str = ""
    store: str = "base"
    saver: dict = field(default_factory=dict)
    variant: str = ""
    always_second_search: bool = False


def _prose(rng: random.Random, component: str, repo: str, topic: str, count: int) -> str:
    lines = rng.sample(FILLER_PROSE, k=min(count * SENTENCE_FACTOR, len(FILLER_PROSE)))
    return " ".join(line.format(c=component, repo=repo, topic=topic) for line in lines)


def _full_sections(rng: random.Random, component: str, repo: str, topic: str, **given: str) -> dict[str, str]:
    sections = {
        "summary": _prose(rng, component, repo, topic, 3),
        "context": f"- {component}: a component of {repo}. " + _prose(rng, component, repo, topic, 2),
        "flow": f"```\ncaller -> {component} -> store\n```\n" + _prose(rng, component, repo, topic, 2),
        "requirements": "- [Verified] " + _prose(rng, component, repo, topic, 2),
        "acceptance": "- [Verified] " + _prose(rng, component, repo, topic, 1),
        "architecture": _prose(rng, component, repo, topic, 3),
        "code": f"- {component}: " + _prose(rng, component, repo, topic, 2),
        "production": _prose(rng, component, repo, topic, 3),
        "testing": "- [Verified] " + _prose(rng, component, repo, topic, 2),
        "lessons": "None hit.",
        "graph": f"{component} -> {repo} -> owner of the {topic}",
        "cheatsheet": _prose(rng, component, repo, topic, 2),
        "reuse": "Prior docs used: none.",
        "status": "- Done and released.",
    }
    sections.update(given)
    return sections


def _lesson(tried: str, failed: str, fixed: str, files: str) -> str:
    return f"- Tried: {tried}\n  Failed because: {failed}\n  Fixed by: {fixed}\n  Files: {files}"


def core_docs(rng: random.Random) -> list[Doc]:
    """The documents each scenario's answer and traps live in."""
    return [
        Doc(
            id="20260312-ledger-sweeper-retry",
            title="LedgerSweeper export retries with a fixed backoff",
            date="2026-03-12",
            repos=("ledger-core",),
            tags=("ledger", "export", "retry"),
            entities=("LedgerSweeper", "ArchiveStore"),
            covers_files=("src/ledger/LedgerSweeper.kt",),
            sections=_full_sections(
                rng, "LedgerSweeper", "ledger-core", "retry policy",
                decisions=(
                    "- Decision: LedgerSweeper retries a failed export batch three times with a fixed 40-second backoff.\n"
                    "- Rejected: exponential backoff, because its last attempt fell outside the archive window.\n"
                    "- Chosen because the archive store throttles bursts but recovers within a minute."
                ),
                lessons=_lesson(
                    "exponential backoff for the nightly export batch retries.",
                    "the last retry landed after the archive window closed.",
                    "a fixed 40-second backoff with three attempts.",
                    "src/ledger/LedgerSweeper.kt",
                ),
                primer=(
                    "LedgerSweeper retries a failed nightly export batch three times, 40 seconds apart.\n"
                    "Keep the retry total inside the archive window when changing it."
                ),
                keywords="nightly export, archive timeout, batch retry, backoff",
            ),
        ),
        Doc(
            id="20260314-ledger-sweeper-retry-notes",
            title="LedgerSweeper export retry follow-up notes",
            date="2026-03-14",
            repos=("ledger-core",),
            tags=("ledger", "export"),
            entities=("LedgerSweeper",),
            related=("20260312-ledger-sweeper-retry",),
            full=False,
            sections={
                "decisions": (
                    "- Decision: keep the export retries at three times with a fixed 40-second backoff after the review.\n"
                    "- Reason: the review found no new failure mode.\n"
                    "- Rejected alternative: not stated in the available material.\n"
                    "- Source: not stated in the available material."
                ),
                "primer": "Follow-up to the LedgerSweeper export retry decision; nothing changed.",
                "reuse": "Prior docs used: none.",
            },
        ),
        Doc(
            id="20260313-ledger-sweeper-import-retry",
            title="LedgerSweeper import retries use exponential backoff",
            date="2026-03-13",
            repos=("ledger-core",),
            tags=("ledger", "import", "retry"),
            entities=("LedgerSweeper", "ImportReader"),
            covers_files=("src/ledger/ImportReader.kt",),
            sections=_full_sections(
                rng, "LedgerSweeper", "ledger-core", "retry policy",
                decisions=(
                    "- Decision: the LedgerSweeper import path retries with exponential backoff up to ten minutes.\n"
                    "- Rejected: a fixed delay, because the import source asks clients to back off progressively."
                ),
                primer=(
                    "LedgerSweeper import retries back off exponentially; this is the import path, not the nightly export.\n"
                    "The export batch has its own retry decision."
                ),
            ),
        ),
        Doc(
            id="20260402-tile-prewarm",
            title="Tile prewarm before traffic on deploy",
            date="2026-04-02",
            repos=("tile-render",),
            tags=("tiles", "deploy"),
            entities=("TilePrewarmer", "TileCache"),
            covers_files=("src/tiles/TilePrewarmer.ts", "src/tiles/TileCache.ts"),
            sections=_full_sections(
                rng, "TilePrewarmer", "tile-render", "startup order",
                decisions=(
                    "- Decision: prewarm the top 500 tiles before the readiness probe passes.\n"
                    "- Rejected: lazy fill on the first request, because the first requests after each deploy took seconds."
                ),
                lessons=_lesson(
                    "lazy tile fill on demand after a deploy.",
                    "every new instance served its first requests from an empty TileCache.",
                    "a prewarm step gated by the readiness probe.",
                    "src/tiles/TilePrewarmer.ts",
                ),
                primer=(
                    "TilePrewarmer fills TileCache from the access ranking before the instance takes traffic.\n"
                    "Readiness waits for the prewarm to finish."
                ),
                keywords="cold start, first request latency, warm-up, readiness",
            ),
        ),
        Doc(
            id="20260220-map-widget-theme",
            title="MapWidget dark theme colours",
            date="2026-02-20",
            repos=("tile-render",),
            tags=("ui", "theme"),
            entities=("MapWidget",),
            covers_files=("src/map/MapWidget.ts",),
            sections=_full_sections(
                rng, "MapWidget", "tile-render", "feature flag rollout",
                decisions=(
                    "- Decision: MapWidget reads its colours from the theme tokens on first page view.\n"
                    "- Rejected: hard-coded palettes, because each theme change needed a rollout."
                ),
                primer="MapWidget takes its colours from theme tokens; no palette lives in the widget.",
            ),
        ),
        Doc(
            id="20260115-intake-queue-consumer",
            title="IntakeQueue consumer concurrency",
            date="2026-01-15",
            repos=("intake-gateway",),
            tags=("queue", "intake"),
            entities=("IntakeQueue", "OrderHandler"),
            covers_files=("src/intake/IntakeConsumer.go",),
            sections=_full_sections(
                rng, "IntakeQueue", "intake-gateway", "batch size",
                decisions=(
                    "- Decision: the IntakeQueue consumer runs eight workers per partition.\n"
                    "- Rejected: one worker per partition, because the backlog grew during the morning peak."
                ),
                lessons=_lesson(
                    "acknowledging each message before the handler committed its order.",
                    "a crash between ack and commit dropped the order.",
                    "acknowledging only after the commit returns.",
                    "src/intake/IntakeConsumer.go",
                ),
                primer=(
                    "The IntakeQueue consumer runs eight workers per partition.\n"
                    "Worker count follows partition count; scale partitions, not threads."
                ),
            ),
        ),
        Doc(
            id="20260118-intake-queue-producer-batching",
            title="IntakeQueue producer batching",
            date="2026-01-18",
            repos=("intake-gateway",),
            tags=("queue", "intake"),
            entities=("IntakeQueue", "IntakeProducer"),
            sections=_full_sections(
                rng, "IntakeQueue", "intake-gateway", "batch size",
                decisions=(
                    "- Decision: the IntakeQueue producer flushes every 50 messages or 200 milliseconds.\n"
                    "- Rejected: per-message sends, because the broker connection count doubled."
                ),
                primer="The IntakeQueue producer batches sends; the consumer side is a separate decision.",
            ),
        ),
        Doc(
            id="20251104-report-store-pool",
            title="ReportStore client connection pool size",
            date="2025-11-04",
            repos=("report-hub",),
            tags=("database", "pool"),
            entities=("ReportStore", "ReportStoreClient"),
            covers_files=("src/report/ReportStoreClient.java",),
            status="superseded",
            superseded_by="20260521-report-store-pool-cap",
            sections=_full_sections(
                rng, "ReportStoreClient", "report-hub", "batch size",
                decisions=(
                    "- Decision: the ReportStore client connection pool size is 200 connections per instance.\n"
                    "- Rejected: a pool of 50, because report rendering queued at the month-end peak."
                ),
                primer=(
                    "ReportStore client connection pool size: 200 connections per instance.\n"
                    "Sized for the month-end report peak."
                ),
                keywords="connection pool size, ReportStore client, pool connections",
            ),
        ),
        Doc(
            id="20260521-report-store-pool-cap",
            title="ReportStore pool cap after the session limit incident",
            date="2026-05-21",
            repos=("report-hub",),
            tags=("database",),
            entities=("ReportStore",),
            covers_files=("src/report/ReportStoreClient.java",),
            related=("20251104-report-store-pool",),
            sections=_full_sections(
                rng, "ReportStore", "report-hub", "lock duration",
                decisions=(
                    "- Decision: cap the ReportStore pool at 64 connections per instance.\n"
                    "- Rejected: keeping 200, because 200 exhausted the database session limit at six instances."
                ),
                lessons=_lesson(
                    "the old pool of 200 per instance after scaling out.",
                    "200 exhausted the database session limit at six instances.",
                    "a cap of 64 per instance and a queue in front of the pool.",
                    "src/report/ReportStoreClient.java",
                ),
                primer=(
                    "ReportStore pool is capped at 64 connections per instance; this replaces the 200 setting.\n"
                    "Total sessions = instances x 64 must stay under the database limit."
                ),
            ),
        ),
        Doc(
            id="20260228-audit-event-timestamps",
            title="Audit event timestamp format",
            date="2026-02-28",
            repos=("ledger-core",),
            tags=("audit", "events"),
            entities=("AuditEvent", "AuditEmitter"),
            covers_files=("src/audit/AuditEmitter.kt",),
            sections=_full_sections(
                rng, "AuditEmitter", "ledger-core", "log volume",
                decisions=(
                    "- Decision: audit events carry UTC timestamps in RFC 3339 with millisecond precision.\n"
                    "- Rejected: local time with an offset, because the nightly reconciliation joined on the raw string."
                ),
                lessons=_lesson(
                    "local time offsets in audit event timestamps.",
                    "local time offsets broke the nightly reconciliation join.",
                    "UTC everywhere, formatted once in AuditEmitter.",
                    "src/audit/AuditEmitter.kt",
                ),
                primer=(
                    "Audit events use UTC RFC 3339 timestamps with milliseconds, formatted in AuditEmitter.\n"
                    "Any service that emits audit events follows the same format."
                ),
            ),
        ),
        Doc(
            id="20260601-archive-exporter-page-size",
            title="ArchiveExporter page size under the gateway timeout",
            date="2026-06-01",
            repos=("report-hub",),
            tags=("export", "pagination"),
            entities=("ArchiveExporter",),
            covers_files=("src/export/ArchiveExporter.java",),
            sections=_full_sections(
                rng, "ArchiveExporter", "report-hub", "page size",
                decisions=(
                    "- Decision: a page size of 2000 rows keeps each request under the 30-second gateway timeout.\n"
                    "- Rejected: 10000 rows, because large tenants hit the gateway timeout."
                ),
                lessons=_lesson(
                    "raising the ArchiveExporter page to 10000 rows.",
                    "large tenants crossed the 30-second gateway timeout mid-page.",
                    "2000 rows per page and a cursor between pages.",
                    "src/export/ArchiveExporter.java",
                ),
                primer=(
                    "ArchiveExporter pages at 2000 rows so each request stays under the gateway timeout.\n"
                    "Raise it only with a timing check on the largest tenant."
                ),
            ),
        ),
    ]


SECOND_SEARCH_VARIANT = "always second search"


def tasks() -> list[Task]:
    """One scenario per class, plus class 2 under the second-search variant; winners declared first."""
    base = _base_tasks()
    gap = next(t for t in base if t.scenario_class == 2)
    variant = dataclasses.replace(
        gap,
        id="wording-gap-second-search",
        variant=SECOND_SEARCH_VARIANT,
        always_second_search=True,
        expected_winner="engmem and grep both reach the answer once the second search always runs",
    )
    index = base.index(gap) + 1
    return [*base[:index], variant, *base[index:]]


def _base_tasks() -> list[Task]:
    return [
        Task(
            id="keyword",
            scenario_class=1,
            class_name="answer exists, keyword-findable",
            description="Add retries to the LedgerSweeper nightly export batch when the archive store times out",
            repo="ledger-core",
            files=("src/ledger/LedgerSweeper.kt",),
            answer_docs=("20260312-ledger-sweeper-retry", "20260314-ledger-sweeper-retry-notes"),
            answer_role="decisions",
            quote="three times with a fixed 40-second backoff",
            artifact="LedgerSweeper export retry",
            pitfall="the last retry landed after the archive window closed",
            expected_winner="engmem and grep both beat no history",
            without_history="picks exponential backoff and overruns the archive window",
            decision_found="Retry the export batch three times with a fixed 40-second backoff.",
            decision_naive="Retry the export batch with exponential backoff.",
        ),
        Task(
            id="wording-gap",
            scenario_class=2,
            class_name="answer exists, wording gap",
            description="The first page view after a rollout is slow for the map widget; make it faster",
            repo="tile-render",
            files=("src/map/MapWidget.ts", "src/tiles/TileCache.ts"),
            answer_docs=("20260402-tile-prewarm",),
            answer_role="decisions",
            quote="prewarm the top 500 tiles before the readiness probe passes",
            artifact="TilePrewarmer readiness gate",
            pitfall="every new instance served its first requests from an empty TileCache",
            expected_winner="engmem (ranked sections, camelCase split) over grep; both over no history",
            without_history="tunes the widget code and misses the empty cache after each deploy",
            decision_found="Prewarm the top 500 tiles before the readiness probe passes.",
            decision_naive="Profile and optimise the MapWidget render path.",
        ),
        Task(
            id="role",
            scenario_class=3,
            class_name="answer in a non-primer section (role search)",
            description="Change the IntakeQueue consumer acknowledgement; list the pitfalls we hit before",
            repo="intake-gateway",
            files=("src/intake/IntakeConsumer.go",),
            answer_docs=("20260115-intake-queue-consumer",),
            answer_role="lessons",
            quote="a crash between ack and commit dropped the order",
            artifact="IntakeQueue acknowledgement pitfall",
            pitfall="a crash between ack and commit dropped the order",
            expected_winner="engmem (role search returns the section) over grep (whole files); both over no history",
            without_history="acknowledges before commit and loses orders on a crash",
            decision_found="Acknowledge only after the order commit returns.",
            decision_naive="Acknowledge each message as soon as it is received.",
        ),
        Task(
            id="stale",
            scenario_class=4,
            class_name="stale trap (superseded decision)",
            description="Set the connection pool size for the ReportStore client",
            repo="report-hub",
            files=("src/report/ReportStoreClient.java",),
            answer_docs=("20260521-report-store-pool-cap",),
            answer_role="decisions",
            quote="cap the ReportStore pool at 64 connections per instance",
            artifact="ReportStore pool cap",
            pitfall="200 exhausted the database session limit at six instances",
            stale_doc="20251104-report-store-pool",
            stale_quote="200 connections per instance",
            expected_winner="engmem (search redirects to the successor) over grep (reads the stale file first)",
            without_history="picks a large pool and exhausts the database session limit",
            decision_found="Cap the ReportStore pool at 64 connections per instance.",
            decision_naive="Size the ReportStore pool at 200 connections per instance.",
        ),
        Task(
            id="no-history",
            scenario_class=5,
            class_name="no relevant history",
            description="Add a keyboard shortcut to toggle the sidebar in the admin console",
            repo="report-hub",
            files=("src/console/Sidebar.tsx",),
            expected_winner="no history (nothing to find); engmem should cost little and say none found",
            without_history="implements the shortcut; nothing is lost",
            decision_found="Bind the shortcut in the sidebar component.",
            decision_naive="Bind the shortcut in the sidebar component.",
        ),
        Task(
            id="cross-repo",
            scenario_class=6,
            class_name="cross-repository answer",
            description="Choose the timestamp format for audit events emitted by the intake gateway",
            repo="intake-gateway",
            files=("src/intake/AuditHook.go",),
            answer_docs=("20260228-audit-event-timestamps",),
            answer_role="decisions",
            quote="UTC timestamps in RFC 3339 with millisecond precision",
            artifact="AuditEvent timestamp format",
            pitfall="local time offsets broke the nightly reconciliation join",
            expected_winner="engmem and grep tie (both search every repository); both over no history",
            without_history="emits local time with an offset and breaks the reconciliation join",
            decision_found="Emit UTC timestamps in RFC 3339 with millisecond precision.",
            decision_naive="Emit local time with an offset.",
        ),
        Task(
            id="scale",
            scenario_class=7,
            class_name="larger store",
            description="Raise the ArchiveExporter page size so exports finish sooner",
            repo="report-hub",
            files=("src/export/ArchiveExporter.java",),
            answer_docs=("20260601-archive-exporter-page-size",),
            answer_role="decisions",
            quote="page size of 2000 rows keeps each request under the 30-second gateway timeout",
            artifact="ArchiveExporter page size",
            pitfall="large tenants crossed the 30-second gateway timeout mid-page",
            store="scale",
            expected_winner="engmem over grep on cost on a larger store; both over no history",
            without_history="raises the page size and large tenants hit the gateway timeout",
            decision_found="Keep 2000 rows per page; add a cursor instead of raising it.",
            decision_naive="Raise the page size to 10000 rows.",
        ),
        Task(
            id="multi-agent",
            scenario_class=8,
            class_name="multi-agent: one saves, another finds and cites",
            description="Add a retry to the order intake call in the gateway client without creating duplicate orders",
            repo="intake-gateway",
            files=("src/intake/GatewayClient.go",),
            answer_docs=("{date}-intake-retry-idempotency",),
            answer_role="decisions",
            quote="reuse the client request token as the idempotency key for 24 hours",
            artifact="intake retry idempotency key",
            pitfall="a retried intake call created a second order",
            expected_winner="engmem (the first agent's save is findable); without engmem nothing was saved",
            without_history="retries without a key and creates duplicate orders",
            decision_found="Retry with the client request token as the idempotency key.",
            decision_naive="Retry the intake call up to three times.",
            saver={
                "doc_slug": "intake-retry-idempotency",
                "description": "Decide how order intake retries detect duplicate orders",
                "title": "Order intake retries reuse the request token as idempotency key",
                "entities": ["IntakeRetry", "IdempotencyKey"],
                "decision": (
                    "- Decision: intake retries reuse the client request token as the idempotency key for 24 hours.\n"
                    "- Rejected: a server-generated key per attempt, because a retried intake call created a second order."
                ),
                "lesson": [
                    "a server-generated key on each retry attempt.",
                    "a retried intake call created a second order.",
                    "reusing the client request token as the key.",
                    "src/intake/Retry.go",
                ],
                "primer": (
                    "Order intake retries are safe because the client request token is the idempotency key.\n"
                    "The key is kept for 24 hours on the intake side."
                ),
            },
        ),
    ]


RESERVED_FOR_CORE = frozenset({("ArchiveExporter", "page size")})
ANSWER_CHAIN_COMPONENT = "ArchiveExporter"
ANSWER_CHAIN_TOPIC = "page size"
ANSWER_CHAIN_SUCCESSOR = "20260601-archive-exporter-page-size"
ANSWER_CHAIN_VALUES = (500, 750, 1000, 1500, 3000, 5000, 8000, 12000)


def _filler_doc(rng: random.Random, index: int) -> Doc:
    component = rng.choice(FILLER_COMPONENTS)
    topic, tag, approach = rng.choice(FILLER_TOPICS)
    while (component, topic) in RESERVED_FOR_CORE:
        topic, tag, approach = rng.choice(FILLER_TOPICS)
    reason = rng.choice(FILLER_REASONS)
    repo = rng.choice(REPOS)
    month = 1 + index % 12
    day = 1 + index % 28
    year = 2025 if index % 3 else 2026
    slug = f"{component.lower()}-{topic.replace(' ', '-')}-{index}"
    doc_id = f"{year}{month:02d}{day:02d}-{slug}"
    value = rng.choice((3, 5, 8, 12, 20, 45, 90, 250, 750))
    decision = (
        f"- Decision: {component} uses {approach} for its {topic}, set to {value}.\n"
        f"- Rejected: leaving the {topic} unbounded, because {reason}."
    )
    primer = f"{component} {topic}: {approach}, value {value}.\nChange it together with the {tag} dashboards."
    full = index % 2 == 0
    if full:
        sections = _full_sections(rng, component, repo, topic, decisions=decision, primer=primer)
    else:
        sections = {"decisions": decision, "primer": primer, "reuse": "Prior docs used: none."}
    return Doc(
        id=doc_id,
        title=f"{component} {topic}",
        date=f"{year}-{month:02d}-{day:02d}",
        repos=(repo,),
        tags=(tag,),
        entities=(component,),
        sections=sections,
        full=full,
    )


def _with_supersede_chains(docs: list[Doc]) -> list[Doc]:
    """Filler on one component and topic forms a chain: each superseded by the next newer one."""
    groups: dict[str, list[int]] = {}
    for index, doc in enumerate(docs):
        groups.setdefault(doc.title, []).append(index)
    result = list(docs)
    for indexes in groups.values():
        ordered = sorted(indexes, key=lambda i: (docs[i].date, docs[i].id))
        for older, newer in zip(ordered, ordered[1:]):
            result[older] = dataclasses.replace(docs[older], status="superseded", superseded_by=docs[newer].id)
    return result


def answer_topic_chain(count: int) -> list[Doc]:
    """Short filler on the class 7 answer's topic, one chain whose last link the answer supersedes."""
    combinations = len(FILLER_COMPONENTS) * len(FILLER_TOPICS)
    length = min(len(ANSWER_CHAIN_VALUES), max(2, count // combinations))
    ids = [f"2025{month:02d}10-archiveexporter-page-size-chain-{month}" for month in range(1, length + 1)]
    successors = [*ids[1:], ANSWER_CHAIN_SUCCESSOR]
    return [
        Doc(
            id=doc_id,
            title=f"{ANSWER_CHAIN_COMPONENT} {ANSWER_CHAIN_TOPIC}",
            date=f"2025-{month:02d}-10",
            repos=("report-hub",),
            tags=("pagination",),
            entities=(ANSWER_CHAIN_COMPONENT,),
            status="superseded",
            superseded_by=successor,
            full=False,
            sections={
                "decisions": (
                    f"- Decision: {ANSWER_CHAIN_COMPONENT} pages at {value} rows.\n"
                    "- Reason: not stated in the available material."
                ),
                "primer": f"{ANSWER_CHAIN_COMPONENT} page size: {value} rows.",
                "reuse": "Prior docs used: none.",
            },
        )
        for month, (doc_id, successor, value) in enumerate(zip(ids, successors, ANSWER_CHAIN_VALUES), start=1)
    ]


def filler_docs(count: int, seed: int = SEED) -> list[Doc]:
    """Random filler, chained per component and topic, plus the one chain on the class 7 answer's topic."""
    rng = random.Random(seed + 1)
    return _with_supersede_chains([_filler_doc(rng, i) for i in range(count)]) + answer_topic_chain(count)


@dataclass(frozen=True)
class Corpus:
    """A generated store directory per size, and the tasks that run against it."""

    root: Path
    stores: dict[str, Path]
    tasks: list[Task]

    def store_for(self, task: Task) -> Path:
        return self.stores[task.store]


def _write_store(directory: Path, docs: list[Doc]) -> Path:
    sessions = directory / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (sessions / f"{doc.id}.md").write_text(doc.render(), encoding="utf-8", newline="\n")
    return sessions


def generate(root: Path, *, filler: int = DEFAULT_FILLER, scale_filler: int = SCALE_FILLER, seed: int = SEED) -> Corpus:
    """Writes the base store, the scale store and `tasks.json` under `root`; same seed, same bytes."""
    core = core_docs(random.Random(seed))
    base = _write_store(root / "base", core + filler_docs(filler, seed))
    scale = _write_store(root / "scale", core + filler_docs(scale_filler, seed))
    all_tasks = tasks()
    (root / "tasks.json").write_text(
        json.dumps([asdict(t) for t in all_tasks], indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return Corpus(root=root, stores={"base": base, "scale": scale}, tasks=all_tasks)
