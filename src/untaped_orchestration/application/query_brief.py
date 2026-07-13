from __future__ import annotations

from dataclasses import dataclass

from untaped_orchestration.application.ports import StoreReader
from untaped_orchestration.application.query_models import (
    BriefData,
    BriefDecision,
    InactiveRuling,
    ItemRow,
)
from untaped_orchestration.application.query_projection import (
    SafeProjection,
    active_records,
    row_for,
    safe_decision_state,
    safe_readiness,
    safe_task_order,
)
from untaped_orchestration.application.results import LoadedRecord
from untaped_orchestration.domain.diagnostics import DiagnosticError, expected_diagnostic
from untaped_orchestration.domain.graph import DecisionState
from untaped_orchestration.domain.ids import DecisionId, StoreId
from untaped_orchestration.domain.models import (
    ActiveTask,
    Decision,
    LinkRelation,
    Revision,
    TaskStage,
)
from untaped_orchestration.domain.ordering import TaskOrderItem


@dataclass(frozen=True, slots=True)
class _Pins:
    decisions: tuple[BriefDecision, ...]
    inactive: tuple[InactiveRuling, ...]
    revisions: tuple[tuple[str, Revision], ...]
    truncated: bool


def _truncate(raw: bytes, limit: int) -> tuple[bytes, bool]:
    if len(raw) <= limit:
        return raw, False
    end = limit
    while end and raw[end] & 0b1100_0000 == 0b1000_0000:
        end -= 1
    return raw[:end], True


def _indexes(
    projection: SafeProjection,
) -> tuple[
    dict[DecisionId, list[LoadedRecord]], dict[tuple[StoreId, DecisionId], list[LoadedRecord]]
]:
    selected = projection.snapshot.selected
    local: dict[DecisionId, list[LoadedRecord]] = {}
    qualified: dict[tuple[StoreId, DecisionId], list[LoadedRecord]] = {}
    for store in projection.snapshot.stores:
        if store.store is None:
            continue
        for record in store.records:
            if not isinstance(record.metadata, Decision):
                continue
            qualified.setdefault((store.store.id, record.metadata.id), []).append(record)
            if store.location.real_root == selected.location.real_root:
                local.setdefault(record.metadata.id, []).append(record)
    return local, qualified


def _pins(
    projection: SafeProjection,
    reader: StoreReader | None,
    local: dict[DecisionId, list[LoadedRecord]],
) -> _Pins:
    selected = projection.snapshot.selected
    config = selected.store
    assert config is not None
    decisions: list[BriefDecision] = []
    inactive: list[InactiveRuling] = []
    revisions: list[tuple[str, Revision]] = []
    remaining = config.brief.max_total_body_bytes
    truncated = False
    for item_id in config.brief.pinned_decisions:
        matches = local.get(item_id, ())
        if len(matches) != 1:
            inactive.append(InactiveRuling(config.id, item_id, None))
            continue
        record = matches[0]
        state = safe_decision_state(projection.graph, config.id, item_id)
        revisions.append((item_id.root, record.revision))
        if state is not DecisionState.ACTIVE:
            inactive.append(InactiveRuling(config.id, item_id, state))
            continue
        if reader is None:
            truncated = True
            continue
        body = reader.read_item_body(selected.location, record.path)
        body, cut = _truncate(body, min(config.brief.max_decision_body_bytes, remaining))
        truncated |= cut
        remaining -= len(body)
        decisions.append(BriefDecision(item_id, record.metadata.title, record.revision, body))
    return _Pins(tuple(decisions), tuple(inactive), tuple(revisions), truncated)


def _governed(
    projection: SafeProjection, qualified: dict[tuple[StoreId, DecisionId], list[LoadedRecord]]
) -> tuple[InactiveRuling, ...]:
    values: dict[tuple[str, str], InactiveRuling] = {}
    for _, record in active_records(projection):
        if not isinstance(record.metadata, ActiveTask):
            continue
        for link in record.metadata.links:
            if (
                link.relation is not LinkRelation.GOVERNED_BY
                or not isinstance(link.target, DecisionId)
                or len(qualified.get((link.target_store_id, link.target), ())) != 1
            ):
                continue
            state = safe_decision_state(projection.graph, link.target_store_id, link.target)
            if state is not None and state is not DecisionState.ACTIVE:
                key = (link.target_store_id.root, link.target.root)
                values[key] = InactiveRuling(link.target_store_id, link.target, state)
    return tuple(values[key] for key in sorted(values))


def _tasks(
    projection: SafeProjection,
) -> tuple[ItemRow | None, tuple[ItemRow, ...], tuple[ItemRow, ...]]:
    order: list[TaskOrderItem] = []
    records: dict[int, LoadedRecord] = {}
    for store, record in active_records(projection):
        if isinstance(record.metadata, ActiveTask):
            assert store.store is not None
            order.append(TaskOrderItem(store.store.id, record.metadata))
            records[id(record.metadata)] = record
    ordered = safe_task_order(order)
    rows = tuple(row_for(projection, value.store_id, records[id(value.task)]) for value in ordered)
    in_progress = next((row for row in rows if row.stage is TaskStage.IN_PROGRESS), None)
    ready: list[ItemRow] = []
    blocked: list[ItemRow] = []
    for value, row in zip(ordered, rows, strict=True):
        status = safe_readiness(projection.graph, value.store_id, value.task.id)
        if status is not None and status.ready and projection.complete:
            ready.append(row)
        elif status is None or not status.ready:
            blocked.append(row)
    return in_progress, tuple(ready), tuple(blocked)


def assemble_brief(
    projection: SafeProjection, reader: StoreReader | None
) -> tuple[BriefData, bool, tuple[tuple[str, Revision], ...], int]:
    selected = projection.snapshot.selected
    config = selected.store
    if config is None:
        raise DiagnosticError(
            expected_diagnostic(
                "ORC003",
                "brief requires a valid selected store configuration",
                field="store",
            )
        )
    local, qualified = _indexes(projection)
    pins = _pins(projection, reader, local)
    inactive_map = {
        (v.store_id.root, v.item_id.root): v
        for v in (*pins.inactive, *_governed(projection, qualified))
    }
    inactive = tuple(inactive_map[key] for key in sorted(inactive_map))
    in_progress, ready, blockers = _tasks(projection)
    cap = config.brief.max_rows_per_section
    diagnostics = projection.diagnostics
    missing = projection.snapshot.completeness.missing_store_ids
    due = projection.due_entries
    data = BriefData(
        config.id,
        selected.store_revision,
        selected.registry_revision,
        pins.revisions,
        pins.decisions,
        inactive[:cap],
        in_progress,
        ready[:cap],
        blockers[:cap],
        due[:cap],
        diagnostics[:cap],
        missing[:cap],
        len(ready),
        len(blockers),
        len(due),
        len(diagnostics),
        len(missing),
        len(inactive),
        projection.complete and bool(ready),
        config.brief.max_total_bytes,
    )
    truncated = pins.truncated or any(
        value > cap
        for value in (
            len(ready),
            len(blockers),
            len(due),
            len(diagnostics),
            len(missing),
            len(inactive),
        )
    )
    return data, truncated, pins.revisions, len(pins.decisions)
