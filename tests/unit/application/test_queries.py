from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID, STORE_ID, TASK_ID
from untaped_orchestration.application.queries import (
    BriefRequest,
    HistoryRequest,
    ListRequest,
    NextRequest,
    QueryScope,
    QueryService,
    RawShowRequest,
    SearchRequest,
    ShowRequest,
    TraceDirection,
    TraceRequest,
)
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompleteStore,
    LoadedRecord,
    RawRecord,
    RawReference,
    StoreLocation,
    StoreSnapshot,
)
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    Link,
    LinkRelation,
    Revision,
)
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository


def _revision(seed: str = "a") -> Revision:
    return Revision(f"sha256:{seed * 64}")


def _task(*, waiting: str = "alexis") -> ActiveTask:
    return ActiveTask.model_validate(
        {
            "schema": "untaped.orchestration.task/v1",
            "id": TASK_ID,
            "kind": "task",
            "title": "Need orchestration",
            "created_at": "2026-07-01T00:00:00.000Z",
            "tags": ["orchestration"],
            "stage": "inbox",
            "priority": "high",
            "rank": 1000,
            "waiting_on": [waiting],
            "links": [],
            "evidence": [],
        }
    )


def _decision() -> Decision:
    return Decision.model_validate(
        {
            "schema": "untaped.orchestration.decision/v1",
            "id": DECISION_ID,
            "kind": "decision",
            "title": "Use typed stores",
            "created_at": "2026-07-01T00:00:00.000Z",
            "tags": ["architecture"],
            "links": [],
            "evidence": [],
        }
    )


class BodyReader:
    def __init__(self, bodies: dict[PurePosixPath, bytes]) -> None:
        self.bodies = bodies
        self.reads: list[PurePosixPath] = []

    def read_item_body(self, location: StoreLocation, path: PurePosixPath) -> bytes:
        del location
        self.reads.append(path)
        return self.bodies[path]

    def read_raw(self, location: StoreLocation, path: PurePosixPath):
        del location
        return RawRecord(path, _revision("9"), 3, b"bad")


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, tzinfo=UTC)


def _scope(*, incomplete: bool = False) -> tuple[QueryScope, BodyReader]:
    location = StoreLocation(Path("/store"), Path("/store"))
    records = (
        LoadedRecord(PurePosixPath(f"tasks/{TASK_ID}-task.md"), _revision("1"), _task(), None),
        LoadedRecord(
            PurePosixPath(f"decisions/{DECISION_ID}-decision.md"),
            _revision("2"),
            _decision(),
            None,
        ),
    )
    config = FilesystemStoreRepository  # keep the test independent of a filesystem fixture
    del config
    from tests.unit.application.test_federation import _registry, _store

    snapshot = StoreSnapshot(
        location,
        _store(STORE_ID),
        _registry(STORE_ID),
        records,
        (),
        (),
        _revision("3"),
        _revision("4"),
        _revision("5"),
    )
    completeness = Completeness()
    if incomplete:
        completeness = Completeness(
            (
                IncompleteStore(
                    StoreId("sto_019f0000000070008000000000000099"),
                    "missing",
                    __import__(
                        "untaped_orchestration.domain.diagnostics", fromlist=["Diagnostic"]
                    ).Diagnostic(
                        code="ORC005",
                        severity="warning",
                        path="registry.toml",
                        field="children",
                        message="missing",
                        hint="restore",
                    ),
                ),
            )
        )
    federation = FederatedSnapshot(snapshot, (snapshot,), completeness)
    reader = BodyReader({records[0].path: b"task body", records[1].path: "décision body".encode()})
    return QueryScope(lambda: federation, lambda: federation), reader


def test_list_filters_waiting_on_deterministically_without_loading_bodies() -> None:
    scope, reader = _scope()
    service = QueryService(scope, reader, Clock())

    result = service.list(ListRequest(waiting_on="alexis"))

    assert [row.item_id.root for row in result.data] == [TASK_ID]
    assert result.complete and not result.truncated
    assert reader.reads == []


def test_show_loads_exactly_one_body_and_raw_is_local_only() -> None:
    scope, reader = _scope()
    service = QueryService(scope, reader, Clock())

    result = service.show(ShowRequest(TaskId(TASK_ID)))

    assert result.data.body == b"task body"
    assert result.data.revision == _revision("1")
    assert reader.reads == [PurePosixPath(f"tasks/{TASK_ID}-task.md")]


def test_raw_show_uses_selected_filename_index_without_parsing_or_recursing() -> None:
    scope, reader = _scope(incomplete=True)
    selected = scope.local().selected
    reference = RawReference(
        PurePosixPath(f"tasks/{TASK_ID}-broken.md"),
        _revision("9"),
        3,
    )
    selected = selected.__class__(
        selected.location,
        selected.store,
        selected.registry,
        selected.records,
        selected.load_diagnostics,
        (reference,),
        selected.store_revision,
        selected.registry_revision,
        selected.store_config_revision,
    )
    local = FederatedSnapshot(selected, (selected,), Completeness())
    service = QueryService(QueryScope(scope.recursive, lambda: local), reader, Clock())

    result = service.show_raw(RawShowRequest(TaskId(TASK_ID)))

    assert result.complete
    assert result.data.content == b"bad"
    assert reader.reads == []


def test_streaming_search_retains_limit_and_reports_truncation() -> None:
    scope, reader = _scope()
    result = QueryService(scope, reader, Clock()).search(SearchRequest("body", limit=1))

    assert len(result.data) == 1
    assert result.truncated
    assert result.retained_bodies <= 1
    assert len(reader.reads) == 2


def test_partial_list_is_safe_but_next_fails_closed_unless_local() -> None:
    scope, reader = _scope(incomplete=True)
    service = QueryService(scope, reader, Clock())
    assert not service.list(ListRequest()).complete

    with pytest.raises(ValueError, match="complete"):
        service.next(NextRequest())
    assert service.next(NextRequest(local=True)).data == ()
    assert reader.reads == []


def test_next_reports_mutation_context_without_loading_bodies() -> None:
    scope, reader = _scope()
    selected = scope.local().selected
    records = []
    for record in selected.records:
        if isinstance(record.metadata, ActiveTask):
            metadata = record.metadata.model_copy(
                update={
                    "waiting_on": (),
                    "links": (
                        Link(
                            relation=LinkRelation.GOVERNED_BY,
                            target_store_id=StoreId(STORE_ID),
                            target=DecisionId(DECISION_ID),
                        ),
                    ),
                }
            )
            record = record.__class__(record.path, record.revision, metadata, None)
        records.append(record)
    selected = selected.__class__(
        selected.location,
        selected.store,
        selected.registry,
        tuple(records),
        (),
        (),
        selected.store_revision,
        selected.registry_revision,
        selected.store_config_revision,
    )
    federation = FederatedSnapshot(selected, (selected,), Completeness())

    result = QueryService(QueryScope(lambda: federation, lambda: federation), reader, Clock()).next(
        NextRequest()
    )

    assert result.data[0].row.item_id == TaskId(TASK_ID)
    assert result.data[0].ancestor_path == ()
    assert result.data[0].unblocks_count == 0
    assert result.data[0].due
    assert result.data[0].governing_decisions[0].item_id == DecisionId(DECISION_ID)
    assert result.data[0].evidence_summary == ()
    assert reader.reads == []


def test_invalid_canonical_store_makes_partial_reads_incomplete_and_readiness_fail_closed() -> None:
    scope, reader = _scope()
    selected = scope.local().selected
    diagnostic = __import__(
        "untaped_orchestration.domain.diagnostics", fromlist=["Diagnostic"]
    ).Diagnostic(
        code="ORC001",
        severity="error",
        path="tasks/broken.md",
        field="",
        message="broken",
        hint="repair",
    )
    selected = selected.__class__(
        selected.location,
        selected.store,
        selected.registry,
        selected.records,
        (diagnostic,),
        selected.raw_index,
        selected.store_revision,
        selected.registry_revision,
        selected.store_config_revision,
    )
    federation = FederatedSnapshot(selected, (selected,), Completeness())
    service = QueryService(QueryScope(lambda: federation, lambda: federation), reader, Clock())

    assert not service.list(ListRequest()).complete
    brief = service.brief(BriefRequest())
    assert [value.code for value in brief.data.diagnostics] == ["ORC001"]
    with pytest.raises(ValueError, match="complete"):
        service.next(NextRequest())


def test_limit_contract_and_history_and_trace_are_typed() -> None:
    scope, reader = _scope()
    service = QueryService(scope, reader, Clock())
    with pytest.raises(ValueError, match=r"1\.\.200"):
        service.list(ListRequest(limit=201))
    assert service.history(HistoryRequest()).data == ()
    trace = service.trace(TraceRequest(DecisionId(DECISION_ID), limit=50))
    assert trace.data.root.item_id.root == DECISION_ID
    assert trace.data.items == ()
    assert reader.reads == []


def test_history_show_loads_one_archived_body() -> None:
    scope, reader = _scope()
    selected = scope.local().selected
    active = _task(waiting="alexis")
    archived_values = active.model_dump(by_alias=True)
    archived_values.pop("stage")
    archived_values.update(
        {
            "closed_from": "inbox",
            "outcome": "declined",
            "closed_at": "2026-07-10T00:00:00.000Z",
            "close_note": "No longer needed",
        }
    )
    archived = ArchivedTask.model_validate(archived_values)
    path = PurePosixPath(f"archive/tasks/{TASK_ID}-task.md")
    record = LoadedRecord(path, _revision("8"), archived, None)
    selected = selected.__class__(
        selected.location,
        selected.store,
        selected.registry,
        (record,),
        (),
        (),
        selected.store_revision,
        selected.registry_revision,
        selected.store_config_revision,
    )
    federation = FederatedSnapshot(selected, (selected,), Completeness())
    reader.bodies[path] = b"archive body"

    result = QueryService(
        QueryScope(lambda: federation, lambda: federation), reader, Clock()
    ).history(HistoryRequest(item_id=TaskId(TASK_ID)))

    assert result.data.body == b"archive body"
    assert result.retained_bodies == 1


def test_trace_is_cycle_safe_breadth_first_directional_and_limit_bounded() -> None:
    scope, reader = _scope()
    selected = scope.local().selected
    task_ids = tuple(TaskId(f"tsk_019f00000000700080000000000000{index:02x}") for index in range(3))
    records = []
    for index, item_id in enumerate(task_ids):
        following = task_ids[(index + 1) % len(task_ids)]
        metadata = _task(waiting="alexis").model_copy(
            update={
                "id": item_id,
                "rank": (index + 1) * 1000,
                "links": (
                    Link(
                        relation=LinkRelation.DEPENDS_ON,
                        target_store_id=StoreId(STORE_ID),
                        target=following,
                    ),
                ),
            }
        )
        path = PurePosixPath(f"tasks/{item_id.root}-task.md")
        records.append(LoadedRecord(path, _revision(str(index + 1)), metadata, None))
    selected = selected.__class__(
        selected.location,
        selected.store,
        selected.registry,
        tuple(records),
        (),
        (),
        selected.store_revision,
        selected.registry_revision,
        selected.store_config_revision,
    )
    federation = FederatedSnapshot(selected, (selected,), Completeness())

    result = QueryService(
        QueryScope(lambda: federation, lambda: federation), reader, Clock()
    ).trace(TraceRequest(task_ids[0], direction=TraceDirection.OUTGOING, limit=1))

    assert [value.item.item_id for value in result.data.items] == [task_ids[1]]
    assert [value.depth for value in result.data.items] == [1]
    assert result.truncated
    assert reader.reads == []
