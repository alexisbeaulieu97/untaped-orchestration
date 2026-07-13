from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomli_w
from pydantic import ValidationError

from untaped_orchestration.application.ports import (
    FileDeletion,
    FileReplacement,
    LockManager,
    StoreReader,
    StoreWriter,
    ViewRenderer,
)
from untaped_orchestration.application.results import MutationReceipt, StoreSnapshot
from untaped_orchestration.application.scaffold import (
    AGENTS_BYTES,
    AGENTS_PATH,
    CLAUDE_BYTES,
    CLAUDE_PATH,
    REGISTRY_PATH,
    STORE_PATH,
)
from untaped_orchestration.domain.diagnostics import (
    DiagnosticError,
    expected_diagnostic,
    validation_diagnostic,
)
from untaped_orchestration.domain.models import Registry, Revision, StoreConfig

DEFAULT_LOCK_TIMEOUT = 10.0
_IGNORED_NAMES = frozenset({".lock", ".DS_Store"})


class InitConflictError(DiagnosticError):
    def __init__(self, message: str) -> None:
        super().__init__(expected_diagnostic("ORC003", message, field="path"))


class InitSchemaError(DiagnosticError):
    def __init__(self, error: ValidationError) -> None:
        super().__init__(
            validation_diagnostic(error, "ORC002", message_prefix="invalid store metadata")
        )


@dataclass(frozen=True, slots=True)
class InitRequest:
    target: Path
    store_id: str
    name: str
    timezone: str
    public: bool = False
    decisions_only: bool = False


def _initial_models(request: InitRequest) -> tuple[StoreConfig, Registry]:
    try:
        config = StoreConfig.model_validate(
            {
                "schema": "untaped.orchestration.store/v1",
                "id": request.store_id,
                "name": request.name,
                "visibility": "public" if request.public else "private",
                "timezone": request.timezone,
                "capabilities": {"active_tasks": not (request.public or request.decisions_only)},
                "curation": {"inbox_review_days": 7, "in_progress_review_days": 14},
                "brief": {
                    "pinned_decisions": [],
                    "max_decision_body_bytes": 4096,
                    "max_total_body_bytes": 16384,
                    "max_rows_per_section": 10,
                    "max_total_bytes": 32768,
                },
            }
        )
        registry = Registry.model_validate(
            {
                "schema": "untaped.orchestration.registry/v1",
                "store_id": request.store_id,
                "children": [],
            }
        )
    except ValidationError as error:
        raise InitSchemaError(error) from error
    return config, registry


def _canonical_toml(model: StoreConfig | Registry) -> bytes:
    table = model.model_dump(by_alias=True, mode="json", exclude_none=True)
    if isinstance(model, Registry) and not model.children:
        table.pop("children")
    return tomli_w.dumps(table).encode()


def _revision(raw: bytes) -> Revision:
    return Revision(f"sha256:{hashlib.sha256(raw).hexdigest()}")


def _store_revision(files: dict[PurePosixPath, bytes]) -> Revision:
    digest = hashlib.sha256()
    for path, raw in sorted(files.items(), key=lambda pair: pair[0].as_posix()):
        path_bytes = path.as_posix().encode()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(bytes.fromhex(_revision(raw).root.removeprefix("sha256:")))
    return Revision(f"sha256:{digest.hexdigest()}")


def _ignored(path: PurePosixPath) -> bool:
    name = path.name
    return (
        name in _IGNORED_NAMES
        or (name.startswith(".") and ".untaped-tmp-" in name)
        or name.endswith(("~", ".swp", ".swo", ".tmp"))
        or name.startswith((".#", "#"))
    )


def _temporary_target(
    path: PurePosixPath, expected: dict[PurePosixPath, bytes]
) -> PurePosixPath | None:
    for target in expected:
        prefix = f".{target.name}.untaped-tmp-"
        if path.parent == target.parent and path.name.startswith(prefix):
            return target
    return None


class InitializeStore:
    def __init__(
        self,
        reader: StoreReader,
        writer: StoreWriter,
        locks: LockManager,
        views: ViewRenderer,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        if lock_timeout < 0:
            raise ValueError("lock timeout must be nonnegative")
        self._reader = reader
        self._writer = writer
        self._locks = locks
        self._views = views
        self._lock_timeout = lock_timeout

    def execute(self, request: InitRequest) -> MutationReceipt:
        if request.public and request.decisions_only:
            raise ValueError("--public and --decisions-only are mutually exclusive")
        config, registry = _initial_models(request)
        root = request.target / ".untaped" / "orchestration"
        location = self._writer.prepare(root)
        canonical = {
            STORE_PATH: _canonical_toml(config),
            REGISTRY_PATH: _canonical_toml(registry),
            AGENTS_PATH: AGENTS_BYTES,
            CLAUDE_PATH: CLAUDE_BYTES,
        }
        snapshot = StoreSnapshot(
            location=location,
            store=config,
            registry=registry,
            records=(),
            load_diagnostics=(),
            raw_index=(),
            store_revision=_store_revision(canonical),
            registry_revision=_revision(canonical[REGISTRY_PATH]),
            store_config_revision=_revision(canonical[STORE_PATH]),
        )
        expected = {**canonical, **self._views.expected(snapshot)}
        ordered = tuple(expected)

        with self._locks.acquire((location,), timeout=self._lock_timeout):
            entries = self._reader.list_entries(location)
            anchored = any(entry.path == STORE_PATH for entry in entries)
            temporaries = tuple(entry for entry in entries if ".untaped-tmp-" in entry.path.name)
            for entry in temporaries:
                target = _temporary_target(entry.path, expected)
                if (
                    anchored
                    or entry.kind != "file"
                    or target is None
                    or self._reader.read_file(location, entry.path).content != expected[target]
                ):
                    raise InitConflictError(
                        f"unrelated or divergent init temporary: {entry.path.as_posix()}"
                    )
                self._writer.delete(location, FileDeletion(entry.path))
            if temporaries:
                entries = self._reader.list_entries(location)
            unsafe = next(
                (
                    entry
                    for entry in entries
                    if entry.kind not in {"file", "directory"}
                    or (entry.kind == "directory" and entry.path != PurePosixPath("views"))
                ),
                None,
            )
            if unsafe is not None:
                raise InitConflictError(
                    f"unsafe or unexpected store entry: {unsafe.path.as_posix()} ({unsafe.kind})"
                )
            present = tuple(
                entry.path for entry in entries if entry.kind == "file" and not _ignored(entry.path)
            )
            admin_prefix_complete = set(canonical) <= set(present)
            if any(
                entry.kind == "directory"
                and (entry.path != PurePosixPath("views") or not admin_prefix_complete)
                for entry in entries
            ):
                raise InitConflictError("unexpected directory blocks init recovery")
            unexpected = next((path for path in present if path not in expected), None)
            if unexpected is not None:
                raise InitConflictError(f"unexpected file blocks init: {unexpected.as_posix()}")
            for path in present:
                if self._reader.read_file(location, path).content != expected[path]:
                    raise InitConflictError(f"divergent scaffold file: {path.as_posix()}")
            prefix = ordered[: len(present)]
            if set(present) != set(prefix):
                raise InitConflictError("existing scaffold is not an exact anchored prefix")

            changed: list[PurePosixPath] = []
            for path in ordered[len(present) :]:
                self._writer.replace(location, FileReplacement(path, expected[path]))
                changed.append(path)

            after = self._reader.load_local(location, headers_only=False)
            complete_replay = len(present) == len(ordered)
            return MutationReceipt(
                applied=True,
                replayed=complete_replay,
                canonical_applied=True,
                views_current=True,
                intended_paths=ordered,
                changed_paths=tuple(changed),
                item_revisions=(),
                store_revision=after.store_revision,
                registry_revision=after.registry_revision,
            )
