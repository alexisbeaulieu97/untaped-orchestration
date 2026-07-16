from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID, STORE_ID, TASK_ID, decision_bytes, task_bytes
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.item_support import MutationExecutionScope
from untaped_orchestration.application.maintenance import (
    ImportConflict,
    ImportRequest,
    ImportService,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.domain.evidence import EvidenceRelation
from untaped_orchestration.domain.limits import BODY_LIMIT, FRONTMATTER_LIMIT
from untaped_orchestration.infrastructure.external_files import FilesystemExternalFileReader
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def _frontmatter(raw: bytes) -> bytes:
    return raw.split(b"+++\n", 2)[1]


def _body(raw: bytes) -> bytes:
    return raw.split(b"+++\n", 2)[2]


def _fixture(
    tmp_path: Path,
    *,
    public: bool = False,
    decisions_only: bool = False,
    external_files=None,
    scope_calls: list[str] | None = None,
):
    target = tmp_path / "repository"
    target.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(
            target,
            STORE_ID,
            "Local",
            "UTC",
            public=public,
            decisions_only=decisions_only,
        )
    )
    location = location_from_root(target / ".untaped" / "orchestration")

    def load() -> FederatedSnapshot:
        selected = repository.load_local(location, headers_only=False)
        return FederatedSnapshot(selected, (selected,), Completeness())

    def scope_factory() -> MutationExecutionScope:
        if scope_calls is not None:
            scope_calls.append("recursive")
        return MutationExecutionScope((location,), location, load)

    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    service = ImportService(
        repository,
        executor,
        views,
        external_files=external_files or FilesystemExternalFileReader(),
        scope_factory=scope_factory,
    )
    return repository, location, service


def _manifest(tmp_path: Path, revision: str, **changes: object) -> Path:
    records = tmp_path / "records"
    records.mkdir(exist_ok=True)
    raw = decision_bytes().replace(
        b"Use TOML front matter and opaque Markdown bodies",
        "Crème brûlée / Import Contract".encode(),
    )
    records.joinpath("decision.toml").write_bytes(_frontmatter(raw))
    records.joinpath("decision.md").write_bytes(_body(raw))
    values = {
        "schema": "untaped.orchestration.import/v1",
        "target_store_id": STORE_ID,
        "expected_store_revision": revision,
        "require_empty_items": True,
    }
    values.update(changes)
    manifest = tmp_path / "import.toml"
    manifest.write_text(
        f'''schema = "{values["schema"]}"
target_store_id = "{values["target_store_id"]}"
expected_store_revision = "{values["expected_store_revision"]}"
require_empty_items = {str(values["require_empty_items"]).lower()}

[[records]]
destination = "decisions"
frontmatter_file = "records/decision.toml"
body_file = "records/decision.md"
source_ref = "git:abc123:orchestration/DECISIONS.md#sha256:{"a" * 64}"
''',
        encoding="utf-8",
    )
    return manifest


def _append_decision_record(
    tmp_path: Path,
    manifest: Path,
    *,
    item_id: str = "dec_019f0000000070008000000000000002",
    source_ref: str | None = None,
) -> PurePosixPath:
    raw = (
        decision_bytes()
        .replace(DECISION_ID.encode(), item_id.encode())
        .replace(
            b"Use TOML front matter and opaque Markdown bodies",
            b"Second imported decision",
        )
    )
    records = tmp_path / "records"
    records.joinpath("decision-2.toml").write_bytes(_frontmatter(raw))
    records.joinpath("decision-2.md").write_bytes(_body(raw))
    reference = source_ref or f"git:def456:DECISIONS.md#sha256:{'b' * 64}"
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(
            f'''\n[[records]]
destination = "decisions"
frontmatter_file = "records/decision-2.toml"
body_file = "records/decision-2.md"
source_ref = "{reference}"
'''
        )
    return PurePosixPath(f"decisions/{item_id}-second-imported-decision.md")


def test_manifest_is_the_only_revision_authority_and_import_defaults_to_dry_run(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)

    result = service.execute(ImportRequest(location, manifest))

    destination = PurePosixPath(f"decisions/{DECISION_ID}-creme-brulee-import-contract.md")
    assert result.receipt.applied is False
    assert result.receipt.intended_paths[0] == destination
    assert result.base_revision == base
    assert result.records[0].path == destination
    assert result.records[0].revision.root.startswith("sha256:")
    assert not location.real_root.joinpath(*destination.parts).exists()
    assert "expected_store_revision" not in ImportRequest.__dataclass_fields__


def test_import_invokes_its_recursive_scope_factory_exactly_once(tmp_path: Path) -> None:
    calls: list[str] = []
    repository, location, service = _fixture(tmp_path, scope_calls=calls)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)

    service.execute(ImportRequest(location, manifest))

    assert calls == ["recursive"]


def test_import_reads_each_bounded_external_snapshot_once(tmp_path: Path) -> None:
    class ReadSpy:
        def __init__(self) -> None:
            self.delegate = FilesystemExternalFileReader()
            self.calls: list[tuple[Path, int, str]] = []

        def read_external(self, path: Path, *, limit: int, field: str) -> bytes:
            self.calls.append((path, limit, field))
            return self.delegate.read_external(path, limit=limit, field=field)

    reader = ReadSpy()
    repository, location, service = _fixture(tmp_path, external_files=reader)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)

    service.execute(ImportRequest(location, manifest))

    assert reader.calls == [
        (manifest, FRONTMATTER_LIMIT, "manifest"),
        (tmp_path / "records" / "decision.toml", FRONTMATTER_LIMIT, "frontmatter"),
        (tmp_path / "records" / "decision.md", BODY_LIMIT, "body"),
    ]


def test_import_manifest_accepts_exact_bound_and_rejects_limit_plus_one(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    raw = manifest.read_bytes()
    padding = FRONTMATTER_LIMIT - len(raw)
    manifest.write_bytes(raw + b"\n#" + b"x" * (padding - 2))
    assert manifest.stat().st_size == FRONTMATTER_LIMIT

    service.execute(ImportRequest(location, manifest))

    manifest.write_bytes(manifest.read_bytes() + b"x")
    with pytest.raises(ImportConflict) as captured:
        service.execute(ImportRequest(location, manifest))
    assert captured.value.diagnostics[0].code == "ORC001"


def test_import_manifest_invalid_utf8_is_orc001_without_content_leak(tmp_path: Path) -> None:
    _, location, service = _fixture(tmp_path)
    manifest = tmp_path / "import.toml"
    manifest.write_bytes(b"private-secret\xff")

    with pytest.raises(ImportConflict) as captured:
        service.execute(ImportRequest(location, manifest))

    diagnostic = captured.value.diagnostics[0]
    assert diagnostic.code == "ORC001"
    assert "private-secret" not in diagnostic.message


def test_import_record_frontmatter_invalid_utf8_is_orc001_without_content_leak(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    frontmatter = tmp_path / "records" / "decision.toml"
    frontmatter.write_bytes(b"private-frontmatter-secret\xff")

    with pytest.raises(ImportConflict) as captured:
        service.execute(ImportRequest(location, manifest))

    diagnostic = captured.value.diagnostics[0]
    assert diagnostic.code == "ORC001"
    assert diagnostic.path == frontmatter.as_posix()
    assert diagnostic.field == "frontmatter"
    assert "private-frontmatter-secret" not in diagnostic.message
    assert "private-frontmatter-secret" not in str(captured.value)


def test_import_record_body_invalid_utf8_preserves_codec_diagnostic_without_content_leak(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    body = tmp_path / "records" / "decision.md"
    body.write_bytes(b"private-body-secret\xff")

    with pytest.raises(ImportConflict) as captured:
        service.execute(ImportRequest(location, manifest))

    diagnostic = captured.value.diagnostics[0]
    assert diagnostic.code == "ORC001"
    assert diagnostic.path == (f"decisions/{DECISION_ID}-creme-brulee-import-contract.md")
    assert diagnostic.field == ""
    assert "private-body-secret" not in diagnostic.message
    assert "private-body-secret" not in str(captured.value)


def test_apply_requires_if_clean_and_inserts_canonical_tracked_by_evidence(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)

    with pytest.raises(ImportConflict, match="--if-clean"):
        service.execute(ImportRequest(location, manifest, apply=True))

    result = service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))
    snapshot = repository.load_local(location, headers_only=False)
    record = snapshot.records[0]
    assert result.receipt.canonical_applied is True
    assert result.receipt.views_current is True
    assert record.metadata.evidence[0].relation is EvidenceRelation.TRACKED_BY
    assert record.metadata.evidence[0].reference.root.startswith("git:abc123:")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "untaped.orchestration.import/v2"),
        ("target_store_id", "sto_019f0000000070008000000000000099"),
        ("expected_store_revision", f"sha256:{'0' * 64}"),
    ],
)
def test_manifest_schema_target_and_exact_base_revision_are_guarded(
    tmp_path: Path, field: str, value: object
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root, **{field: value})
    with pytest.raises((ImportConflict, ValueError)):
        service.execute(ImportRequest(location, manifest))


def test_manifest_destination_collisions_and_duplicate_normalized_evidence_refuse(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    record_block = manifest.read_text().split("[[records]]", 1)[1]
    manifest.write_text(manifest.read_text() + "\n[[records]]" + record_block)
    with pytest.raises(ImportConflict, match="collision"):
        service.execute(ImportRequest(location, manifest))

    manifest = _manifest(tmp_path, base.root)
    frontmatter = tmp_path / "records" / "decision.toml"
    frontmatter.write_bytes(
        frontmatter.read_bytes()
        + b'\n[[evidence]]\nrelation = "tracked-by"\nreference = "url:https://EXAMPLE.com/source"\n'
        + b'\n[[evidence]]\nrelation = "tracked-by"\nreference = "url:https://example.com/source"\n'
    )
    with pytest.raises(ImportConflict):
        service.execute(ImportRequest(location, manifest))


@pytest.mark.parametrize("policy", ["public", "decisions_only"])
def test_task_import_is_refused_by_store_policy(tmp_path: Path, policy: str) -> None:
    repository, location, service = _fixture(
        tmp_path,
        public=policy == "public",
        decisions_only=policy == "decisions_only",
    )
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    raw = task_bytes()
    (tmp_path / "records" / "decision.toml").write_bytes(_frontmatter(raw))
    (tmp_path / "records" / "decision.md").write_bytes(_body(raw))
    manifest.write_text(
        manifest.read_text()
        .replace('destination = "decisions"', 'destination = "tasks"')
        .replace(DECISION_ID, TASK_ID)
    )
    with pytest.raises(ValueError):
        service.execute(ImportRequest(location, manifest))


def test_import_runs_full_graph_validation(tmp_path: Path) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    frontmatter = tmp_path / "records" / "decision.toml"
    frontmatter.write_bytes(
        frontmatter.read_bytes()
        + b'\n[[links]]\nrelation = "supersedes"\n'
        + f'target_store_id = "{STORE_ID}"\n'.encode()
        + b'target = "dec_019f0000000070008000000000000099"\n'
    )
    with pytest.raises(ValueError):
        service.execute(ImportRequest(location, manifest))


@pytest.mark.parametrize(
    ("relation", "existing_reference", "manifest_reference"),
    [
        (
            relation,
            existing,
            canonical,
        )
        for relation in ("implemented-by", "verified-by", "released-as")
        for existing, canonical in (
            ("url:https://EXAMPLE.com/Source", "url:https://example.com/Source"),
            ("github-pr:Owner/Repo#7", "github-pr:owner/repo#7"),
        )
    ],
)
@pytest.mark.parametrize("apply", [False, True])
def test_import_rejects_canonical_source_reference_under_any_other_relation_before_write(
    tmp_path: Path,
    relation: str,
    existing_reference: str,
    manifest_reference: str,
    apply: bool,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    frontmatter = tmp_path / "records" / "decision.toml"
    frontmatter.write_bytes(
        frontmatter.read_bytes()
        + f'\n[[evidence]]\nrelation = "{relation}"\n'.encode()
        + f'reference = "{existing_reference}"\n'.encode()
    )
    manifest.write_text(
        manifest.read_text().replace(
            f"git:abc123:orchestration/DECISIONS.md#sha256:{'a' * 64}",
            manifest_reference,
        )
    )
    destination = location.real_root / "decisions"

    with pytest.raises(ImportConflict, match="source_ref"):
        service.execute(ImportRequest(location, manifest, apply=apply, if_clean=apply))

    assert not destination.exists()


def test_exact_tracked_by_canonical_source_is_idempotent_across_multi_record_import(
    tmp_path: Path,
) -> None:
    repository, location, service = _fixture(tmp_path)
    base = repository.load_local(location, headers_only=False).store_revision
    manifest = _manifest(tmp_path, base.root)
    frontmatter = tmp_path / "records" / "decision.toml"
    frontmatter.write_bytes(
        frontmatter.read_bytes()
        + b'\n[[evidence]]\nrelation = "tracked-by"\n'
        + b'reference = "url:https://EXAMPLE.com/Source"\n'
    )
    manifest.write_text(
        manifest.read_text().replace(
            f"git:abc123:orchestration/DECISIONS.md#sha256:{'a' * 64}",
            "url:https://example.com/Source",
        )
    )
    second = _append_decision_record(tmp_path, manifest)

    result = service.execute(ImportRequest(location, manifest, apply=True, if_clean=True))

    assert len(result.records) == 2
    assert location.real_root.joinpath(*second.parts).is_file()
    first = repository.load_local(location, headers_only=False).records[0]
    matching = [
        evidence
        for evidence in first.metadata.evidence
        if evidence.reference.root == "url:https://example.com/Source"
    ]
    assert len(matching) == 1
    assert matching[0].relation is EvidenceRelation.TRACKED_BY
