from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol

from untaped_orchestration.application.item_support import (
    ItemMutationResult,
    MutationScope,
    execute_mutation,
)
from untaped_orchestration.application.mutations import IntendedMutation, MutationExecutor
from untaped_orchestration.application.ports import (
    ExternalFileReader,
    FileReplacement,
    StoreLocation,
    StoreReader,
    UnprovableBodyBoundary,
)
from untaped_orchestration.application.results import (
    FederatedSnapshot,
    MutationReceipt,
)
from untaped_orchestration.application.tasks import RepairDuplicateRequest
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.domain.diagnostics import (
    Diagnostic,
    DiagnosticError,
    expected_diagnostic,
)
from untaped_orchestration.domain.limits import BODY_LIMIT, FRONTMATTER_LIMIT
from untaped_orchestration.domain.models import Revision


class RepairConflict(DiagnosticError):
    def __init__(
        self,
        message: str,
        diagnostics: tuple[Diagnostic, ...] | None = None,
    ) -> None:
        super().__init__(diagnostics or expected_diagnostic("ORC002", message))


class RepairRepository(StoreReader, Protocol):
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


@dataclass(slots=True)
class _PlannedRepair:
    before: bytes | None = None
    after: bytes | None = None


class RepairService:
    def __init__(
        self,
        repository: RepairRepository,
        executor: MutationExecutor,
        scope: MutationScope,
        *,
        external_files: ExternalFileReader,
        duplicate_repair: DuplicateRepair | None = None,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._scope = scope
        self._external_files = external_files
        self._duplicate_repair = duplicate_repair

    def duplicate(self, request: RepairDuplicateRequest) -> ItemMutationResult:
        if self._duplicate_repair is None:
            raise RuntimeError("duplicate repair service was not configured")
        return self._duplicate_repair.repair_duplicate(request)

    def frontmatter(self, request: RepairFrontmatterRequest) -> RepairFrontmatterResult:
        replacement, body = self._replacement_inputs(request)
        planned = _PlannedRepair()

        def current_validator(snapshot: FederatedSnapshot) -> tuple[Diagnostic, ...]:
            selected = replace(
                snapshot.selected,
                load_diagnostics=tuple(
                    diagnostic
                    for diagnostic in snapshot.selected.load_diagnostics
                    if diagnostic.path != request.path.as_posix()
                ),
            )
            stores = tuple(
                selected if store.location.real_root == selected.location.real_root else store
                for store in snapshot.stores
            )
            return validate_snapshot(
                replace(snapshot, selected=selected, stores=stores),
                require_children=False,
            )

        def guard(snapshot: FederatedSnapshot) -> None:
            if snapshot.selected.location.real_root != request.location.real_root:
                raise RepairConflict(
                    "repair location does not match the selected mutation store",
                    expected_diagnostic(
                        "ORC007",
                        "repair location does not match the selected mutation store",
                        path=request.path.as_posix(),
                        field="store",
                    ),
                )
            raw = self._repository.read_raw(request.location, request.path)
            if raw.revision != request.expected_revision:
                raise RepairConflict(
                    "item revision guard does not match current revision",
                    expected_diagnostic(
                        "ORC007",
                        "item revision guard does not match current revision",
                        path=request.path.as_posix(),
                        field="revision",
                    ),
                )
            planned.before = raw.content

        def build(snapshot: FederatedSnapshot) -> IntendedMutation:
            del snapshot
            assert planned.before is not None
            planned.after = self._planned_repair(
                request,
                planned.before,
                replacement,
                body,
            )
            return IntendedMutation(replacements=(FileReplacement(request.path, planned.after),))

        receipt = execute_mutation(
            self._executor,
            self._scope.recursive,
            guard,
            build,
            current_validator=current_validator,
            projected_validator=lambda snapshot: validate_snapshot(
                snapshot,
                require_children=False,
            ),
            dry_run=not request.apply,
        )
        assert planned.before is not None and planned.after is not None
        return RepairFrontmatterResult(receipt, planned.before, planned.after)

    def _replacement_inputs(
        self,
        request: RepairFrontmatterRequest,
    ) -> tuple[bytes, bytes | None]:
        try:
            replacement = self._external_files.read_external(
                request.frontmatter_file,
                limit=FRONTMATTER_LIMIT,
                field="frontmatter",
            )
            body = (
                self._external_files.read_external(
                    request.body_file,
                    limit=BODY_LIMIT,
                    field="body",
                )
                if request.body_file is not None
                else None
            )
            return replacement, body
        except DiagnosticError as error:
            raise RepairConflict(
                "replacement front matter or body is invalid",
                error.diagnostics,
            ) from error
        except (OSError, ValueError) as error:
            raise RepairConflict("replacement front matter or body is invalid") from error

    def _planned_repair(
        self,
        request: RepairFrontmatterRequest,
        before: bytes,
        replacement: bytes,
        body: bytes | None,
    ) -> bytes:
        try:
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
