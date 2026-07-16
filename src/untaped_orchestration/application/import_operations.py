from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import ValidationError

from untaped_orchestration.application.item_support import (
    MutationScopeFactory,
    execute_mutation,
    validated_copy,
)
from untaped_orchestration.application.mutations import IntendedMutation, MutationExecutor
from untaped_orchestration.application.ports import (
    CanonicalFormatter,
    ExternalFileReader,
    FileDeletion,
    FileReplacement,
    StoreLocation,
    StoreReader,
    ViewRenderer,
)
from untaped_orchestration.application.results import (
    FederatedSnapshot,
    MutationReceipt,
)
from untaped_orchestration.application.validation import validate_snapshot
from untaped_orchestration.application.view_management import view_comparisons
from untaped_orchestration.domain.canonical import CanonicalItem
from untaped_orchestration.domain.diagnostics import (
    Diagnostic,
    DiagnosticError,
    expected_diagnostic,
)
from untaped_orchestration.domain.evidence import Evidence, EvidenceRelation
from untaped_orchestration.domain.ids import DecisionId, TaskId, item_filename
from untaped_orchestration.domain.limits import BODY_LIMIT, FRONTMATTER_LIMIT
from untaped_orchestration.domain.models import ImportManifest, Revision

ITEM_ROOTS = (
    PurePosixPath("tasks"),
    PurePosixPath("decisions"),
    PurePosixPath("archive/tasks"),
)


class ImportConflict(DiagnosticError):
    def __init__(
        self,
        message: str,
        diagnostics: tuple[Diagnostic, ...] | None = None,
    ) -> None:
        super().__init__(diagnostics or expected_diagnostic("ORC002", message))


def _prefixed_diagnostics(
    prefix: str,
    diagnostics: tuple[Diagnostic, ...],
) -> tuple[Diagnostic, ...]:
    return tuple(
        diagnostic.model_copy(update={"message": f"{prefix}: {diagnostic.message}"})
        for diagnostic in diagnostics
    )


class ImportRepository(StoreReader, CanonicalFormatter, Protocol):
    def parse_item_parts(
        self,
        frontmatter: bytes,
        body: bytes,
        *,
        relative_path: PurePosixPath,
    ) -> tuple[CanonicalItem, bytes]: ...


@dataclass(frozen=True, slots=True)
class ImportRequest:
    location: StoreLocation
    manifest: Path
    apply: bool = False
    if_clean: bool = False


@dataclass(frozen=True, slots=True)
class ImportedRecord:
    path: PurePosixPath
    revision: Revision
    content: bytes
    already_present: bool = False


@dataclass(frozen=True, slots=True)
class ImportResult:
    receipt: MutationReceipt
    base_revision: Revision
    records: tuple[ImportedRecord, ...]


def _manifest_record_file(
    reader: ExternalFileReader,
    root: Path,
    relative: str,
    *,
    limit: int,
    field: str,
) -> bytes:
    if root.is_symlink():
        raise ImportConflict("manifest record root cannot be a symlink")
    relative_path = PurePosixPath(relative)
    candidate = root.joinpath(*relative_path.parts)
    try:
        base = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (FileNotFoundError, ValueError, OSError) as error:
        raise ImportConflict("manifest record path is unsafe or missing") from error
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ImportConflict("manifest record path cannot contain symlinks")
        current = current.parent
    try:
        return reader.read_external(candidate, limit=limit, field=field)
    except DiagnosticError as error:
        raise ImportConflict(
            "manifest record cannot be read",
            _prefixed_diagnostics("manifest record cannot be read", error.diagnostics),
        ) from error
    except (OSError, ValueError) as error:
        raise ImportConflict("manifest record path must be a regular nonsymlink file") from error


def _load_manifest(request: ImportRequest, reader: ExternalFileReader) -> ImportManifest:
    try:
        raw = reader.read_external(
            request.manifest,
            limit=FRONTMATTER_LIMIT,
            field="manifest",
        )
    except DiagnosticError as error:
        raise ImportConflict(
            "manifest does not match untaped.orchestration.import/v1",
            _prefixed_diagnostics("import manifest cannot be read", error.diagnostics),
        ) from error
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise ImportConflict(
            "manifest does not match untaped.orchestration.import/v1",
            expected_diagnostic(
                "ORC001",
                "import manifest is not valid UTF-8",
                path=request.manifest.as_posix(),
                field="manifest",
            ),
        ) from error
    try:
        return ImportManifest.model_validate(tomllib.loads(text))
    except (OSError, tomllib.TOMLDecodeError, ValidationError, ValueError) as error:
        raise ImportConflict("manifest does not match untaped.orchestration.import/v1") from error


def _item_identity(
    frontmatter: bytes,
    *,
    path: Path,
) -> tuple[TaskId | DecisionId, str]:
    try:
        text = frontmatter.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ImportConflict(
            "manifest front matter is not valid UTF-8",
            expected_diagnostic(
                "ORC001",
                "manifest record front matter is not valid UTF-8",
                path=path.absolute().as_posix(),
                field="frontmatter",
            ),
        ) from error
    try:
        values = tomllib.loads(text)
        raw_id = values["id"]
        title = values["title"]
        if not isinstance(raw_id, str) or not isinstance(title, str):
            raise TypeError
        if raw_id.startswith("tsk_"):
            return TaskId(raw_id), title
        return DecisionId(raw_id), title
    except (tomllib.TOMLDecodeError, KeyError, TypeError, ValidationError) as error:
        raise ImportConflict("manifest front matter is not a complete canonical item") from error


def _is_item_path(path: PurePosixPath) -> bool:
    return any(path.parts[:-1] == root.parts for root in ITEM_ROOTS)


@dataclass(slots=True)
class _ImportExecution:
    repository: ImportRepository
    executor: MutationExecutor
    views: ViewRenderer
    request: ImportRequest
    manifest: ImportManifest
    records: tuple[ImportedRecord, ...]
    present: set[PurePosixPath]
    current_checked: bool = False

    def validator(self, snapshot: FederatedSnapshot) -> tuple[Diagnostic, ...]:
        if not self.current_checked:
            return ()
        return validate_snapshot(snapshot, require_children=True)

    def guard(self, current: FederatedSnapshot) -> None:
        selected = current.selected
        if selected.store is None or selected.store.id != self.manifest.target_store_id:
            raise ImportConflict(
                "manifest target_store_id does not match selected store",
                expected_diagnostic(
                    "ORC003",
                    "manifest target_store_id does not match selected store",
                    field="target_store_id",
                ),
            )
        expected = {record.path: record for record in self.records}
        for entry in self.repository.list_entries(self.request.location):
            if entry.kind != "file" or not _is_item_path(entry.path):
                continue
            planned = expected.get(entry.path)
            if planned is None:
                if self.manifest.require_empty_items:
                    raise ImportConflict(
                        f"unexpected item blocks clean import: {entry.path.as_posix()}"
                    )
                continue
            actual = self.repository.read_file(self.request.location, entry.path).content
            if actual != planned.content:
                raise ImportConflict(f"divergent manifest destination: {entry.path.as_posix()}")
            self.present.add(entry.path)
        reconstructed = self.executor.project(
            current,
            (),
            tuple(FileDeletion(path) for path in sorted(self.present)),
        ).snapshot
        base_diagnostics = validate_snapshot(reconstructed, require_children=True)
        if any(value.severity == "error" for value in base_diagnostics):
            raise ImportConflict("reconstructed pre-import store is invalid", base_diagnostics)
        if reconstructed.selected.store_revision != self.manifest.expected_store_revision:
            raise ImportConflict(
                "expected_store_revision does not match reconstructed base revision",
                expected_diagnostic(
                    "ORC007",
                    "expected_store_revision does not match reconstructed base revision",
                    field="expected_store_revision",
                ),
            )
        if self.manifest.require_empty_items and not self.present:
            _, managed = view_comparisons(
                self.repository,
                self.request.location,
                self.views,
                selected,
            )
            if not all(managed.values()):
                raise ImportConflict("--if-clean requires current views")
        self.current_checked = True

    def build(self, current: FederatedSnapshot) -> IntendedMutation:
        del current
        return IntendedMutation(
            replacements=tuple(
                FileReplacement(record.path, record.content)
                for record in self.records
                if record.path not in self.present
            )
        )


class ImportService:
    def __init__(
        self,
        repository: ImportRepository,
        executor: MutationExecutor,
        views: ViewRenderer,
        *,
        external_files: ExternalFileReader,
        scope_factory: MutationScopeFactory,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._views = views
        self._external_files = external_files
        self._scope_factory = scope_factory

    def _records(
        self,
        request: ImportRequest,
        manifest: ImportManifest,
    ) -> tuple[ImportedRecord, ...]:
        imported: list[ImportedRecord] = []
        seen: set[PurePosixPath] = set()
        for entry in manifest.records:
            frontmatter = _manifest_record_file(
                self._external_files,
                request.manifest.parent,
                entry.frontmatter_file,
                limit=FRONTMATTER_LIMIT,
                field="frontmatter",
            )
            body = _manifest_record_file(
                self._external_files,
                request.manifest.parent,
                entry.body_file,
                limit=BODY_LIMIT,
                field="body",
            )
            frontmatter_path = request.manifest.parent.joinpath(
                *PurePosixPath(entry.frontmatter_file).parts
            )
            item_id, title = _item_identity(frontmatter, path=frontmatter_path)
            path = PurePosixPath(entry.destination.value) / item_filename(item_id, title)
            if path in seen:
                raise ImportConflict(f"manifest destination collision: {path.as_posix()}")
            seen.add(path)
            try:
                metadata, parsed_body = self._repository.parse_item_parts(
                    frontmatter,
                    body,
                    relative_path=path,
                )
                provenance = Evidence(
                    relation=EvidenceRelation.TRACKED_BY,
                    reference=entry.source_ref,
                )
                conflicting_provenance = tuple(
                    evidence
                    for evidence in metadata.evidence
                    if evidence.reference == entry.source_ref
                    and evidence.relation is not EvidenceRelation.TRACKED_BY
                )
                if conflicting_provenance:
                    raise ImportConflict(
                        "manifest source_ref already exists under a different evidence relation"
                    )
                if provenance not in metadata.evidence:
                    metadata = validated_copy(
                        metadata,
                        {"evidence": (*metadata.evidence, provenance)},
                    )
                content = self._repository.item_bytes(metadata, parsed_body)
            except ImportConflict:
                raise
            except DiagnosticError as error:
                raise ImportConflict(
                    f"invalid manifest record: {path.as_posix()}",
                    error.diagnostics,
                ) from error
            except (ValidationError, ValueError) as error:
                raise ImportConflict(f"invalid manifest record: {path.as_posix()}") from error
            revision = Revision(f"sha256:{sha256(content).hexdigest()}")
            imported.append(ImportedRecord(path, revision, content))
        return tuple(imported)

    def execute(self, request: ImportRequest) -> ImportResult:
        manifest = _load_manifest(request, self._external_files)
        if request.apply and manifest.require_empty_items and not request.if_clean:
            raise ImportConflict("import apply requires explicit --if-clean")
        records = self._records(request, manifest)
        operation = _ImportExecution(
            self._repository,
            self._executor,
            self._views,
            request,
            manifest,
            records,
            set(),
        )
        receipt = execute_mutation(
            self._executor,
            self._scope_factory,
            operation.guard,
            operation.build,
            validator=operation.validator,
            dry_run=not request.apply,
        )
        reported = tuple(
            replace(record, already_present=record.path in operation.present) for record in records
        )
        if not request.apply:
            receipt = replace(receipt, intended_paths=tuple(record.path for record in records))
        return ImportResult(receipt, manifest.expected_store_revision, reported)
