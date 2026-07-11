from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from untaped_orchestration.application.results import FederatedSnapshot
from untaped_orchestration.domain.curation import CurationEntry
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.graph import DecisionState
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    ItemKind,
    LinkRelation,
    Revision,
    TaskOutcome,
    TaskPriority,
    TaskStage,
)
from untaped_orchestration.domain.time import CalendarDate

DEFAULT_LIMIT = 50


@dataclass(frozen=True, slots=True)
class QueryScope:
    recursive: Callable[[], FederatedSnapshot]
    local: Callable[[], FederatedSnapshot]


@dataclass(frozen=True, slots=True)
class QueryResult[T]:
    data: T
    complete: bool
    truncated: bool
    diagnostics: tuple[Diagnostic, ...]
    store_revisions: tuple[tuple[str, Revision], ...]
    item_revisions: tuple[tuple[str, Revision], ...] = ()
    retained_bodies: int = 0


@dataclass(frozen=True, slots=True)
class ListRequest:
    kind: ItemKind | None = None
    stage: TaskStage | None = None
    decision_state: DecisionState | None = None
    tag: str | None = None
    waiting_on: str | None = None
    local: bool = False
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class ShowRequest:
    item_id: TaskId | DecisionId
    local: bool = False


@dataclass(frozen=True, slots=True)
class RawShowRequest:
    item_id: TaskId | DecisionId


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    local: bool = False
    history: bool = False
    limit: int = DEFAULT_LIMIT


class TraceDirection(StrEnum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class TraceRequest:
    item_id: TaskId | DecisionId
    direction: TraceDirection = TraceDirection.BOTH
    local: bool = False
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class NextRequest:
    local: bool = False
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class HistoryRequest:
    item_id: TaskId | None = None
    query: str | None = None
    local: bool = False
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class HistoryListRequest:
    outcome: TaskOutcome | str | None = None
    tag: str | None = None
    local: bool = False
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class HistorySearchRequest:
    query: str
    local: bool = False
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class HistoryShowRequest:
    item_id: TaskId
    local: bool = False


@dataclass(frozen=True, slots=True)
class BriefRequest:
    local: bool = False


@dataclass(frozen=True, slots=True)
class ItemRow:
    item_id: TaskId | DecisionId
    kind: ItemKind
    title: str
    store_id: StoreId
    path: str
    revision: Revision
    stage: TaskStage | None = None
    state: DecisionState | None = None
    waiting_on: tuple[str, ...] = ()
    priority: TaskPriority | None = None
    rank: int | None = None
    due_on: CalendarDate | None = None


@dataclass(frozen=True, slots=True)
class ItemDetail:
    row: ItemRow
    metadata: ActiveTask | ArchivedTask | Decision
    body: bytes
    store_revision: Revision
    blocked: bool | None = None
    blockers: tuple[str, ...] = ()
    due_on: CalendarDate | None = None
    complete: bool = True

    @property
    def revision(self) -> Revision:
        return self.row.revision


@dataclass(frozen=True, slots=True)
class NextItem:
    row: ItemRow
    ancestor_path: tuple[TaskId, ...]
    unblocks_count: int
    due: bool
    governing_decisions: tuple[QualifiedItem, ...]
    evidence_summary: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchHit:
    row: ItemRow
    snippet: str


@dataclass(frozen=True, slots=True)
class QualifiedItem:
    store_id: StoreId
    item_id: TaskId | DecisionId


@dataclass(frozen=True, slots=True)
class TraceItem:
    item: QualifiedItem
    depth: int


@dataclass(frozen=True, slots=True)
class TraceLink:
    source: QualifiedItem
    target: QualifiedItem
    relation: LinkRelation
    depth: int


@dataclass(frozen=True, slots=True)
class TraceEvidence:
    owner: QualifiedItem
    relation: str
    reference: str
    depth: int


@dataclass(frozen=True, slots=True)
class TraceData:
    root: QualifiedItem
    items: tuple[TraceItem, ...]
    links: tuple[TraceLink, ...]
    evidence: tuple[TraceEvidence, ...]


@dataclass(frozen=True, slots=True)
class BriefDecision:
    item_id: DecisionId
    title: str
    revision: Revision
    body: bytes


@dataclass(frozen=True, slots=True)
class InactiveRuling:
    store_id: StoreId
    item_id: DecisionId
    state: DecisionState | None


@dataclass(frozen=True, slots=True)
class BriefData:
    store_id: StoreId
    store_revision: Revision
    registry_revision: Revision | None
    item_revisions: tuple[tuple[str, Revision], ...]
    pinned_decisions: tuple[BriefDecision, ...]
    inactive_rulings: tuple[InactiveRuling, ...]
    in_progress: ItemRow | None
    ready: tuple[ItemRow, ...]
    blockers: tuple[ItemRow, ...]
    due: tuple[CurationEntry, ...]
    diagnostics: tuple[Diagnostic, ...]
    missing_store_ids: tuple[str, ...]
    ready_count: int
    blocker_count: int
    due_count: int
    diagnostic_count: int
    missing_store_count: int
    inactive_ruling_count: int
    globally_ready: bool
