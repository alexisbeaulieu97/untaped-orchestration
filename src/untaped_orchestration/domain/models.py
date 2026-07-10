from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    field_validator,
    model_validator,
)

from untaped_orchestration.domain.evidence import Evidence, EvidenceReference
from untaped_orchestration.domain.ids import DecisionId, Slug, StoreId, TaskId
from untaped_orchestration.domain.time import CalendarDate, IanaTimezone, UtcTimestamp

type PositiveInt = Annotated[int, Field(strict=True, gt=0)]
type PositiveRank = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
type DecisionBodyLimit = Annotated[int, Field(strict=True, gt=0, le=4096)]
type TotalBodyLimit = Annotated[int, Field(strict=True, gt=0, le=16384)]
type SectionRowLimit = Annotated[int, Field(strict=True, gt=0, le=10)]
type BriefByteLimit = Annotated[int, Field(strict=True, ge=4096, le=32768)]


class ItemKind(StrEnum):
    TASK = "task"
    DECISION = "decision"


class Visibility(StrEnum):
    PRIVATE = "private"
    PUBLIC = "public"


class TaskStage(StrEnum):
    INBOX = "inbox"
    BACKLOG = "backlog"
    PLANNED = "planned"
    IN_PROGRESS = "in-progress"


class TaskPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class TaskOutcome(StrEnum):
    DELIVERED = "delivered"
    DECLINED = "declined"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class LinkRelation(StrEnum):
    DEPENDS_ON = "depends-on"
    GOVERNED_BY = "governed-by"
    SUPERSEDES = "supersedes"
    FOLLOW_UP_TO = "follow-up-to"


class ImportDestination(StrEnum):
    TASKS = "tasks"
    DECISIONS = "decisions"
    ARCHIVED_TASKS = "archive/tasks"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)


class StoreCapabilities(_FrozenModel):
    active_tasks: StrictBool


class CurationConfig(_FrozenModel):
    inbox_review_days: PositiveInt
    in_progress_review_days: PositiveInt


class BriefConfig(_FrozenModel):
    pinned_decisions: Annotated[tuple[DecisionId, ...], Field(max_length=10)]
    max_decision_body_bytes: DecisionBodyLimit
    max_total_body_bytes: TotalBodyLimit
    max_rows_per_section: SectionRowLimit
    max_total_bytes: BriefByteLimit

    @field_validator("pinned_decisions")
    @classmethod
    def _unique_pins(cls, values: tuple[DecisionId, ...]) -> tuple[DecisionId, ...]:
        roots = [value.root for value in values]
        if len(roots) != len(set(roots)):
            raise ValueError("pinned decision IDs must be unique")
        return values

    @model_validator(mode="after")
    def _decision_body_fits_total(self) -> BriefConfig:
        if self.max_decision_body_bytes > self.max_total_body_bytes:
            raise ValueError("decision body byte limit cannot exceed total body byte limit")
        return self


class StoreConfig(_FrozenModel):
    schema_: Literal["untaped.orchestration.store/v1"] = Field(alias="schema")
    id: StoreId
    name: str
    visibility: Visibility
    timezone: IanaTimezone
    capabilities: StoreCapabilities
    curation: CurationConfig
    brief: BriefConfig

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value.strip() or len(value) > 120:
            raise ValueError("store name must be nonempty and at most 120 characters")
        if any(character in value for character in ("\r", "\n", "\u2028", "\u2029")):
            raise ValueError("store name cannot contain line breaks")
        return value

    @model_validator(mode="after")
    def _enforce_public_policy(self) -> StoreConfig:
        if self.visibility is Visibility.PUBLIC and self.capabilities.active_tasks:
            raise ValueError("public stores cannot enable active tasks")
        return self


class RegistryChild(_FrozenModel):
    id: StoreId
    path: str

    @field_validator("path")
    @classmethod
    def _validate_posix_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or "\\" in value or path.is_absolute() or value == ".":
            raise ValueError("registry child path must be a relative POSIX path")
        return value


class Registry(_FrozenModel):
    schema_: Literal["untaped.orchestration.registry/v1"] = Field(alias="schema")
    store_id: StoreId
    children: tuple[RegistryChild, ...] = ()

    @field_validator("children")
    @classmethod
    def _sort_unique_children(cls, values: tuple[RegistryChild, ...]) -> tuple[RegistryChild, ...]:
        ids = [value.id.root for value in values]
        paths = [value.path for value in values]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("registry children must have unique IDs and paths")
        return tuple(sorted(values, key=lambda value: value.id.root))


class Link(_FrozenModel):
    relation: LinkRelation
    target_store_id: StoreId
    target: TaskId | DecisionId

    @model_validator(mode="after")
    def _validate_target_kind(self) -> Link:
        if self.relation in {LinkRelation.DEPENDS_ON, LinkRelation.FOLLOW_UP_TO} and not isinstance(
            self.target, TaskId
        ):
            raise ValueError(f"{self.relation.value} requires a task target")
        if self.relation is LinkRelation.GOVERNED_BY and not isinstance(self.target, DecisionId):
            raise ValueError("governed-by requires a decision target")
        return self


class _CommonItem(_FrozenModel):
    title: str
    created_at: UtcTimestamp
    tags: Annotated[tuple[Slug, ...], Field(max_length=32)]
    links: tuple[Link, ...] = ()
    evidence: tuple[Evidence, ...] = ()

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value.strip() or len(value) > 240:
            raise ValueError("item title must be nonempty and at most 240 characters")
        return value

    @field_validator("tags")
    @classmethod
    def _sort_unique_tags(cls, values: tuple[Slug, ...]) -> tuple[Slug, ...]:
        roots = [value.root for value in values]
        if len(roots) != len(set(roots)):
            raise ValueError("tags must be unique")
        return tuple(sorted(values, key=lambda value: value.root))

    @field_validator("links")
    @classmethod
    def _sort_unique_links(cls, values: tuple[Link, ...]) -> tuple[Link, ...]:
        keys = [
            (value.relation.value, value.target_store_id.root, value.target.root)
            for value in values
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("links must be unique")
        return tuple(
            sorted(
                values,
                key=lambda value: (
                    value.relation.value,
                    value.target_store_id.root,
                    value.target.root,
                ),
            )
        )

    @field_validator("evidence")
    @classmethod
    def _sort_unique_evidence(cls, values: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        keys = [(value.relation.value, value.reference.root) for value in values]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate canonical evidence")
        return tuple(sorted(values, key=lambda value: (value.relation.value, value.reference.root)))


class ActiveTask(_CommonItem):
    schema_: Literal["untaped.orchestration.task/v1"] = Field(alias="schema")
    id: TaskId
    kind: Literal[ItemKind.TASK]
    stage: TaskStage
    priority: TaskPriority
    rank: PositiveRank
    parent: TaskId | None = None
    started_at: UtcTimestamp | None = None
    revisit_when: str | None = None
    reviewed_at: UtcTimestamp | None = None
    review_on: CalendarDate | None = None
    waiting_on: Annotated[tuple[Slug, ...], Field(max_length=8)]

    @field_validator("revisit_when")
    @classmethod
    def _nonempty_revisit_trigger(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("revisit_when must be nonempty")
        return value

    @field_validator("waiting_on")
    @classmethod
    def _sort_unique_waiting_parties(cls, values: tuple[Slug, ...]) -> tuple[Slug, ...]:
        roots = [value.root for value in values]
        if len(roots) != len(set(roots)):
            raise ValueError("waiting parties must be unique")
        return tuple(sorted(values, key=lambda value: value.root))

    @model_validator(mode="after")
    def _validate_active_lifecycle_shape(self) -> ActiveTask:
        if self.stage is TaskStage.BACKLOG and self.revisit_when is None:
            raise ValueError("backlog tasks require revisit_when")
        if self.stage is not TaskStage.BACKLOG and self.revisit_when is not None:
            raise ValueError("revisit_when is forbidden outside backlog")
        if self.stage is TaskStage.IN_PROGRESS and self.started_at is None:
            raise ValueError("in-progress tasks require started_at")
        return self


class ArchivedTask(_CommonItem):
    schema_: Literal["untaped.orchestration.task/v1"] = Field(alias="schema")
    id: TaskId
    kind: Literal[ItemKind.TASK]
    priority: TaskPriority
    rank: PositiveRank
    parent: TaskId | None = None
    started_at: UtcTimestamp | None = None
    revisit_when: str | None = None
    reviewed_at: UtcTimestamp | None = None
    review_on: CalendarDate | None = None
    waiting_on: Annotated[tuple[Slug, ...], Field(max_length=8)]
    closed_from: TaskStage
    outcome: TaskOutcome
    closed_at: UtcTimestamp
    close_note: str

    @field_validator("revisit_when", "close_note")
    @classmethod
    def _nonempty_archive_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("archive lifecycle text must be nonempty")
        return value

    @field_validator("waiting_on")
    @classmethod
    def _sort_unique_waiting_parties(cls, values: tuple[Slug, ...]) -> tuple[Slug, ...]:
        roots = [value.root for value in values]
        if len(roots) != len(set(roots)):
            raise ValueError("waiting parties must be unique")
        return tuple(sorted(values, key=lambda value: value.root))

    @model_validator(mode="after")
    def _validate_archived_lifecycle_shape(self) -> ArchivedTask:
        if self.closed_from is TaskStage.BACKLOG and self.revisit_when is None:
            raise ValueError("tasks closed from backlog retain revisit_when")
        if self.closed_from is not TaskStage.BACKLOG and self.revisit_when is not None:
            raise ValueError("only tasks closed from backlog retain revisit_when")
        return self


class Decision(_CommonItem):
    schema_: Literal["untaped.orchestration.decision/v1"] = Field(alias="schema")
    id: DecisionId
    kind: Literal[ItemKind.DECISION]
    reviewed_at: UtcTimestamp | None = None
    review_on: CalendarDate | None = None
    retired_at: UtcTimestamp | None = None
    retire_note: str | None = None

    @model_validator(mode="after")
    def _validate_retirement_pair(self) -> Decision:
        if (self.retired_at is None) != (self.retire_note is None):
            raise ValueError("retired_at and retire_note must be present together")
        if self.retire_note is not None and not self.retire_note.strip():
            raise ValueError("retire_note must be nonempty")
        return self


class Revision(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _validate_revision(cls, value: str) -> str:
        prefix = "sha256:"
        payload = value.removeprefix(prefix)
        if (
            not value.startswith(prefix)
            or len(payload) != 64
            or any(character not in "0123456789abcdef" for character in payload)
        ):
            raise ValueError("expected sha256 revision with 64 lowercase hexadecimal characters")
        return value

    def __str__(self) -> str:
        return self.root


class ImportRecord(_FrozenModel):
    destination: ImportDestination
    frontmatter_file: str
    body_file: str
    source_ref: EvidenceReference

    @field_validator("frontmatter_file", "body_file")
    @classmethod
    def _validate_manifest_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or "\\" in value or path.is_absolute() or value == "." or ".." in path.parts:
            raise ValueError("import record files must be safe relative POSIX paths")
        return value


class ImportManifest(_FrozenModel):
    schema_: Literal["untaped.orchestration.import/v1"] = Field(alias="schema")
    target_store_id: StoreId
    expected_store_revision: Revision
    require_empty_items: StrictBool
    records: tuple[ImportRecord, ...]


type TaskRecord = ActiveTask | ArchivedTask
type ItemRecord = ActiveTask | ArchivedTask | Decision
