from collections.abc import Callable

import pytest
from pydantic import ValidationError

from untaped_orchestration.domain.ids import (
    DecisionId,
    Slug,
    StoreId,
    TaskId,
    TypedId,
    creation_slug,
    item_filename,
    item_filename_prefix,
)

VALID_STORE_ID = "sto_019f000000007000800000000000abcd"
VALID_TASK_ID = "tsk_019f000000007000800000000000abcd"
VALID_DECISION_ID = "dec_019f000000007000800000000000abcd"


@pytest.mark.parametrize(
    ("factory", "value", "prefix"),
    [
        (StoreId, VALID_STORE_ID, "sto"),
        (TaskId, VALID_TASK_ID, "tsk"),
        (DecisionId, VALID_DECISION_ID, "dec"),
    ],
)
def test_typed_ids_accept_only_their_uuidv7_prefix(
    factory: Callable[[str], TypedId], value: str, prefix: str
) -> None:
    parsed = factory(value)

    assert parsed.root == value
    assert str(parsed) == value
    assert TypedId.parse(value, prefix=prefix).root == value


@pytest.mark.parametrize(
    ("factory", "value"),
    [
        (StoreId, VALID_TASK_ID),
        (TaskId, VALID_DECISION_ID),
        (DecisionId, VALID_STORE_ID),
        (TaskId, "tsk_019f000000007000800000000000ABCD"),
        (TaskId, "tsk_019f000000006000800000000000abcd"),
        (TaskId, "tsk_019f000000007000700000000000abcd"),
        (TaskId, "tsk_019f000000007000c00000000000abcd"),
        (TaskId, "tsk_019f000000007000800000000000abc"),
    ],
)
def test_typed_ids_reject_wrong_prefix_case_version_variant_or_length(
    factory: Callable[[str], TypedId], value: str
) -> None:
    with pytest.raises((ValueError, ValidationError)):
        factory(value)


def test_item_filename_prefix_is_safe_and_includes_the_full_identity() -> None:
    task_id = TaskId(VALID_TASK_ID)
    decision_id = DecisionId(VALID_DECISION_ID)

    assert item_filename_prefix(task_id) == f"{VALID_TASK_ID}-"
    assert item_filename_prefix(decision_id) == f"{VALID_DECISION_ID}-"
    assert item_filename(task_id, "Crème brûlée") == f"{VALID_TASK_ID}-creme-brulee.md"
    with pytest.raises(TypeError, match="task or decision"):
        item_filename_prefix(StoreId(VALID_STORE_ID))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Crème brûlée", "creme-brulee"),
        ("日本語", "item"),
        ("--- One___Two ---", "one-two"),
        ("a" * 65, "a" * 64),
        ("a" * 63 + " " + "b", "a" * 63),
    ],
)
def test_creation_slug_is_canonical_bounded_and_creation_stable(title: str, expected: str) -> None:
    slug = creation_slug(title)

    assert slug == expected
    assert len(slug) <= 64


@pytest.mark.parametrize("value", ["alexis", "release-team", "a" * 64])
def test_slug_accepts_lowercase_safe_values(value: str) -> None:
    assert Slug(value).root == value


@pytest.mark.parametrize(
    "value",
    ["Alexis", "release_team", "release team", "-release", "release-", "a" * 65, ""],
)
def test_slug_rejects_noncanonical_or_oversized_values(value: str) -> None:
    with pytest.raises(ValidationError):
        Slug(value)
