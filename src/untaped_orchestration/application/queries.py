from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from untaped_orchestration.application.ports import Clock, StoreReader
from untaped_orchestration.application.query_models import (
    BriefData,
    BriefDecision,
    BriefRequest,
    HistoryRequest,
    InactiveRuling,
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
    TraceDirection,
    TraceEvidence,
    TraceItem,
    TraceLink,
    TraceRequest,
)
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    LoadedRecord,
    RawRecord,
    StoreSnapshot,
)
from untaped_orchestration.application.validation import _graph_state, validate_snapshot
from untaped_orchestration.domain.curation import StoreCurationContext, curation_queue
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.graph import (
    DecisionRef,
    DecisionState,
    GraphState,
    TaskRef,
    decision_state,
    readiness,
)
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    LinkRelation,
    Revision,
    TaskStage,
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


def _store_revisions(snapshot: FederatedSnapshot, local: bool) -> tuple[tuple[str, Revision], ...]:
    values = []
    for store in _selected(snapshot, local):
        if store.store is not None:
            values.append((store.store.id.root, store.store_revision))
    return tuple(sorted(values))


def _validated_scope(snapshot: FederatedSnapshot, local: bool) -> FederatedSnapshot:
    if not local:
        return snapshot
    return FederatedSnapshot(snapshot.selected, (snapshot.selected,), Completeness())


def _query_diagnostics(snapshot: FederatedSnapshot, local: bool) -> tuple[Diagnostic, ...]:
    return validate_snapshot(_validated_scope(snapshot, local), require_children=False)


def _query_complete(snapshot: FederatedSnapshot, local: bool) -> bool:
    diagnostics = _query_diagnostics(snapshot, local)
    federation_complete = local or snapshot.completeness.complete
    return federation_complete and not any(value.severity == "error" for value in diagnostics)


def _active_records(
    snapshot: FederatedSnapshot, local: bool
) -> Iterable[tuple[StoreSnapshot, LoadedRecord]]:
    for store in _selected(snapshot, local):
        if store.store is None:
            continue
        for record in store.records:
            if isinstance(record.metadata, (ActiveTask, Decision)):
                yield store, record


def _row(
    store_id: StoreId,
    record: LoadedRecord,
    graph: GraphState | None = None,
) -> ItemRow:
    metadata = record.metadata
    state = None
    if isinstance(metadata, Decision) and graph is not None:
        state = decision_state(DecisionRef(store_id, metadata.id), graph)
    return ItemRow(
        metadata.id,
        metadata.kind,
        metadata.title,
        store_id,
        record.path.as_posix(),
        record.revision,
        metadata.stage if isinstance(metadata, ActiveTask) else None,
        state,
        tuple(value.root for value in metadata.waiting_on)
        if isinstance(metadata, (ActiveTask, ArchivedTask))
        else (),
    )


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


def _truncate_utf8(raw: bytes, limit: int) -> tuple[bytes, bool]:
    if len(raw) <= limit:
        return raw, False
    end = limit
    while end and raw[end : end + 1] and raw[end] & 0b1100_0000 == 0b1000_0000:
        end -= 1
    return raw[:end], True


class QueryService:
    def __init__(self, scope: QueryScope, reader: StoreReader, clock: Clock) -> None:
        self._scope = scope
        self._reader = reader
        self._clock = clock

    def _load(self, local: bool) -> FederatedSnapshot:
        return (self._scope.local if local else self._scope.recursive)()

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
        diagnostics = _query_diagnostics(snapshot, local)
        return QueryResult(
            data,
            _query_complete(snapshot, local),
            truncated,
            diagnostics,
            _store_revisions(snapshot, local),
            revisions,
            retained,
        )

    def list(self, request: ListRequest) -> QueryResult[tuple[ItemRow, ...]]:
        limit = _limit(request.limit)
        snapshot = self._load(request.local)
        graph = _graph_state(snapshot)
        rows = []
        for store, record in _active_records(snapshot, request.local):
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
            row = _row(store.store.id, record, graph)
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
        detail = ItemDetail(
            _row(store.store.id, record, _graph_state(snapshot)),
            record.metadata,
            body,
            store.store_revision,
        )
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
        graph = _graph_state(snapshot)
        needle = request.query.casefold()
        hits: list[SearchHit] = []
        total = 0
        for store in _selected(snapshot, request.local):
            if store.store is None:
                continue
            for record in store.records:
                archived = isinstance(record.metadata, ArchivedTask)
                if archived != request.history:
                    continue
                body = self._reader.read_item_body(store.location, record.path)
                haystack = f"{record.metadata.title}\n".encode() + body
                if needle not in haystack.decode("utf-8", errors="replace").casefold():
                    continue
                total += 1
                if len(hits) < limit:
                    text = body.decode("utf-8", errors="replace")
                    hits.append(SearchHit(_row(store.store.id, record, graph), text[:512]))
        return self._result(
            snapshot, tuple(hits), local=request.local, truncated=total > limit, retained=len(hits)
        )

    def next(self, request: NextRequest) -> QueryResult[tuple[NextItem, ...]]:
        limit = _limit(request.limit)
        snapshot = self._load(request.local)
        if not _query_complete(snapshot, request.local):
            raise QueryIncompleteError(_query_diagnostics(snapshot, request.local))
        graph = _graph_state(snapshot)
        candidates = []
        by_key = {}
        for store, record in _active_records(snapshot, request.local):
            if isinstance(record.metadata, ActiveTask):
                assert store.store is not None
                key = (store.store.id, record.metadata.id)
                by_key[key] = record
                if readiness(TaskRef(*key), graph).ready:
                    candidates.append(TaskOrderItem(store.store.id, record.metadata))
        ordered = sort_tasks(candidates)
        contexts = tuple(
            StoreCurationContext(store.store.id, store.store.timezone, store.store.curation)
            for store in _selected(snapshot, request.local)
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
                    _row(value.store_id, record, graph),
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

    def history(self, request: HistoryRequest) -> QueryResult[tuple[ItemRow, ...] | ItemDetail]:
        limit = _limit(request.limit)
        snapshot = self._load(request.local)
        values = [
            (store, record)
            for store in _selected(snapshot, request.local)
            for record in store.records
            if isinstance(record.metadata, ArchivedTask)
            and (
                request.query is None
                or request.query.casefold() in record.metadata.title.casefold()
            )
        ]
        if request.item_id is not None:
            matches = [
                (store, record) for store, record in values if record.metadata.id == request.item_id
            ]
            if len(matches) != 1:
                raise ValueError("archived item does not resolve uniquely")
            store, record = matches[0]
            assert store.store is not None
            body = self._reader.read_item_body(store.location, record.path)
            return self._result(
                snapshot,
                ItemDetail(
                    _row(store.store.id, record), record.metadata, body, store.store_revision
                ),
                local=request.local,
                revisions=((record.metadata.id.root, record.revision),),
                retained=1,
            )
        rows = sorted(
            (_row(store.store.id, record) for store, record in values if store.store is not None),
            key=lambda row: (row.store_id.root, row.path),
        )
        return self._result(
            snapshot, tuple(rows[:limit]), local=request.local, truncated=len(rows) > limit
        )

    def trace(self, request: TraceRequest) -> QueryResult[TraceData]:
        limit = _limit(request.limit)
        snapshot = self._load(request.local)
        stores = _selected(snapshot, request.local)
        index = {
            (store.store.id.root, record.metadata.id.root): (store, record)
            for store in stores
            if store.store is not None
            for record in store.records
        }
        root_store, root_record = _find(snapshot, request.item_id, request.local)
        assert root_store.store is not None
        root = QualifiedItem(root_store.store.id, root_record.metadata.id)
        pending = deque([(root, 0)])
        seen = {(root.store_id.root, root.item_id.root)}
        items: list[TraceItem] = []
        links: list[TraceLink] = []
        evidence: list[TraceEvidence] = []
        truncated = False
        while pending:
            current, depth = pending.popleft()
            pair = index.get((current.store_id.root, current.item_id.root))
            if pair is None:
                continue
            _, record = pair
            evidence.extend(
                TraceEvidence(current, value.relation.value, value.reference.root, depth)
                for value in record.metadata.evidence
            )
            edges: list[tuple[QualifiedItem, QualifiedItem, LinkRelation]] = []
            if request.direction in {TraceDirection.OUTGOING, TraceDirection.BOTH}:
                edges.extend(
                    (current, QualifiedItem(link.target_store_id, link.target), link.relation)
                    for link in record.metadata.links
                )
            if request.direction in {TraceDirection.INCOMING, TraceDirection.BOTH}:
                for (source_store, _), (_, candidate) in index.items():
                    for link in candidate.metadata.links:
                        if (
                            link.target_store_id == current.store_id
                            and link.target == current.item_id
                        ):
                            edges.append(
                                (
                                    QualifiedItem(StoreId(source_store), candidate.metadata.id),
                                    current,
                                    link.relation,
                                )
                            )
            edges.sort(
                key=lambda value: (
                    value[2].value,
                    value[1].store_id.root,
                    value[1].item_id.root,
                    value[0].item_id.root,
                )
            )
            for source, target, relation in edges:
                neighbor = target if source == current else source
                key = (neighbor.store_id.root, neighbor.item_id.root)
                links.append(TraceLink(source, target, relation, depth + 1))
                if key in seen:
                    continue
                if len(items) >= limit:
                    truncated = True
                    continue
                seen.add(key)
                items.append(TraceItem(neighbor, depth + 1))
                pending.append((neighbor, depth + 1))
        data = TraceData(root, tuple(items), tuple(links), tuple(evidence))
        return self._result(snapshot, data, local=request.local, truncated=truncated)

    def brief(self, request: BriefRequest) -> QueryResult[BriefData]:
        del request
        snapshot = self._load(False)
        selected = snapshot.selected
        config = selected.store
        if config is None:
            raise ValueError("brief requires a valid selected store configuration")
        graph = _graph_state(snapshot)
        query_complete = _query_complete(snapshot, False)
        local_decisions = {
            record.metadata.id: record
            for record in selected.records
            if isinstance(record.metadata, Decision)
        }
        pinned: list[BriefDecision] = []
        inactive: list[InactiveRuling] = []
        item_revisions: list[tuple[str, Revision]] = []
        remaining = config.brief.max_total_body_bytes
        truncated = False
        for item_id in config.brief.pinned_decisions[:10]:
            record = local_decisions.get(item_id)
            if record is None:
                inactive.append(InactiveRuling(item_id, None))
                continue
            state = decision_state(DecisionRef(config.id, item_id), graph)
            item_revisions.append((item_id.root, record.revision))
            if state is not DecisionState.ACTIVE:
                inactive.append(InactiveRuling(item_id, state))
                continue
            body = self._reader.read_item_body(selected.location, record.path)
            body, cut = _truncate_utf8(
                body,
                min(config.brief.max_decision_body_bytes, remaining),
            )
            truncated |= cut
            remaining -= len(body)
            pinned.append(BriefDecision(item_id, record.metadata.title, record.revision, body))

        task_pairs: list[tuple[StoreSnapshot, LoadedRecord]] = []
        order_items: list[TaskOrderItem] = []
        for store, record in _active_records(snapshot, False):
            if not isinstance(record.metadata, ActiveTask):
                continue
            assert store.store is not None
            task_pairs.append((store, record))
            order_items.append(TaskOrderItem(store.store.id, record.metadata))
        ordered = sort_tasks(order_items)
        record_index = {
            (store.store.id, record.metadata.id): record
            for store, record in task_pairs
            if store.store is not None
        }
        rows = [
            _row(value.store_id, record_index[(value.store_id, value.task.id)], graph)
            for value in ordered
        ]
        in_progress = next((row for row in rows if row.stage is TaskStage.IN_PROGRESS), None)
        ready_rows: list[ItemRow] = []
        blocker_rows: list[ItemRow] = []
        for value, row in zip(ordered, rows, strict=True):
            status = readiness(TaskRef(value.store_id, value.task.id), graph)
            if status.ready and query_complete:
                ready_rows.append(row)
            elif not status.ready:
                blocker_rows.append(row)

        contexts = tuple(
            StoreCurationContext(store.store.id, store.store.timezone, store.store.curation)
            for store in snapshot.stores
            if store.store is not None
        )
        due = curation_queue(
            graph,
            now=UtcTimestamp.from_datetime(self._clock.now()),
            contexts=contexts,
        )
        cap = config.brief.max_rows_per_section
        diagnostics = _query_diagnostics(snapshot, False)
        missing = snapshot.completeness.missing_store_ids
        data = BriefData(
            config.id,
            selected.store_revision,
            selected.registry_revision,
            tuple(item_revisions),
            tuple(pinned),
            tuple(inactive[:cap]),
            in_progress,
            tuple(ready_rows[:cap]),
            tuple(blocker_rows[:cap]),
            tuple(due[:cap]),
            diagnostics[:cap],
            missing[:cap],
            len(ready_rows),
            len(blocker_rows),
            len(due),
            len(diagnostics),
            len(missing),
            query_complete and bool(ready_rows),
        )
        truncated |= any(
            value > cap
            for value in (
                len(ready_rows),
                len(blocker_rows),
                len(due),
                len(diagnostics),
                len(missing),
            )
        )
        return self._result(
            snapshot,
            data,
            local=False,
            truncated=truncated,
            revisions=tuple(item_revisions),
            retained=len(pinned),
        )
