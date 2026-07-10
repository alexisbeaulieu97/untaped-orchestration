from typing import Any

import pytest
from pydantic import ValidationError

from untaped_orchestration.domain.evidence import (
    Evidence,
    EvidenceReference,
    canonicalize_evidence_reference,
)
from untaped_orchestration.domain.models import ActiveTask

TASK_ID = "tsk_019f0000000070008000000000000010"
TIMESTAMP = "2026-07-10T01:02:03.004Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("github-pr:Alexis/Untaped#35", "github-pr:alexis/untaped#35"),
        ("github-issue:Alexis/Untaped#12", "github-issue:alexis/untaped#12"),
        (
            "github-release:Alexis/Untaped@V1.0.0-RC1",
            "github-release:alexis/untaped@V1.0.0-RC1",
        ),
        (
            "github-commit:Alexis/Untaped@ABCDEF0123456789ABCDEF0123456789ABCDEF01",
            "github-commit:alexis/untaped@abcdef0123456789abcdef0123456789abcdef01",
        ),
        ("pypi:Untaped_Orchestration@0.1.0", "pypi:untaped-orchestration@0.1.0"),
        (
            "url:https://EXAMPLE.com:443/path?q=Value#Fragment",
            "url:https://example.com/path?q=Value#Fragment",
        ),
        ("url:https://EXAMPLE.com:8443/path", "url:https://example.com:8443/path"),
        (
            "git:abc123:orchestration/DECISIONS.md#sha256:abcd",
            "git:abc123:orchestration/DECISIONS.md#sha256:abcd",
        ),
    ],
)
def test_evidence_reference_canonicalizes_known_schemes_and_preserves_unknown_lowercase(
    value: str, expected: str
) -> None:
    assert canonicalize_evidence_reference(value) == expected
    assert EvidenceReference(value).root == expected


@pytest.mark.parametrize(
    "value",
    [
        "github-pr:owner/repo#0",
        "github-issue:owner/repo#0",
        "github-release:owner/repo@",
        "github-commit:owner/repo@abc",
        "pypi:not a project@1.0",
        "url:http://example.com/path",
        "url:https:///missing-host",
        "Unknown:value",
        "unknown:payload with space",
        "unknown:",
    ],
)
def test_evidence_reference_rejects_invalid_known_and_opaque_syntax(value: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        EvidenceReference(value)


def test_evidence_record_is_frozen_canonical_and_extra_forbid() -> None:
    evidence = Evidence.model_validate(
        {"relation": "tracked-by", "reference": "github-pr:Alexis/Untaped#35"}
    )

    assert evidence.reference.root == "github-pr:alexis/untaped#35"
    with pytest.raises(ValidationError, match="frozen"):
        evidence.relation = "verified-by"  # type: ignore[assignment]
    with pytest.raises(ValidationError):
        Evidence.model_validate(
            {
                "relation": "tracked-by",
                "reference": "github-pr:owner/repo#1",
                "extra": True,
            }
        )


def active_task_data() -> dict[str, Any]:
    return {
        "schema": "untaped.orchestration.task/v1",
        "id": TASK_ID,
        "kind": "task",
        "title": "Evidence task",
        "created_at": TIMESTAMP,
        "tags": [],
        "stage": "inbox",
        "priority": "normal",
        "rank": 1000,
        "waiting_on": [],
        "links": [],
        "evidence": [],
    }


def test_item_rejects_duplicate_evidence_after_canonicalization() -> None:
    data = active_task_data()
    data["evidence"] = [
        {"relation": "tracked-by", "reference": "github-pr:Alexis/Untaped#35"},
        {"relation": "tracked-by", "reference": "github-pr:alexis/untaped#35"},
    ]

    with pytest.raises(ValidationError, match="duplicate canonical evidence"):
        ActiveTask.model_validate(data)


def test_item_sorts_evidence_by_relation_and_canonical_reference() -> None:
    data = active_task_data()
    data["evidence"] = [
        {"relation": "verified-by", "reference": "url:https://example.com/z"},
        {"relation": "tracked-by", "reference": "github-pr:Owner/Repo#2"},
        {"relation": "tracked-by", "reference": "github-pr:Owner/Repo#1"},
    ]

    task = ActiveTask.model_validate(data)

    assert [(item.relation.value, item.reference.root) for item in task.evidence] == [
        ("tracked-by", "github-pr:owner/repo#1"),
        ("tracked-by", "github-pr:owner/repo#2"),
        ("verified-by", "url:https://example.com/z"),
    ]
