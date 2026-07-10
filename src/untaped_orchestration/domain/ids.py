import re
import unicodedata
from typing import Literal, Self
from uuid import RFC_4122, UUID

from pydantic import ConfigDict, RootModel, field_validator

IdPrefix = Literal["sto", "tsk", "dec"]
ID_RE = re.compile(r"(?P<prefix>sto|tsk|dec)_(?P<hex>[0-9a-f]{32})")
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _validated_id(value: str, *, prefix: IdPrefix) -> str:
    match = ID_RE.fullmatch(value)
    if match is None or match.group("prefix") != prefix:
        raise ValueError(f"expected {prefix}_ UUIDv7 identifier")
    parsed = UUID(hex=match.group("hex"))
    if parsed.version != 7 or parsed.variant != RFC_4122:
        raise ValueError("identifier payload must be an RFC 4122 UUIDv7")
    return value


class TypedId(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @classmethod
    def parse(cls, value: str, *, prefix: IdPrefix) -> Self:
        return cls(_validated_id(value, prefix=prefix))

    def __str__(self) -> str:
        return self.root


class StoreId(TypedId):
    @field_validator("root")
    @classmethod
    def _validate_store_id(cls, value: str) -> str:
        return _validated_id(value, prefix="sto")


class TaskId(TypedId):
    @field_validator("root")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        return _validated_id(value, prefix="tsk")


class DecisionId(TypedId):
    @field_validator("root")
    @classmethod
    def _validate_decision_id(cls, value: str) -> str:
        return _validated_id(value, prefix="dec")


class Slug(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        if len(value) > 64 or SLUG_RE.fullmatch(value) is None:
            raise ValueError("expected a lowercase slug of at most 64 characters")
        return value

    def __str__(self) -> str:
        return self.root


def creation_slug(title: str) -> str:
    ascii_title = (
        unicodedata.normalize("NFKD", title).encode("ascii", errors="ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    slug = slug[:64].rstrip("-")
    return slug or "item"


def item_filename_prefix(item_id: TaskId | DecisionId) -> str:
    if not isinstance(item_id, (TaskId, DecisionId)):
        raise TypeError("filename identity must be a task or decision ID")
    return f"{item_id.root}-"


def item_filename(item_id: TaskId | DecisionId, title: str) -> str:
    return f"{item_filename_prefix(item_id)}{creation_slug(title)}.md"
