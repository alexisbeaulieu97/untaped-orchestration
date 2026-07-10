from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    IncompleteStore,
    LoadedRecord,
    StoreLocation,
    StoreSnapshot,
)
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    BriefConfig,
    CurationConfig,
    Decision,
    Link,
    LinkRelation,
    Revision,
    StoreCapabilities,
    StoreConfig,
    TaskOutcome,
    TaskPriority,
    TaskStage,
    Visibility,
)
from untaped_orchestration.domain.time import IanaTimezone, UtcTimestamp

STORE = StoreId("sto_019f0000000070008000000000000000")
OTHER = StoreId("sto_019f0000000070008000000000000001")
NOW = UtcTimestamp("2026-07-10T01:02:03.004Z")
REVISION = Revision("sha256:" + "a" * 64)
COMPLETE = Completeness()


def tid(number: int) -> TaskId:
    return TaskId(f"tsk_019f000000007000800000000000{number:04x}")


def did(number: int) -> DecisionId:
    return DecisionId(f"dec_019f000000007000800000000000{number:04x}")


def config(
    store_id: StoreId = STORE,
    *,
    visibility: Visibility = Visibility.PRIVATE,
    active_tasks: bool = True,
    pins: tuple[DecisionId, ...] = (),
) -> StoreConfig:
    return StoreConfig(
        schema="untaped.orchestration.store/v1",
        id=store_id,
        name=store_id.root,
        visibility=visibility,
        timezone=IanaTimezone("UTC"),
        capabilities=StoreCapabilities(active_tasks=active_tasks),
        curation=CurationConfig(inbox_review_days=7, in_progress_review_days=14),
        brief=BriefConfig(
            pinned_decisions=pins,
            max_decision_body_bytes=4096,
            max_total_body_bytes=16384,
            max_rows_per_section=10,
            max_total_bytes=32768,
        ),
    )


def task(number: int, *, links: tuple[Link, ...] = ()) -> ActiveTask:
    return ActiveTask(
        schema="untaped.orchestration.task/v1",
        id=tid(number),
        kind="task",
        title=f"Task {number}",
        created_at=NOW,
        tags=(),
        links=links,
        evidence=(),
        stage=TaskStage.INBOX,
        priority=TaskPriority.NORMAL,
        rank=number * 1000,
        waiting_on=(),
    )


def archived(number: int, outcome: TaskOutcome) -> ArchivedTask:
    return ArchivedTask(
        schema="untaped.orchestration.task/v1",
        id=tid(number),
        kind="task",
        title=f"Task {number}",
        created_at=NOW,
        tags=(),
        links=(),
        evidence=(),
        priority=TaskPriority.NORMAL,
        rank=number * 1000,
        started_at=NOW if outcome is TaskOutcome.CANCELLED else None,
        waiting_on=(),
        closed_from=TaskStage.PLANNED,
        outcome=outcome,
        closed_at=NOW,
        close_note="closed",
    )


def decision(
    number: int,
    *,
    retired: bool = False,
    links: tuple[Link, ...] = (),
) -> Decision:
    return Decision(
        schema="untaped.orchestration.decision/v1",
        id=did(number),
        kind="decision",
        title=f"Decision {number}",
        created_at=NOW,
        tags=(),
        links=links,
        evidence=(),
        retired_at=NOW if retired else None,
        retire_note="retired" if retired else None,
    )


def relation(relation: LinkRelation, target: TaskId | DecisionId, store: StoreId) -> Link:
    return Link(relation=relation, target_store_id=store, target=target)


def loaded(path: str, metadata: ActiveTask | ArchivedTask | Decision) -> LoadedRecord:
    return LoadedRecord(PurePosixPath(path), REVISION, metadata, None)


def store_snapshot(
    store_id: StoreId,
    records: tuple[LoadedRecord, ...],
    *,
    store_config: StoreConfig | None = None,
    diagnostics: tuple[Diagnostic, ...] = (),
) -> StoreSnapshot:
    root = Path(f"/tmp/{store_id.root}")
    return StoreSnapshot(
        location=StoreLocation(root, root),
        store=store_config or config(store_id),
        registry=None,
        records=records,
        load_diagnostics=diagnostics,
        raw_index=(),
        store_revision=REVISION,
        registry_revision=None,
        store_config_revision=REVISION,
    )


def snapshot(
    selected: StoreSnapshot,
    *stores: StoreSnapshot,
    completeness: Completeness = COMPLETE,
) -> FederatedSnapshot:
    values = (selected, *stores)
    return FederatedSnapshot(selected=selected, stores=values, completeness=completeness)


@pytest.mark.parametrize(
    ("relation_kind", "target", "target_store"),
    [
        (LinkRelation.DEPENDS_ON, tid(2), OTHER),
        (LinkRelation.SUPERSEDES, tid(2), OTHER),
        (LinkRelation.GOVERNED_BY, did(2), OTHER),
        (LinkRelation.FOLLOW_UP_TO, tid(2), OTHER),
    ],
)
def test_relation_locality_and_cross_store_matrix(
    relation_kind: LinkRelation,
    target: TaskId | DecisionId,
    target_store: StoreId,
) -> None:
    source = task(1, links=(relation(relation_kind, target, target_store),))
    selected = store_snapshot(STORE, (loaded("tasks/source.md", source),))

    diagnostics = validate_snapshot(snapshot(selected), require_children=False)

    structural = relation_kind in {LinkRelation.DEPENDS_ON, LinkRelation.SUPERSEDES}
    assert any("same-store" in value.message for value in diagnostics) is structural


def test_complete_federation_reports_missing_cross_store_target_but_incomplete_defers_it() -> None:
    source = task(1, links=(relation(LinkRelation.GOVERNED_BY, did(2), OTHER),))
    selected = store_snapshot(STORE, (loaded("tasks/source.md", source),))
    missing = IncompleteStore(
        expected_store_id=OTHER,
        reason="missing",
        diagnostic=Diagnostic(
            code="ORC005",
            severity="warning",
            path="registry.toml",
            field="children",
            message="missing child",
            hint="restore it",
        ),
    )

    complete = validate_snapshot(snapshot(selected), require_children=False)
    incomplete = validate_snapshot(
        snapshot(selected, completeness=Completeness((missing,))),
        require_children=False,
    )

    assert any("target store" in value.message for value in complete)
    assert not any("target store" in value.message for value in incomplete)
    assert any(value.code == "ORC005" for value in incomplete)


def test_missing_target_inside_loaded_store_is_always_invalid() -> None:
    source = task(1, links=(relation(LinkRelation.GOVERNED_BY, did(2), OTHER),))
    selected = store_snapshot(STORE, (loaded("tasks/source.md", source),))
    other = store_snapshot(OTHER, ())

    diagnostics = validate_snapshot(snapshot(selected, other), require_children=False)

    assert any("target item" in value.message for value in diagnostics)


@pytest.mark.parametrize(
    ("relation_kind", "target", "target_store"),
    [
        (LinkRelation.DEPENDS_ON, tid(2), STORE),
        (LinkRelation.GOVERNED_BY, did(2), OTHER),
        (LinkRelation.FOLLOW_UP_TO, tid(2), OTHER),
    ],
)
def test_loaded_target_store_wins_over_overlapping_incompleteness_metadata(
    relation_kind: LinkRelation,
    target: TaskId | DecisionId,
    target_store: StoreId,
) -> None:
    source = task(1, links=(relation(relation_kind, target, target_store),))
    selected = store_snapshot(STORE, (loaded("tasks/source.md", source),))
    other = store_snapshot(OTHER, ())
    missing = IncompleteStore(
        expected_store_id=target_store,
        reason="missing",
        diagnostic=Diagnostic(
            code="ORC005",
            severity="warning",
            path="registry.toml",
            field="children",
            message="stale missing metadata",
            hint="reread it",
        ),
    )

    diagnostics = validate_snapshot(
        snapshot(selected, other, completeness=Completeness((missing,))),
        require_children=False,
    )

    assert any("target item" in value.message for value in diagnostics)


def test_public_or_decision_only_store_rejects_active_tasks() -> None:
    value = loaded("tasks/source.md", task(1))
    public_config = config(STORE, visibility=Visibility.PUBLIC, active_tasks=False)
    public = store_snapshot(STORE, (value,), store_config=public_config)
    decision_only = store_snapshot(
        OTHER,
        (value,),
        store_config=config(OTHER, active_tasks=False),
    )

    public_diagnostics = validate_snapshot(snapshot(public), require_children=False)
    decision_diagnostics = validate_snapshot(snapshot(decision_only), require_children=False)

    assert any(value.code == "ORC009" and "public" in value.message for value in public_diagnostics)
    assert any(
        value.code == "ORC009" and "task records" in value.message for value in decision_diagnostics
    )


@pytest.mark.parametrize("outcome", list(TaskOutcome))
@pytest.mark.parametrize("policy", ["public", "decision-only"])
def test_public_or_decision_only_store_rejects_every_archived_task_outcome(
    outcome: TaskOutcome,
    policy: str,
) -> None:
    value = loaded("archive/tasks/source.md", archived(1, outcome))
    store_config = (
        config(STORE, visibility=Visibility.PUBLIC, active_tasks=False)
        if policy == "public"
        else config(STORE, active_tasks=False)
    )
    selected = store_snapshot(STORE, (value,), store_config=store_config)

    diagnostics = validate_snapshot(snapshot(selected), require_children=False)

    assert any(value.code == "ORC009" and "task records" in value.message for value in diagnostics)


def test_pins_must_resolve_to_active_local_decisions() -> None:
    retired = decision(1, retired=True)
    value = store_snapshot(
        STORE,
        (loaded("decisions/retired.md", retired),),
        store_config=config(STORE, pins=(did(1), did(2))),
    )

    diagnostics = validate_snapshot(snapshot(value), require_children=False)

    assert sum(value.field.startswith("brief.pinned_decisions") for value in diagnostics) == 2
    assert all(
        value.code == "ORC006"
        for value in diagnostics
        if value.field.startswith("brief.pinned_decisions")
    )


def test_active_local_pin_is_not_confused_by_same_decision_id_in_another_store() -> None:
    local = decision(1)
    remote = decision(1, retired=True)
    selected = store_snapshot(
        STORE,
        (loaded("decisions/local.md", local),),
        store_config=config(STORE, pins=(did(1),)),
    )
    other = store_snapshot(OTHER, (loaded("decisions/remote.md", remote),))

    diagnostics = validate_snapshot(snapshot(selected, other), require_children=False)

    assert not any(value.field.startswith("brief.pinned_decisions") for value in diagnostics)


def test_valid_cross_store_duplicate_task_and_decision_ids_remain_distinct_and_deterministic() -> (
    None
):
    selected = store_snapshot(
        STORE,
        (
            loaded("tasks/local.md", task(1)),
            loaded("decisions/local.md", decision(1)),
        ),
    )
    other = store_snapshot(
        OTHER,
        (
            loaded("archive/tasks/remote.md", archived(1, TaskOutcome.DELIVERED)),
            loaded("decisions/remote.md", decision(1)),
        ),
    )
    forward = FederatedSnapshot(selected, (selected, other), COMPLETE)
    reversed_stores = FederatedSnapshot(selected, (other, selected), COMPLETE)

    first = validate_snapshot(forward, require_children=False)
    second = validate_snapshot(reversed_stores, require_children=False)

    assert first == second
    assert not any(value.code == "ORC003" for value in first)


@pytest.mark.parametrize("inactive_kind", ["retired", "superseded"])
def test_governed_by_inactive_decision_is_valid_but_warned(inactive_kind: str) -> None:
    inactive = decision(1, retired=inactive_kind == "retired")
    records = [loaded("decisions/inactive.md", inactive)]
    if inactive_kind == "superseded":
        successor = decision(
            2,
            links=(relation(LinkRelation.SUPERSEDES, did(1), STORE),),
        )
        records.append(loaded("decisions/successor.md", successor))
    source = task(1, links=(relation(LinkRelation.GOVERNED_BY, did(1), STORE),))
    records.append(loaded("tasks/source.md", source))
    value = store_snapshot(
        STORE,
        tuple(records),
    )

    diagnostics = validate_snapshot(snapshot(value), require_children=False)

    warning = next(value for value in diagnostics if "inactive decision" in value.message)
    assert warning.severity == "warning"


def test_require_children_promotes_incompleteness_warning_without_mutating_input() -> None:
    selected = store_snapshot(STORE, ())
    original = Diagnostic(
        code="ORC005",
        severity="warning",
        path="registry.toml",
        field="children",
        message="missing child",
        hint="restore it",
    )
    missing = IncompleteStore(OTHER, "missing", original)
    value = snapshot(selected, completeness=Completeness((missing,)))

    permissive = validate_snapshot(value, require_children=False)
    required = validate_snapshot(value, require_children=True)

    assert permissive[0].severity == "warning"
    assert required[0].severity == "error"
    assert original.severity == "warning"


def test_validation_aggregates_load_policy_relation_and_graph_diagnostics_in_stable_order() -> None:
    syntax = Diagnostic(
        code="ORC001",
        severity="error",
        path="z.md",
        field="toml",
        message="bad syntax",
        hint="fix it",
    )
    first = task(1, links=(relation(LinkRelation.DEPENDS_ON, tid(2), STORE),))
    second = task(2, links=(relation(LinkRelation.DEPENDS_ON, tid(1), STORE),))
    selected = store_snapshot(
        STORE,
        (loaded("tasks/a.md", first), loaded("tasks/b.md", second)),
        diagnostics=(syntax,),
    )

    diagnostics = validate_snapshot(snapshot(selected), require_children=False)

    assert syntax in diagnostics
    assert any("dependency cycle" in value.message for value in diagnostics)
    assert diagnostics == sort_diagnostics(diagnostics)
