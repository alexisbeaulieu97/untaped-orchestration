from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from tests.builders import STORE_ID
from tests.unit.application.test_federation import _registry, _store
from untaped_orchestration.application.queries import BriefRequest, QueryScope, QueryService
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompleteStore,
    LoadedRecord,
    StoreLocation,
    StoreSnapshot,
)
from untaped_orchestration.domain.diagnostics import Diagnostic
from untaped_orchestration.domain.ids import DecisionId, StoreId
from untaped_orchestration.domain.models import Decision, Revision


def _revision(seed: str) -> Revision:
    return Revision(f"sha256:{seed * 64}")


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, tzinfo=UTC)


class Bodies:
    def __init__(self, values: dict[PurePosixPath, bytes]) -> None:
        self.values = values
        self.reads: list[PurePosixPath] = []

    def read_item_body(self, location, path):
        del location
        self.reads.append(path)
        return self.values[path]


def test_brief_reads_at_most_ten_pins_and_applies_utf8_and_aggregate_body_caps() -> None:
    ids = tuple(DecisionId(f"dec_019f00000000700080000000000000{index:02x}") for index in range(10))
    config = _store(STORE_ID)
    config = config.model_copy(
        update={
            "brief": config.brief.model_copy(
                update={
                    "pinned_decisions": ids,
                    "max_decision_body_bytes": 4096,
                    "max_total_body_bytes": 16384,
                }
            )
        }
    )
    records = []
    values = {}
    for index, item_id in enumerate(ids):
        metadata = Decision.model_validate(
            {
                "schema": "untaped.orchestration.decision/v1",
                "id": item_id.root,
                "kind": "decision",
                "title": f"Ruling {index}",
                "created_at": "2026-07-01T00:00:00.000Z",
                "tags": [],
                "links": [],
                "evidence": [],
            }
        )
        path = PurePosixPath(f"decisions/{item_id.root}-ruling.md")
        records.append(LoadedRecord(path, _revision(format(index, "x")), metadata, None))
        values[path] = ("é" * 3000).encode()
    location = StoreLocation(Path("/store"), Path("/store"))
    snapshot = StoreSnapshot(
        location,
        config,
        _registry(STORE_ID),
        tuple(records),
        (),
        (),
        _revision("a"),
        _revision("b"),
        _revision("c"),
    )
    federation = FederatedSnapshot(snapshot, (snapshot,), Completeness())
    bodies = Bodies(values)

    result = QueryService(
        QueryScope(lambda: federation, lambda: federation), bodies, Clock()
    ).brief(BriefRequest())

    assert len(result.data.pinned_decisions) == 10
    assert len(bodies.reads) == 10
    assert all(len(value.body) <= 4096 for value in result.data.pinned_decisions)
    assert sum(len(value.body) for value in result.data.pinned_decisions) <= 16384
    assert all(value.body.decode("utf-8") for value in result.data.pinned_decisions if value.body)
    assert result.truncated
    assert result.data.store_revision == _revision("a")
    assert len(result.data.item_revisions) == 10


def test_incomplete_brief_names_missing_stores_and_never_emits_global_ready_rows() -> None:
    from tests.unit.application.test_queries import _scope

    scope, bodies = _scope(incomplete=True)
    result = QueryService(scope, bodies, Clock()).brief(BriefRequest())

    assert not result.complete
    assert result.data.ready == ()
    assert result.data.missing_store_ids == ("sto_019f0000000070008000000000000099",)
    assert result.data.missing_store_count == 1
    assert result.data.diagnostic_count == 1
    assert result.data.globally_ready is False


def test_brief_caps_diagnostics_and_missing_ids_but_preserves_full_counts() -> None:
    from tests.unit.application.test_queries import _scope

    scope, bodies = _scope()
    base = scope.local()
    entries = tuple(
        IncompleteStore(
            StoreId(f"sto_019f00000000700080000000{index:08x}"),
            "missing",
            Diagnostic(
                code="ORC005",
                severity="warning",
                path=f"registry-{index}.toml",
                field="children",
                message=f"missing {index}",
                hint="restore",
            ),
        )
        for index in range(12)
    )
    federation = FederatedSnapshot(base.selected, base.stores, Completeness(entries))

    result = QueryService(QueryScope(lambda: federation, lambda: base), bodies, Clock()).brief(
        BriefRequest()
    )

    assert len(result.data.diagnostics) == 10
    assert len(result.data.missing_store_ids) == 10
    assert result.data.diagnostic_count == 12
    assert result.data.missing_store_count == 12
    assert result.truncated


def test_brief_names_inactive_pinned_ruling_without_loading_its_body() -> None:
    from tests.unit.application.test_queries import _scope

    scope, bodies = _scope()
    selected = scope.local().selected
    decision_record = next(
        record for record in selected.records if isinstance(record.metadata, Decision)
    )
    retired = decision_record.metadata.model_copy(
        update={
            "retired_at": decision_record.metadata.created_at,
            "retire_note": "Mechanism removed",
        }
    )
    records = tuple(
        record.__class__(record.path, record.revision, retired, record.body)
        if record is decision_record
        else record
        for record in selected.records
    )
    assert selected.store is not None
    config = selected.store.model_copy(
        update={
            "brief": selected.store.brief.model_copy(update={"pinned_decisions": (retired.id,)})
        }
    )
    selected = selected.__class__(
        selected.location,
        config,
        selected.registry,
        records,
        (),
        (),
        selected.store_revision,
        selected.registry_revision,
        selected.store_config_revision,
    )
    federation = FederatedSnapshot(selected, (selected,), Completeness())

    result = QueryService(
        QueryScope(lambda: federation, lambda: federation), bodies, Clock()
    ).brief(BriefRequest())

    assert result.data.pinned_decisions == ()
    assert result.data.inactive_rulings[0].item_id == retired.id
    assert result.data.inactive_rulings[0].state.value == "retired"
    assert bodies.reads == []
