from __future__ import annotations

from collections import deque

from untaped_orchestration.application.query_models import (
    QualifiedItem,
    TraceData,
    TraceDirection,
    TraceEvidence,
    TraceItem,
    TraceLink,
)
from untaped_orchestration.application.results import LoadedRecord, StoreSnapshot
from untaped_orchestration.domain.ids import StoreId
from untaped_orchestration.domain.models import LinkRelation

type Edge = tuple[QualifiedItem, QualifiedItem, LinkRelation]


def _index(stores: tuple[StoreSnapshot, ...]) -> dict[tuple[str, str], LoadedRecord]:
    return {
        (store.store.id.root, record.metadata.id.root): record
        for store in stores
        if store.store is not None
        for record in store.records
    }


def _edges(stores: tuple[StoreSnapshot, ...]) -> tuple[Edge, ...]:
    persisted: dict[tuple[str, str, str, str, str], Edge] = {}
    for store in stores:
        if store.store is None:
            continue
        for record in store.records:
            source = QualifiedItem(store.store.id, record.metadata.id)
            for link in record.metadata.links:
                target = QualifiedItem(link.target_store_id, link.target)
                key = (
                    source.store_id.root,
                    source.item_id.root,
                    link.relation.value,
                    target.store_id.root,
                    target.item_id.root,
                )
                persisted[key] = (source, target, link.relation)
    return tuple(
        sorted(
            persisted.values(),
            key=lambda value: (
                value[2].value,
                value[1].store_id.root,
                value[1].item_id.root,
                value[0].store_id.root,
                value[0].item_id.root,
            ),
        )
    )


def _neighbors(
    current: QualifiedItem,
    edges: tuple[Edge, ...],
    direction: TraceDirection,
) -> tuple[QualifiedItem, ...]:
    values = []
    for source, target, _ in edges:
        if direction in {TraceDirection.OUTGOING, TraceDirection.BOTH} and source == current:
            values.append(target)
        if direction in {TraceDirection.INCOMING, TraceDirection.BOTH} and target == current:
            values.append(source)
    return tuple(values)


def _walk(
    root: QualifiedItem,
    edges: tuple[Edge, ...],
    direction: TraceDirection,
    limit: int,
) -> tuple[list[TraceItem], dict[tuple[str, str], int], bool]:
    pending = deque([(root, 0)])
    seen = {(root.store_id.root, root.item_id.root)}
    depths = {(root.store_id.root, root.item_id.root): 0}
    items: list[TraceItem] = []
    truncated = False
    while pending:
        current, depth = pending.popleft()
        for neighbor in _neighbors(current, edges, direction):
            key = (neighbor.store_id.root, neighbor.item_id.root)
            if key in seen:
                continue
            if len(items) >= limit:
                truncated = True
                continue
            seen.add(key)
            depths[key] = depth + 1
            items.append(TraceItem(neighbor, depth + 1))
            pending.append((neighbor, depth + 1))
    return items, depths, truncated


def _links(
    edges: tuple[Edge, ...],
    depths: dict[tuple[str, str], int],
    limit: int,
) -> tuple[list[TraceLink], bool]:
    values: list[TraceLink] = []
    for source, target, relation in edges:
        source_key = (source.store_id.root, source.item_id.root)
        target_key = (target.store_id.root, target.item_id.root)
        if source_key not in depths or target_key not in depths:
            continue
        if len(values) >= limit:
            return values, True
        values.append(
            TraceLink(source, target, relation, max(depths[source_key], depths[target_key]))
        )
    return values, False


def _evidence(
    index: dict[tuple[str, str], LoadedRecord],
    depths: dict[tuple[str, str], int],
    limit: int,
) -> tuple[list[TraceEvidence], bool]:
    values: list[TraceEvidence] = []
    for key in sorted(depths, key=lambda value: (depths[value], value)):
        record = index.get(key)
        if record is None:
            continue
        owner = QualifiedItem(StoreId(key[0]), record.metadata.id)
        for evidence in record.metadata.evidence:
            if len(values) >= limit:
                return values, True
            values.append(
                TraceEvidence(owner, evidence.relation.value, evidence.reference.root, depths[key])
            )
    return values, False


def build_trace(
    stores: tuple[StoreSnapshot, ...],
    root: QualifiedItem,
    *,
    direction: TraceDirection,
    limit: int,
) -> tuple[TraceData, bool]:
    index = _index(stores)
    edges = _edges(stores)
    items, depths, item_truncated = _walk(root, edges, direction, limit)
    links, link_truncated = _links(edges, depths, limit)
    evidence, evidence_truncated = _evidence(index, depths, limit)
    return (
        TraceData(root, tuple(items), tuple(links), tuple(evidence)),
        item_truncated or link_truncated or evidence_truncated,
    )
