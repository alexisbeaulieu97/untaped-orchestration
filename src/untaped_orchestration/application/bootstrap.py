from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomli_w

from untaped_orchestration.application.ports import (
    FileReplacement,
    LockManager,
    StoreReader,
    StoreWriter,
    ViewRenderer,
)
from untaped_orchestration.application.results import MutationReceipt, StoreSnapshot
from untaped_orchestration.domain.models import Registry, Revision, StoreConfig

DEFAULT_LOCK_TIMEOUT = 10.0
_AGENTS = b"""# Local orchestration store

Use `untaped-orchestration` for all canonical reads and writes. Do not read generated
views as tool input. Keep unfinished tasks private, preserve revision guards, and get
explicit approval before pushes, merges, releases, publications, or external changes.
"""
_IGNORED_NAMES = frozenset({".lock", ".DS_Store"})


class InitConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InitRequest:
    target: Path
    store_id: str
    name: str
    timezone: str
    public: bool = False
    decisions_only: bool = False


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
        root = request.target / ".untaped" / "orchestration"
        location = self._writer.prepare(root)
        canonical = {
            PurePosixPath("store.toml"): _canonical_toml(config),
            PurePosixPath("registry.toml"): _canonical_toml(registry),
            PurePosixPath("AGENTS.md"): _AGENTS,
            PurePosixPath("CLAUDE.md"): b"@AGENTS.md\n",
        }
        snapshot = StoreSnapshot(
            location=location,
            store=config,
            registry=registry,
            records=(),
            load_diagnostics=(),
            raw_index=(),
            store_revision=_store_revision(canonical),
            registry_revision=_revision(canonical[PurePosixPath("registry.toml")]),
            store_config_revision=_revision(canonical[PurePosixPath("store.toml")]),
        )
        expected = {**canonical, **self._views.expected(snapshot)}
        ordered = tuple(expected)

        with self._locks.acquire((location,), timeout=self._lock_timeout):
            present = tuple(
                path for path in self._reader.list_files(location) if not _ignored(path)
            )
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
