"""Structural scale proof; local baseline was about 0.4 s and 3 MiB peak on Apple Silicon.

The measurements are documentary only. Assertions intentionally concern retained objects and
body-read structure, never wall time or a machine-specific memory ceiling.
"""

from __future__ import annotations

import tracemalloc
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from tests.unit.application.test_federation import _registry, _store
from untaped_orchestration.application.curation import CurateNextRequest, CurationService
from untaped_orchestration.application.item_support import (
    MutationExecutionScope,
    MutationScope,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.ports import CanonicalFormatter
from untaped_orchestration.application.queries import (
    BriefRequest,
    ListRequest,
    NextRequest,
    QueryScope,
    QueryService,
    SearchRequest,
    ShowRequest,
)
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    LoadedRecord,
    StoreLocation,
    StoreSnapshot,
)
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import ActiveTask, Decision, Revision


def _revision(value: int) -> Revision:
    return Revision(f"sha256:{value:064x}")


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, tzinfo=UTC)


class Bodies:
    def __init__(self, values: dict[tuple[str, PurePosixPath], bytes]) -> None:
        self.values = values
        self.reads: list[tuple[str, PurePosixPath]] = []

    def read_item_body(self, location, path):
        key = (location.real_root.as_posix(), path)
        self.reads.append(key)
        return self.values[key]


def _fixture() -> tuple[QueryScope, Bodies, TaskId]:
    stores = []
    bodies = {}
    first_task = None
    pins = tuple(DecisionId(f"dec_019f00000000700080000000{index:08x}") for index in range(10))
    serial = 1
    for store_index in range(11):
        store_id = StoreId(f"sto_019f00000000700080000000{store_index:08x}")
        config = _store(store_id.root)
        if store_index == 0:
            config = config.model_copy(
                update={"brief": config.brief.model_copy(update={"pinned_decisions": pins})}
            )
        location = StoreLocation(Path(f"/stores/{store_index}"), Path(f"/stores/{store_index}"))
        records = []
        count = 90
        for item_index in range(count):
            task_id = TaskId(f"tsk_019f0000000070008000{serial:012x}")
            first_task = first_task or task_id
            metadata = ActiveTask.model_validate(
                {
                    "schema": "untaped.orchestration.task/v1",
                    "id": task_id.root,
                    "kind": "task",
                    "title": f"Scale task {serial}",
                    "created_at": "2026-07-01T00:00:00.000Z",
                    "tags": [],
                    "stage": "planned",
                    "priority": "normal",
                    "rank": (item_index + 1) * 1000,
                    "waiting_on": [],
                    "links": [],
                    "evidence": [],
                }
            )
            path = PurePosixPath(f"tasks/{task_id.root}-scale.md")
            records.append(LoadedRecord(path, _revision(serial), metadata, None))
            bodies[(location.real_root.as_posix(), path)] = b"maximum searchable body " * 100
            serial += 1
        if store_index == 0:
            for pin_index, decision_id in enumerate(pins):
                metadata = Decision.model_validate(
                    {
                        "schema": "untaped.orchestration.decision/v1",
                        "id": decision_id.root,
                        "kind": "decision",
                        "title": f"Pinned {pin_index}",
                        "created_at": "2026-07-01T00:00:00.000Z",
                        "tags": [],
                        "links": [],
                        "evidence": [],
                    }
                )
                path = PurePosixPath(f"decisions/{decision_id.root}-pinned.md")
                records.append(LoadedRecord(path, _revision(serial), metadata, None))
                bodies[(location.real_root.as_posix(), path)] = "é".encode() * 3000
                serial += 1
        stores.append(
            StoreSnapshot(
                location,
                config,
                _registry(store_id.root),
                tuple(records),
                (),
                (),
                _revision(serial),
                _revision(serial + 1),
                _revision(serial + 2),
            )
        )
    federation = FederatedSnapshot(stores[0], tuple(stores), Completeness())
    reader = Bodies(bodies)
    assert first_task is not None
    return (
        QueryScope(
            lambda: federation, lambda: FederatedSnapshot(stores[0], (stores[0],), Completeness())
        ),
        reader,
        first_task,
    )


def test_11_store_1000_item_queries_are_structurally_bounded() -> None:
    scope, bodies, first_task = _fixture()
    service = QueryService(scope, bodies, Clock())

    assert len(service.list(ListRequest(limit=200)).data) == 200
    assert bodies.reads == []
    service.next(NextRequest(limit=200))
    assert bodies.reads == []
    recursive = scope.recursive()
    execution = MutationExecutionScope((), recursive.selected.location, scope.recursive)
    CurationService(
        cast(MutationExecutor, object()),
        cast(CanonicalFormatter, object()),
        Clock(),
        MutationScope(execution, execution),
    ).next(CurateNextRequest())
    assert bodies.reads == []

    shown = service.show(ShowRequest(first_task))
    assert shown.retained_bodies == 1
    assert len(bodies.reads) == 1

    bodies.reads.clear()
    brief = service.brief(BriefRequest())
    assert brief.retained_bodies <= 10
    assert len(bodies.reads) <= 10

    bodies.reads.clear()
    tracemalloc.start()
    searched = service.search(SearchRequest("searchable", limit=5))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert searched.retained_bodies <= 5
    assert len(searched.data) == 5
    assert all(len(hit.snippet) <= 512 for hit in searched.data)
    assert sum(len(hit.snippet) for hit in searched.data) <= 5 * 512
    assert peak > 0
