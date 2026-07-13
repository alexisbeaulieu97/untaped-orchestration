from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol

from untaped_orchestration.application.item_support import ItemMutationResult
from untaped_orchestration.application.ports import (
    CanonicalFormatter,
    FileReplacement,
    LockManager,
    MutationProjector,
    StoreLocation,
    StoreReader,
    StoreWriter,
    UnprovableBodyBoundary,
    ViewRenderer,
)
from untaped_orchestration.application.results import (
    Completeness,
    FederatedSnapshot,
    ItemRevision,
    MutationReceipt,
    StoreSnapshot,
)
from untaped_orchestration.application.tasks import RepairDuplicateRequest
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.application.view_management import apply_views
from untaped_orchestration.domain.diagnostics import (
    Diagnostic,
    DiagnosticError,
    expected_diagnostic,
)
from untaped_orchestration.domain.models import Revision

DEFAULT_LOCK_TIMEOUT = 10.0


class RepairConflict(DiagnosticError):
    def __init__(
        self,
        message: str,
        diagnostics: tuple[Diagnostic, ...] | None = None,
    ) -> None:
        super().__init__(diagnostics or expected_diagnostic("ORC002", message))


class RepairRepository(
    StoreReader,
    StoreWriter,
    CanonicalFormatter,
    MutationProjector,
    Protocol,
):
    def repaired_item_bytes(
        self,
        *,
        relative_path: PurePosixPath,
        current: bytes,
        replacement_frontmatter: bytes,
        replacement_body: bytes | None,
    ) -> bytes: ...


class DuplicateRepair(Protocol):
    def repair_duplicate(self, request: RepairDuplicateRequest) -> ItemMutationResult: ...


@dataclass(frozen=True, slots=True)
class RepairFrontmatterRequest:
    location: StoreLocation
    path: PurePosixPath
    frontmatter_file: Path
    expected_revision: Revision
    body_file: Path | None = None
    apply: bool = False


@dataclass(frozen=True, slots=True)
class RepairFrontmatterResult:
    receipt: MutationReceipt
    before: bytes
    after: bytes


def _regular_external_file(path: Path, *, label: str) -> bytes:
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular nonsymlink file")
    return path.read_bytes()


def _federated(snapshot: StoreSnapshot) -> FederatedSnapshot:
    return FederatedSnapshot(snapshot, (snapshot,), Completeness())


def _receipt(
    snapshot: StoreSnapshot,
    *,
    intended: tuple[PurePosixPath, ...],
    changed: tuple[PurePosixPath, ...],
    canonical_applied: bool,
    views_current: bool,
) -> MutationReceipt:
    return MutationReceipt(
        applied=bool(changed),
        replayed=False,
        canonical_applied=canonical_applied,
        views_current=views_current,
        intended_paths=intended,
        changed_paths=changed,
        item_revisions=tuple(
            ItemRevision(value.path, value.revision) for value in snapshot.records
        ),
        store_revision=snapshot.store_revision,
        registry_revision=snapshot.registry_revision,
    )


class RepairService:
    def __init__(
        self,
        repository: RepairRepository,
        writer: StoreWriter,
        locks: LockManager,
        views: ViewRenderer,
        *,
        duplicate_repair: DuplicateRepair | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._repository = repository
        self._writer = writer
        self._locks = locks
        self._views = views
        self._duplicate_repair = duplicate_repair
        self._lock_timeout = lock_timeout

    def duplicate(self, request: RepairDuplicateRequest) -> ItemMutationResult:
        if self._duplicate_repair is None:
            raise RuntimeError("duplicate repair service was not configured")
        return self._duplicate_repair.repair_duplicate(request)

    def frontmatter(self, request: RepairFrontmatterRequest) -> RepairFrontmatterResult:
        with self._locks.acquire((request.location,), timeout=self._lock_timeout):
            before = self._repository.read_raw(request.location, request.path).content
            if Revision(f"sha256:{sha256(before).hexdigest()}") != request.expected_revision:
                raise RepairConflict(
                    "item revision guard does not match current revision",
                    expected_diagnostic(
                        "ORC007",
                        "item revision guard does not match current revision",
                        path=request.path.as_posix(),
                        field="revision",
                    ),
                )
            after = self._planned_repair(request, before)
            current = self._repository.load_local(request.location, headers_only=False)
            projected = self._repository.project(
                _federated(current),
                request.location,
                (FileReplacement(request.path, after),),
                (),
            )
            diagnostics = validate_snapshot(projected.snapshot, require_children=True)
            if any(value.severity == "error" for value in diagnostics):
                raise RepairConflict("repair would leave an invalid store", diagnostics)
            return self._finish(request, before, after, projected.snapshot.selected)

    def _planned_repair(self, request: RepairFrontmatterRequest, before: bytes) -> bytes:
        try:
            replacement = _regular_external_file(
                request.frontmatter_file,
                label="frontmatter file",
            )
            body = (
                _regular_external_file(request.body_file, label="body file")
                if request.body_file is not None
                else None
            )
            return self._repository.repaired_item_bytes(
                relative_path=request.path,
                current=before,
                replacement_frontmatter=replacement,
                replacement_body=body,
            )
        except DiagnosticError as error:
            raise RepairConflict(
                "replacement front matter or body is invalid",
                error.diagnostics,
            ) from error
        except (OSError, ValueError) as error:
            if isinstance(error, UnprovableBodyBoundary):
                raise RepairConflict(
                    "body boundary is unprovable; provide --body-file",
                    expected_diagnostic(
                        "ORC001",
                        "body boundary is unprovable; provide --body-file",
                        path=request.path.as_posix(),
                        field="body",
                    ),
                ) from error
            raise RepairConflict("replacement front matter or body is invalid") from error

    def _finish(
        self,
        request: RepairFrontmatterRequest,
        before: bytes,
        after: bytes,
        projected: StoreSnapshot,
    ) -> RepairFrontmatterResult:
        changed: list[PurePosixPath] = []
        if request.apply and after != before:
            self._writer.replace(request.location, FileReplacement(request.path, after))
            changed.append(request.path)
        selected_after = (
            self._repository.load_local(request.location, headers_only=False)
            if changed
            else projected
        )
        intended: tuple[PurePosixPath, ...] = (request.path,)
        views_current = False
        if request.apply:
            view_state = apply_views(
                self._repository,
                self._writer,
                request.location,
                self._views,
                selected_after,
            )
            changed.extend(view_state.changed_paths)
            intended = (request.path, *view_state.intended_paths)
            views_current = view_state.current
        snapshot_for_receipt = (
            selected_after
            if request.apply
            else self._repository.load_local(request.location, headers_only=False)
        )
        receipt = _receipt(
            snapshot_for_receipt,
            intended=intended,
            changed=tuple(changed),
            canonical_applied=request.apply and after != before,
            views_current=views_current,
        )
        return RepairFrontmatterResult(receipt, before, after)
