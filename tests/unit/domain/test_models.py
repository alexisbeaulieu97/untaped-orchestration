from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from untaped_orchestration.domain.models import (
    ActiveTask,
    ArchivedTask,
    BriefConfig,
    Decision,
    ImportManifest,
    Link,
    Registry,
    StoreConfig,
)
from untaped_orchestration.domain.time import (
    CalendarDate,
    IanaTimezone,
    UtcTimestamp,
    format_utc_timestamp,
    local_calendar_date,
)

STORE_ID = "sto_019f0000000070008000000000000000"
TASK_ID = "tsk_019f0000000070008000000000000010"
DECISION_ID = "dec_019f0000000070008000000000000001"
TIMESTAMP = "2026-07-10T01:02:03.004Z"
REVISION = "sha256:" + "a" * 64


def store_data() -> dict[str, Any]:
    return {
        "schema": "untaped.orchestration.store/v1",
        "id": STORE_ID,
        "name": "Untaped orchestration hub",
        "visibility": "private",
        "timezone": "America/Montreal",
        "capabilities": {"active_tasks": True},
        "curation": {"inbox_review_days": 7, "in_progress_review_days": 14},
        "brief": {
            "pinned_decisions": [],
            "max_decision_body_bytes": 4096,
            "max_total_body_bytes": 16384,
            "max_rows_per_section": 10,
            "max_total_bytes": 32768,
        },
    }


def task_data() -> dict[str, Any]:
    return {
        "schema": "untaped.orchestration.task/v1",
        "id": TASK_ID,
        "kind": "task",
        "title": "Land the public orchestration specification",
        "created_at": TIMESTAMP,
        "tags": ["orchestration", "specification"],
        "stage": "inbox",
        "priority": "normal",
        "rank": 1000,
        "waiting_on": [],
        "links": [],
        "evidence": [],
    }


def decision_data() -> dict[str, Any]:
    return {
        "schema": "untaped.orchestration.decision/v1",
        "id": DECISION_ID,
        "kind": "decision",
        "title": "Use TOML front matter",
        "created_at": TIMESTAMP,
        "tags": ["format"],
        "links": [],
        "evidence": [],
    }


@pytest.mark.parametrize(
    "value",
    [TIMESTAMP, "2000-02-29T23:59:59.999Z"],
)
def test_utc_timestamp_accepts_exact_millisecond_utc_values(value: str) -> None:
    assert UtcTimestamp(value).root == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-10T01:02:03Z",
        "2026-07-10T01:02:03.04Z",
        "2026-07-10T01:02:03.004+00:00",
        "2026-07-10 01:02:03.004Z",
        "2026-02-30T01:02:03.004Z",
    ],
)
def test_utc_timestamp_rejects_nonexact_or_impossible_values(value: str) -> None:
    with pytest.raises(ValidationError):
        UtcTimestamp(value)


def test_time_helpers_format_injected_clock_values_and_convert_local_dates() -> None:
    formatted = format_utc_timestamp(datetime(2026, 7, 10, 1, 2, 3, 456789, tzinfo=UTC))

    assert formatted.root == "2026-07-10T01:02:03.456Z"
    assert local_calendar_date(formatted, IanaTimezone("America/Montreal")).root == "2026-07-09"
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc_timestamp(datetime(2026, 7, 10, 1, 2, 3))


@pytest.mark.parametrize("value", ["UTC", "America/Montreal", "Europe/Paris"])
def test_iana_timezone_accepts_installed_iana_names(value: str) -> None:
    assert IanaTimezone(value).root == value


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "", "America\\Montreal"])
def test_iana_timezone_rejects_unknown_names(value: str) -> None:
    with pytest.raises(ValidationError):
        IanaTimezone(value)


@pytest.mark.parametrize("value", ["2026-07-10", "2000-02-29"])
def test_calendar_date_accepts_exact_dates(value: str) -> None:
    assert CalendarDate(value).root == value


@pytest.mark.parametrize("value", ["2026-7-10", "2026-02-30", "2026-07-10Z"])
def test_calendar_date_rejects_nonexact_or_impossible_dates(value: str) -> None:
    with pytest.raises(ValidationError):
        CalendarDate(value)


def test_store_config_models_the_complete_frozen_admin_shape() -> None:
    store = StoreConfig.model_validate(store_data())

    assert store.id.root == STORE_ID
    assert store.brief.max_total_bytes == 32768
    with pytest.raises(ValidationError, match="frozen"):
        store.name = "Changed"  # type: ignore[misc]


@pytest.mark.parametrize("value", ["", " ", "a" * 121, "bad\nname", "bad\u2028name"])
def test_store_name_enforces_nonempty_display_bounds(value: str) -> None:
    data = store_data()
    data["name"] = value

    with pytest.raises(ValidationError):
        StoreConfig.model_validate(data)


def test_public_store_forbids_active_task_capability() -> None:
    data = store_data()
    data["visibility"] = "public"

    with pytest.raises(ValidationError, match="public stores cannot enable active tasks"):
        StoreConfig.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_decision_body_bytes", 4097),
        ("max_total_body_bytes", 16385),
        ("max_rows_per_section", 11),
        ("max_total_bytes", 4095),
        ("max_total_bytes", 32769),
    ],
)
def test_brief_config_enforces_body_row_and_total_output_bounds(field: str, value: int) -> None:
    data = deepcopy(store_data()["brief"])
    data[field] = value

    with pytest.raises(ValidationError):
        BriefConfig.model_validate(data)


def test_store_and_registry_models_forbid_extra_fields() -> None:
    data = store_data()
    data["unknown"] = True
    with pytest.raises(ValidationError):
        StoreConfig.model_validate(data)

    with pytest.raises(ValidationError):
        Registry.model_validate(
            {
                "schema": "untaped.orchestration.registry/v1",
                "store_id": STORE_ID,
                "children": [],
                "unknown": True,
            }
        )


def test_registry_keeps_typed_child_identity_and_relative_path() -> None:
    registry = Registry.model_validate(
        {
            "schema": "untaped.orchestration.registry/v1",
            "store_id": STORE_ID,
            "children": [
                {
                    "id": "sto_019f0000000070008000000000000002",
                    "path": "../../untaped/.untaped/orchestration",
                }
            ],
        }
    )

    assert registry.children[0].id.root.endswith("0002")


@pytest.mark.parametrize("title", ["", " ", "a" * 241])
def test_item_title_enforces_nonempty_unicode_bounds(title: str) -> None:
    data = task_data()
    data["title"] = title

    with pytest.raises(ValidationError):
        ActiveTask.model_validate(data)


@pytest.mark.parametrize("tag", ["Mixed-Case", "under_score", "a" * 65])
def test_tags_require_lowercase_bounded_slugs(tag: str) -> None:
    data = task_data()
    data["tags"] = [tag]

    with pytest.raises(ValidationError):
        ActiveTask.model_validate(data)


def test_tags_are_unique_sorted_and_bounded() -> None:
    data = task_data()
    data["tags"] = ["zeta", "alpha"]
    task = ActiveTask.model_validate(data)
    assert [tag.root for tag in task.tags] == ["alpha", "zeta"]

    data["tags"] = ["duplicate", "duplicate"]
    with pytest.raises(ValidationError, match="unique"):
        ActiveTask.model_validate(data)

    data["tags"] = [f"tag-{index}" for index in range(33)]
    with pytest.raises(ValidationError):
        ActiveTask.model_validate(data)


@pytest.mark.parametrize("party", ["Alexis", "release_team", "a" * 65])
def test_waiting_parties_require_lowercase_bounded_slugs(party: str) -> None:
    data = task_data()
    data["waiting_on"] = [party]

    with pytest.raises(ValidationError):
        ActiveTask.model_validate(data)


def test_waiting_parties_are_unique_sorted_and_limited_to_eight() -> None:
    data = task_data()
    data["waiting_on"] = ["zeta", "alpha"]
    task = ActiveTask.model_validate(data)
    assert [party.root for party in task.waiting_on] == ["alpha", "zeta"]

    data["waiting_on"] = ["duplicate", "duplicate"]
    with pytest.raises(ValidationError, match="unique"):
        ActiveTask.model_validate(data)

    data["waiting_on"] = [f"party-{index}" for index in range(9)]
    with pytest.raises(ValidationError):
        ActiveTask.model_validate(data)


def test_backlog_requires_revisit_trigger_and_other_active_stages_forbid_it() -> None:
    backlog = task_data()
    backlog["stage"] = "backlog"
    with pytest.raises(ValidationError, match="revisit_when"):
        ActiveTask.model_validate(backlog)

    inbox = task_data()
    inbox["revisit_when"] = "After the SDK release"
    with pytest.raises(ValidationError, match="revisit_when"):
        ActiveTask.model_validate(inbox)


def test_rank_is_positive_and_within_signed_64_bit_range() -> None:
    for invalid_rank in (0, -1, 2**63):
        data = task_data()
        data["rank"] = invalid_rank
        with pytest.raises(ValidationError):
            ActiveTask.model_validate(data)


def test_active_and_archive_shapes_cannot_mix_lifecycle_owned_fields() -> None:
    active = task_data()
    active.update(
        {
            "closed_from": "inbox",
            "outcome": "declined",
            "closed_at": TIMESTAMP,
            "close_note": "No longer needed",
        }
    )
    with pytest.raises(ValidationError):
        ActiveTask.model_validate(active)

    archived = task_data()
    archived.pop("stage")
    archived.update(
        {
            "closed_from": "inbox",
            "outcome": "declined",
            "closed_at": TIMESTAMP,
            "close_note": "No longer needed",
        }
    )
    parsed = ArchivedTask.model_validate(archived)
    assert parsed.closed_from.value == "inbox"

    archived["stage"] = "inbox"
    with pytest.raises(ValidationError):
        ArchivedTask.model_validate(archived)


def test_task_model_forbids_unknown_fields() -> None:
    data = task_data()
    data["unknown"] = True

    with pytest.raises(ValidationError):
        ActiveTask.model_validate(data)


def test_links_are_typed_and_forbid_unknown_fields() -> None:
    link = Link.model_validate(
        {
            "relation": "depends-on",
            "target_store_id": STORE_ID,
            "target": TASK_ID,
        }
    )
    assert link.target.root == TASK_ID

    with pytest.raises(ValidationError):
        Link.model_validate(
            {
                "relation": "depends-on",
                "target_store_id": STORE_ID,
                "target": TASK_ID,
                "extra": True,
            }
        )


@pytest.mark.parametrize(
    ("retired_at", "retire_note"),
    [(TIMESTAMP, None), (None, "Mechanism ended"), (TIMESTAMP, " ")],
)
def test_decision_retirement_fields_are_paired_and_note_is_nonempty(
    retired_at: str | None, retire_note: str | None
) -> None:
    data = decision_data()
    data["retired_at"] = retired_at
    data["retire_note"] = retire_note

    with pytest.raises(ValidationError):
        Decision.model_validate(data)


def test_import_manifest_is_typed_frozen_and_forbids_extra_fields() -> None:
    manifest = ImportManifest.model_validate(
        {
            "schema": "untaped.orchestration.import/v1",
            "target_store_id": STORE_ID,
            "expected_store_revision": REVISION,
            "require_empty_items": True,
            "records": [
                {
                    "destination": "decisions",
                    "frontmatter_file": "records/decision-01.toml",
                    "body_file": "records/decision-01.md",
                    "source_ref": "git:abc123:orchestration/DECISIONS.md#sha256:abcd",
                }
            ],
        }
    )
    assert manifest.expected_store_revision.root == REVISION
    assert manifest.records[0].source_ref.root.startswith("git:")

    data = manifest.model_dump(mode="json")
    data["extra"] = True
    with pytest.raises(ValidationError):
        ImportManifest.model_validate(data)


@pytest.mark.parametrize(
    "revision",
    ["sha256:" + "A" * 64, "sha256:" + "a" * 63, "md5:" + "a" * 64],
)
def test_import_manifest_requires_exact_lowercase_sha256_revision(revision: str) -> None:
    data = {
        "schema": "untaped.orchestration.import/v1",
        "target_store_id": STORE_ID,
        "expected_store_revision": revision,
        "require_empty_items": True,
        "records": [],
    }

    with pytest.raises(ValidationError):
        ImportManifest.model_validate(data)
