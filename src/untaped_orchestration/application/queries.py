from __future__ import annotations

from untaped_orchestration.application.ports import Clock, StoreReader
from untaped_orchestration.application.query_brief import assemble_brief
from untaped_orchestration.application.query_models import (
    BriefData,
    BriefRequest,
    HistoryListRequest,
    HistoryRequest,
    HistorySearchRequest,
    HistoryShowRequest,
    ItemDetail,
    ItemRow,
    ListRequest,
    NextItem,
    NextRequest,
    QualifiedItem,
    QueryResult,
    QueryScope,
    RawShowRequest,
    SearchHit,
    SearchRequest,
    ShowRequest,
    TraceData,
    TraceRequest,
)
from untaped_orchestration.application.query_models import (
    TraceDirection as TraceDirection,
)
from untaped_orchestration.application.query_projection import (
    SafeProjection,
    active_records,
    detail_for,
    project_safely,
    row_for,
    selected_stores,
    store_revisions,
)
from untaped_orchestration.application.query_search import stream_search
from untaped_orchestration.application.query_trace import build_trace
from untaped_orchestration.application.results import (
    FederatedSnapshot,
    LoadedRecord,
    RawRecord,
    StoreSnapshot,
)
from untaped_orchestration.domain.curation import StoreCurationContext, curation_queue
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.graph import TaskRef, readiness
from untaped_orchestration.domain.ids import DecisionId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    LinkRelation,
    Revision,
    TaskOutcome,
)
from untaped_orchestration.domain.ordering import TaskOrderItem, sort_tasks
from untaped_orchestration.domain.time import UtcTimestamp

MAX_LIMIT = 200


class QueryIncompleteError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("query requires a complete federation; retry locally or restore children")


def _limit(value: int) -> int:
    if not 1 <= value <= MAX_LIMIT:
        raise ValueError("limit must be in range 1..200")
    return value


def _selected(snapshot: FederatedSnapshot, local: bool) -> tuple[StoreSnapshot, ...]:
    return (snapshot.selected,) if local else snapshot.stores


def _find(
    snapshot: FederatedSnapshot,
    item_id: TaskId | DecisionId,
    local: bool,
) -> tuple[StoreSnapshot, LoadedRecord]:
    matches = [
        (store, record)
        for store in _selected(snapshot, local)
        for record in store.records
        if record.metadata.id == item_id
    ]
    if len(matches) != 1:
        raise ValueError("item does not resolve uniquely")
    return matches[0]


class QueryService:
    def __init__(self, scope: QueryScope, reader: StoreReader, clock: Clock) -> None:
        self._scope = scope
        self._reader = reader
        self._clock = clock

    def _load(self, local: bool) -> FederatedSnapshot:
        return (self._scope.local if local else self._scope.recursive)()

    def _project(self, snapshot: FederatedSnapshot, local: bool) -> SafeProjection:
        return project_safely(
            snapshot,
            local=local,
            now=UtcTimestamp.from_datetime(self._clock.now()),
        )

    def _result[T](
        self,
        snapshot: FederatedSnapshot,
        data: T,
        *,
        local: bool,
        truncated: bool = False,
        revisions: tuple[tuple[str, Revision], ...] = (),
        retained: int = 0,
    ) -> QueryResult[T]:
        projection = self._project(snapshot, local)
        return QueryResult(
            data,
            projection.complete,
            truncated,
            projection.diagnostics,
            store_revisions(projection),
            revisions,
            retained,
        )

    def list(self, request: ListRequest) -> QueryResult[tuple[ItemRow, ...]]:
        limit = _limit(request.limit)
        snapshot = self._load(request.local)
        projection = self._project(snapshot, request.local)
        rows = []
        for store, record in active_records(projection):
            metadata = record.metadata
            if request.kind is not None and metadata.kind is not request.kind:
                continue
            if request.stage is not None and (
                not isinstance(metadata, ActiveTask) or metadata.stage is not request.stage
            ):
                continue
            if request.tag is not None and request.tag not in {
                value.root for value in metadata.tags
            }:
                continue
            if request.waiting_on is not None and (
                not isinstance(metadata, ActiveTask)
                or request.waiting_on not in {value.root for value in metadata.waiting_on}
            ):
                continue
            assert store.store is not None
            row = row_for(projection, store.store.id, record)
            if request.decision_state is not None and row.state is not request.decision_state:
                continue
            rows.append(row)
        rows.sort(key=lambda value: (value.kind.value, value.store_id.root, value.path))
        return self._result(
            snapshot,
            tuple(rows[:limit]),
            local=request.local,
            truncated=len(rows) > limit,
            revisions=tuple((row.item_id.root, row.revision) for row in rows[:limit]),
        )

    def show(self, request: ShowRequest) -> QueryResult[ItemDetail]:
        snapshot = self._load(request.local)
        store, record = _find(snapshot, request.item_id, request.local)
        assert store.store is not None
        body = self._reader.read_item_body(store.location, record.path)
        projection = self._project(snapshot, request.local)
        detail = detail_for(projection, store, record, body)
        return self._result(
            snapshot,
            detail,
            local=request.local,
            revisions=((record.metadata.id.root, record.revision),),
            retained=1,
        )

    def show_raw(self, request: RawShowRequest) -> QueryResult[RawRecord]:
        snapshot = self._load(True)
        prefix = request.item_id.root
        matches = tuple(
            value
            for value in snapshot.selected.raw_index
            if value.path.name.startswith(f"{prefix}-")
        )
        if len(matches) != 1:
            raise ValueError("raw item filename prefix does not resolve uniquely in selected store")
        raw = self._reader.read_raw(snapshot.selected.location, matches[0].path)
        return self._result(
            snapshot,
            raw,
            local=True,
            revisions=((prefix, raw.revision),),
            retained=1,
        )

    def search(self, request: SearchRequest) -> QueryResult[tuple[SearchHit, ...]]:
        limit = _limit(request.limit)
        if not request.query:
            raise ValueError("search query must be nonempty")
        snapshot = self._load(request.local)
        projection = self._project(snapshot, request.local)
        hits, total = stream_search(
            projection,
            self._reader,
            request.query,
            limit=limit,
            archived=request.history,
        )
        return self._result(
            snapshot, tuple(hits), local=request.local, truncated=total > limit, retained=len(hits)
        )

    def next(self, request: NextRequest) -> QueryResult[tuple[NextItem, ...]]:
        limit = _limit(request.limit)
        snapshot = self._load(request.local)
        projection = self._project(snapshot, request.local)
        if not projection.complete:
            raise QueryIncompleteError(projection.diagnostics)
        graph = projection.graph
        candidates = []
        by_key = {}
        for store, record in active_records(projection):
            if isinstance(record.metadata, ActiveTask):
                assert store.store is not None
                key = (store.store.id, record.metadata.id)
                by_key[key] = record
                if readiness(TaskRef(*key), graph).ready:
                    candidates.append(TaskOrderItem(store.store.id, record.metadata))
        ordered = sort_tasks(candidates)
        contexts = tuple(
            StoreCurationContext(store.store.id, store.store.timezone, store.store.curation)
            for store in projection.snapshot.stores
            if store.store is not None
        )
        due_ids = {
            (value.store_id, value.item_id)
            for value in curation_queue(
                graph,
                now=UtcTimestamp.from_datetime(self._clock.now()),
                contexts=contexts,
            )
        }
        task_index = {
            (store_id, task_id): record.metadata
            for (store_id, task_id), record in by_key.items()
            if isinstance(record.metadata, ActiveTask)
        }
        rows: list[NextItem] = []
        for value in ordered[:limit]:
            record = by_key[(value.store_id, value.task.id)]
            ancestors: list[TaskId] = []
            parent = value.task.parent
            while parent is not None:
                ancestors.append(parent)
                parent = task_index[(value.store_id, parent)].parent
            governing = tuple(
                QualifiedItem(link.target_store_id, link.target)
                for link in value.task.links
                if link.relation is LinkRelation.GOVERNED_BY and isinstance(link.target, DecisionId)
            )
            unblocks = sum(
                1
                for candidate in task_index.values()
                for link in candidate.links
                if link.relation is LinkRelation.DEPENDS_ON
                and link.target_store_id == value.store_id
                and link.target == value.task.id
            )
            rows.append(
                NextItem(
                    row_for(projection, value.store_id, record),
                    tuple(reversed(ancestors)),
                    unblocks,
                    (value.store_id, value.task.id) in due_ids,
                    governing,
                    tuple(item.reference.root for item in value.task.evidence),
                )
            )
        result_rows = tuple(rows)
        return self._result(
            snapshot,
            result_rows,
            local=request.local,
            truncated=len(ordered) > limit,
            revisions=tuple((value.row.item_id.root, value.row.revision) for value in result_rows),
        )

    def history(
        self, request: HistoryRequest
    ) -> (
        QueryResult[tuple[ItemRow, ...]]
        | QueryResult[tuple[SearchHit, ...]]
        | QueryResult[ItemDetail]
    ):
        if request.item_id is not None:
            return self.history_show(HistoryShowRequest(request.item_id, local=request.local))
        if request.query is not None:
            return self.history_search(
                HistorySearchRequest(request.query, local=request.local, limit=request.limit)
            )
        return self.history_list(HistoryListRequest(local=request.local, limit=request.limit))

    def history_list(self, request: HistoryListRequest) -> QueryResult[tuple[ItemRow, ...]]:
        limit = _limit(request.limit)
        snapshot = self._load(request.local)
        projection = self._project(snapshot, request.local)
        outcome = (
            request.outcome.value if isinstance(request.outcome, TaskOutcome) else request.outcome
        )
        rows = []
        for store in projection.snapshot.stores:
            if store.store is None:
                continue
            for record in store.records:
                metadata = record.metadata
                if not isinstance(metadata, ArchivedTask):
                    continue
                if outcome is not None and metadata.outcome.value != outcome:
                    continue
                if request.tag is not None and request.tag not in {
                    value.root for value in metadata.tags
                }:
                    continue
                rows.append(row_for(projection, store.store.id, record))
        rows.sort(key=lambda value: (value.store_id.root, value.path))
        selected = tuple(rows[:limit])
        return self._result(
            snapshot,
            selected,
            local=request.local,
            truncated=len(rows) > limit,
            revisions=tuple((row.item_id.root, row.revision) for row in selected),
        )

    def history_search(self, request: HistorySearchRequest) -> QueryResult[tuple[SearchHit, ...]]:
        limit = _limit(request.limit)
        if not request.query:
            raise ValueError("search query must be nonempty")
        snapshot = self._load(request.local)
        projection = self._project(snapshot, request.local)
        hits, total = stream_search(
            projection,
            self._reader,
            request.query,
            limit=limit,
            archived=True,
        )
        return self._result(
            snapshot,
            hits,
            local=request.local,
            truncated=total > limit,
            revisions=tuple((hit.row.item_id.root, hit.row.revision) for hit in hits),
            retained=len(hits),
        )

    def history_show(self, request: HistoryShowRequest) -> QueryResult[ItemDetail]:
        snapshot = self._load(request.local)
        matches = [
            (store, record)
            for store in selected_stores(snapshot, request.local)
            for record in store.records
            if isinstance(record.metadata, ArchivedTask) and record.metadata.id == request.item_id
        ]
        if len(matches) != 1:
            raise ValueError("archived item does not resolve uniquely")
        store, record = matches[0]
        assert store.store is not None
        body = self._reader.read_item_body(store.location, record.path)
        projection = self._project(snapshot, request.local)
        detail = detail_for(projection, store, record, body)
        return self._result(
            snapshot,
            detail,
            local=request.local,
            revisions=((record.metadata.id.root, record.revision),),
            retained=1,
        )

    def trace(self, request: TraceRequest) -> QueryResult[TraceData]:
        limit = _limit(request.limit)
        snapshot = self._load(request.local)
        stores = selected_stores(snapshot, request.local)
        root_store, root_record = _find(snapshot, request.item_id, request.local)
        assert root_store.store is not None
        root = QualifiedItem(root_store.store.id, root_record.metadata.id)
        data, truncated = build_trace(
            stores,
            root,
            direction=request.direction,
            limit=limit,
        )
        return self._result(snapshot, data, local=request.local, truncated=truncated)

    def brief(self, request: BriefRequest) -> QueryResult[BriefData]:
        snapshot = self._load(request.local)
        projection = self._project(snapshot, request.local)
        data, truncated, revisions, retained = assemble_brief(projection, self._reader)
        return self._result(
            snapshot,
            data,
            local=request.local,
            truncated=truncated,
            revisions=revisions,
            retained=retained,
        )
