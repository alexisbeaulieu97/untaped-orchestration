from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath

from untaped_orchestration.application.item_support import validated_copy
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.ports import CanonicalFormatter
from untaped_orchestration.application.results import (
    FederatedSnapshot,
    FileDeletion,
    FileReplacement,
    LoadedRecord,
)
from untaped_orchestration.domain.ids import StoreId, TaskId
from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Link,
    LinkRelation,
    Revision,
)


def active_source(archived: ArchivedTask) -> ActiveTask:
    values = archived.model_dump(by_alias=True)
    closed_from = values.pop("closed_from")
    for field in ("outcome", "closed_at", "close_note"):
        values.pop(field)
    values["stage"] = closed_from
    return ActiveTask.model_validate(values)


def canonical_revision(
    formatter: CanonicalFormatter,
    metadata: ActiveTask,
    body: bytes,
) -> Revision:
    digest = sha256(formatter.item_bytes(metadata, body)).hexdigest()
    return Revision(f"sha256:{digest}")


def semantic_source_matches(active: LoadedRecord, archived: LoadedRecord) -> bool:
    return (
        isinstance(active.metadata, ActiveTask)
        and isinstance(archived.metadata, ArchivedTask)
        and active_source(archived.metadata) == active.metadata
        and active.body == archived.body
    )


def successor_source(
    formatter: CanonicalFormatter,
    successor: LoadedRecord | None,
    link: Link,
    expected_revision: Revision | None,
) -> ActiveTask | None:
    if (
        successor is None
        or not isinstance(successor.metadata, ActiveTask)
        or successor.body is None
        or expected_revision is None
        or link not in successor.metadata.links
    ):
        return None
    source = validated_copy(
        successor.metadata,
        {"links": tuple(value for value in successor.metadata.links if value != link)},
    )
    assert isinstance(source, ActiveTask)
    return (
        source
        if canonical_revision(formatter, source, successor.body) == expected_revision
        else None
    )


def accepted_close_base_matches(
    executor: MutationExecutor,
    formatter: CanonicalFormatter,
    snapshot: FederatedSnapshot,
    *,
    active: LoadedRecord | None,
    archive: LoadedRecord | None,
    successor: LoadedRecord | None,
    successor_link: Link | None,
    expected_successor_revision: Revision | None,
    expected_store_revision: Revision,
) -> bool:
    replacements: list[FileReplacement] = []
    deletions: list[FileDeletion] = []
    if archive is not None:
        if not isinstance(archive.metadata, ArchivedTask) or archive.body is None:
            return False
        deletions.append(FileDeletion(archive.path))
        if active is None:
            replacements.append(
                FileReplacement(
                    PurePosixPath("tasks") / archive.path.name,
                    formatter.item_bytes(active_source(archive.metadata), archive.body),
                )
            )
    if successor_link is not None:
        source = successor_source(
            formatter,
            successor,
            successor_link,
            expected_successor_revision,
        )
        if source is None or successor is None or successor.body is None:
            return False
        replacements.append(
            FileReplacement(
                successor.path,
                formatter.item_bytes(source, successor.body),
            )
        )
    projected = executor.project(snapshot, replacements, deletions)
    return projected.snapshot.selected.store_revision == expected_store_revision


def supersedes_link(store_id: StoreId, predecessor_id: TaskId) -> Link:
    return Link(
        relation=LinkRelation.SUPERSEDES,
        target_store_id=store_id,
        target=predecessor_id,
    )
