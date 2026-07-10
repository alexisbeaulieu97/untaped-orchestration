from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    Decision,
    Registry,
    StoreConfig,
)

type CanonicalTable = dict[str, object]
type CanonicalItem = ActiveTask | ArchivedTask | Decision


def _common_item_fields(item: CanonicalItem) -> CanonicalTable:
    return {
        "schema": item.schema_,
        "id": item.id.root,
        "kind": item.kind.value,
        "title": item.title,
        "created_at": item.created_at.root,
        "tags": [tag.root for tag in item.tags],
    }


def _optional(table: CanonicalTable, key: str, value: object | None) -> None:
    if value is not None:
        table[key] = value


def canonical_item_table(item: CanonicalItem) -> CanonicalTable:
    table = _common_item_fields(item)
    if isinstance(item, ActiveTask):
        table.update(
            {
                "stage": item.stage.value,
                "priority": item.priority.value,
                "rank": item.rank,
            }
        )
        _optional(table, "parent", item.parent.root if item.parent is not None else None)
        _optional(
            table,
            "started_at",
            item.started_at.root if item.started_at is not None else None,
        )
        _optional(table, "revisit_when", item.revisit_when)
        _optional(
            table,
            "reviewed_at",
            item.reviewed_at.root if item.reviewed_at is not None else None,
        )
        _optional(table, "review_on", item.review_on.root if item.review_on is not None else None)
        table["waiting_on"] = [party.root for party in item.waiting_on]
    elif isinstance(item, ArchivedTask):
        table.update({"priority": item.priority.value, "rank": item.rank})
        _optional(table, "parent", item.parent.root if item.parent is not None else None)
        _optional(
            table,
            "started_at",
            item.started_at.root if item.started_at is not None else None,
        )
        _optional(table, "revisit_when", item.revisit_when)
        _optional(
            table,
            "reviewed_at",
            item.reviewed_at.root if item.reviewed_at is not None else None,
        )
        _optional(table, "review_on", item.review_on.root if item.review_on is not None else None)
        table.update(
            {
                "waiting_on": [party.root for party in item.waiting_on],
                "closed_from": item.closed_from.value,
                "outcome": item.outcome.value,
                "closed_at": item.closed_at.root,
                "close_note": item.close_note,
            }
        )
    else:
        _optional(
            table,
            "reviewed_at",
            item.reviewed_at.root if item.reviewed_at is not None else None,
        )
        _optional(table, "review_on", item.review_on.root if item.review_on is not None else None)
        _optional(
            table,
            "retired_at",
            item.retired_at.root if item.retired_at is not None else None,
        )
        _optional(table, "retire_note", item.retire_note)

    table["links"] = [
        {
            "relation": link.relation.value,
            "target_store_id": link.target_store_id.root,
            "target": link.target.root,
        }
        for link in sorted(
            item.links,
            key=lambda value: (
                value.relation.value,
                value.target_store_id.root,
                value.target.root,
            ),
        )
    ]
    table["evidence"] = [
        {"relation": evidence.relation.value, "reference": evidence.reference.root}
        for evidence in sorted(
            item.evidence,
            key=lambda value: (value.relation.value, value.reference.root),
        )
    ]
    return table


def canonical_store_table(config: StoreConfig) -> CanonicalTable:
    return {
        "schema": config.schema_,
        "id": config.id.root,
        "name": config.name,
        "visibility": config.visibility.value,
        "timezone": config.timezone.root,
        "capabilities": {"active_tasks": config.capabilities.active_tasks},
        "curation": {
            "inbox_review_days": config.curation.inbox_review_days,
            "in_progress_review_days": config.curation.in_progress_review_days,
        },
        "brief": {
            "pinned_decisions": [decision_id.root for decision_id in config.brief.pinned_decisions],
            "max_decision_body_bytes": config.brief.max_decision_body_bytes,
            "max_total_body_bytes": config.brief.max_total_body_bytes,
            "max_rows_per_section": config.brief.max_rows_per_section,
            "max_total_bytes": config.brief.max_total_bytes,
        },
    }


def canonical_registry_table(registry: Registry) -> CanonicalTable:
    return {
        "schema": registry.schema_,
        "store_id": registry.store_id.root,
        "children": [
            {"id": child.id.root, "path": child.path}
            for child in sorted(registry.children, key=lambda value: value.id.root)
        ],
    }
