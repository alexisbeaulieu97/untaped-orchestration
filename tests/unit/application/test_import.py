from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID, STORE_ID, TASK_ID, decision_bytes, task_bytes
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.maintenance import (
    ImportConflict,
    ImportRequest,
    ImportService,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.results import Completeness, FederatedSnapshot
from untaped_orchestration.domain.evidence import EvidenceRelation
from untaped_orchestration.infrastructure.filesystem import location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def _frontmatter(raw: bytes) -> bytes:
    return raw.split(b"+++\n", 2)[1]


def _body(raw: bytes) -> bytes:
    return raw.split(b"+++\n", 2)[2]


def _fixture(tmp_path: Path, *, public: bool = False, decisions_only: bool = False):
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

    executor = MutationExecutor(repository, repository, locks, views, projector=repository)
    service = ImportService(
        repository,
        executor,
        views,
        locations=(location,),
        load=load,
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
