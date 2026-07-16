from __future__ import annotations

from collections.abc import Callable, Iterator
from enum import StrEnum

from untaped_orchestration.application.results import LoadedRecord
from untaped_orchestration.domain.ids import DecisionId, Slug, StoreId
from untaped_orchestration.domain.models import (
    Decision,
    Link,
    LinkRelation,
    Revision,
    StoreConfig,
)


class SupersedePhase(StrEnum):
    SUCCESSOR_ONLY = "successor-only"
    FRESH_FINAL = "fresh-final"


def supersedes_links(
    store_id: StoreId, predecessor_ids: tuple[DecisionId, ...]
) -> tuple[Link, ...]:
    return tuple(
        Link(
            relation=LinkRelation.SUPERSEDES,
            target_store_id=store_id,
            target=item_id,
        )
        for item_id in sorted(predecessor_ids, key=lambda value: value.root)
    )


def pins_after_supersede(
    pins: tuple[DecisionId, ...],
    predecessor_ids: frozenset[DecisionId],
    successor_id: DecisionId,
) -> tuple[DecisionId, ...]:
    predecessor_positions = [index for index, value in enumerate(pins) if value in predecessor_ids]
    stripped = [value for value in pins if value not in predecessor_ids and value != successor_id]
    if not predecessor_positions:
        return tuple(stripped)
    earliest = min(predecessor_positions)
    insertion = sum(
        1 for value in pins[:earliest] if value not in predecessor_ids and value != successor_id
    )
    stripped.insert(insertion, successor_id)
    return tuple(stripped)


def store_with_pins(config: StoreConfig, pins: tuple[DecisionId, ...]) -> StoreConfig:
    return config.model_copy(
        update={"brief": config.brief.model_copy(update={"pinned_decisions": pins})}
    )


def exact_successor_shape(
    record: LoadedRecord,
    *,
    title: str,
    body: bytes,
    tags: tuple[Slug, ...],
    links: tuple[Link, ...],
) -> bool:
    metadata = record.metadata
    return (
        isinstance(metadata, Decision)
        and record.body == body
        and metadata.title == title
        and metadata.tags == tuple(sorted(tags, key=lambda value: value.root))
        and metadata.links == links
        and not metadata.evidence
        and metadata.reviewed_at is None
        and metadata.review_on is None
        and metadata.retired_at is None
        and metadata.retire_note is None
    )


def recognize_supersede_phase(
    *,
    current_revision: Revision,
    expected_revision: Revision,
    pins: tuple[DecisionId, ...],
    predecessor_ids: tuple[DecisionId, ...],
    reverse_revision: Callable[[], Revision],
) -> SupersedePhase | None:
    predecessor_set = frozenset(predecessor_ids)
    if current_revision == expected_revision and not predecessor_set.intersection(pins):
        return SupersedePhase.FRESH_FINAL
    if reverse_revision() == expected_revision:
        return SupersedePhase.SUCCESSOR_ONLY
    return None


def retirement_prior_configs(config: StoreConfig, item_id: DecisionId) -> Iterator[StoreConfig]:
    if item_id in config.brief.pinned_decisions:
        return
    for index in range(len(config.brief.pinned_decisions) + 1):
        pins = list(config.brief.pinned_decisions)
        pins.insert(index, item_id)
        yield store_with_pins(config, tuple(pins))
