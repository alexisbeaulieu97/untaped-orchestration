from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID, TASK_ID, decision_bytes, task_bytes, write_store
from untaped_orchestration.application.results import RawReference, StoreLocation
from untaped_orchestration.infrastructure import filesystem
from untaped_orchestration.infrastructure.filesystem import (
    AmbiguousRawPrefixError,
    PathSafetyError,
    StoreNotFoundError,
    canonical_input_paths,
    discover_location,
    file_revision,
    location_from_root,
    normalized_real_path_key,
    raw_reference_by_prefix,
    registry_location,
    safe_raw_path,
    store_revision,
    store_revision_from_file_revisions,
)


def test_discovers_store_upward_and_override_selects_an_exact_store_root(
    tmp_path: Path,
) -> None:
    first = write_store(tmp_path / "first")
    second = write_store(tmp_path / "second")
    nested = first.parent / "src" / "package"
    nested.mkdir(parents=True)

    discovered = discover_location(nested)
    overridden = discover_location(nested, override=second)

    assert discovered.root == first.absolute()
    assert discovered.real_root == first.resolve()
    assert overridden.root == second.absolute()
    assert overridden.real_root == second.resolve()


def test_discovery_accepts_a_file_start_and_symlinked_repository_root(tmp_path: Path) -> None:
    real_repository = tmp_path / "real"
    root = write_store(real_repository)
    source = real_repository / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("pass\n")
    linked_repository = tmp_path / "linked"
    linked_repository.symlink_to(real_repository, target_is_directory=True)

    location = discover_location(linked_repository / "src" / "module.py")

    assert location.root == linked_repository / ".untaped" / "orchestration"
    assert location.real_root == root.resolve()


def test_discovery_rejects_an_absent_or_nonregular_anchor(tmp_path: Path) -> None:
    with pytest.raises(StoreNotFoundError):
        discover_location(tmp_path)

    root = tmp_path / "explicit"
    root.mkdir()
    root.joinpath("store.toml").mkdir()
    with pytest.raises(StoreNotFoundError):
        discover_location(tmp_path, override=root)


def test_registry_location_allows_real_sibling_dotdot_and_symlinked_store_roots(
    tmp_path: Path,
) -> None:
    parent = write_store(tmp_path / "work" / "parent")
    child = write_store(tmp_path / "work" / "child")
    linked_child = tmp_path / "work" / "linked-child-store"
    linked_child.symlink_to(child, target_is_directory=True)

    sibling = registry_location(location_from_root(parent), "../../../child/.untaped/orchestration")
    linked = registry_location(location_from_root(parent), "../../../linked-child-store")

    assert sibling.real_root == child.resolve()
    assert linked.root == linked_child.absolute()
    assert linked.real_root == child.resolve()


def test_normalized_real_path_key_detects_casefold_and_symlink_aliases(tmp_path: Path) -> None:
    root = write_store(tmp_path / "Repository")
    alias = tmp_path / "repository-link"
    alias.symlink_to(root, target_is_directory=True)
    canonical = location_from_root(root)
    linked = location_from_root(alias)
    case_alias = StoreLocation(root=root, real_root=Path(str(root.resolve()).swapcase()))

    assert normalized_real_path_key(canonical) == normalized_real_path_key(linked)
    assert normalized_real_path_key(canonical) == normalized_real_path_key(case_alias)


def test_canonical_inputs_are_safe_regular_files_in_deterministic_posix_order(
    local_store: Path,
) -> None:
    task = local_store / "tasks" / f"{TASK_ID}-task.md"
    decision = local_store / "decisions" / f"{DECISION_ID}-decision.md"
    archive = local_store / "archive" / "tasks" / f"{TASK_ID}-archive.md"
    for path, raw in ((task, task_bytes()), (decision, decision_bytes()), (archive, task_bytes())):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (local_store / "views").mkdir()
    (local_store / "views" / "roadmap.md").write_text("derived\n")
    (local_store / ".lock").write_text("")
    (task.parent / f".{task.name}.untaped-tmp-deadbeef").write_text("temporary")
    (task.parent / f"{task.name}~").write_text("editor")

    paths = canonical_input_paths(location_from_root(local_store))

    assert [path.as_posix() for path in paths] == [
        "AGENTS.md",
        "CLAUDE.md",
        f"archive/tasks/{TASK_ID}-archive.md",
        f"decisions/{DECISION_ID}-decision.md",
        "registry.toml",
        "store.toml",
        f"tasks/{TASK_ID}-task.md",
    ]


def test_canonical_inputs_allow_lazy_missing_item_directories(local_store: Path) -> None:
    assert [path.as_posix() for path in canonical_input_paths(location_from_root(local_store))] == [
        "AGENTS.md",
        "CLAUDE.md",
        "registry.toml",
        "store.toml",
    ]


def test_canonical_inputs_reject_casefold_item_path_aliases(
    local_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliases = [
        PurePosixPath(f"tasks/{TASK_ID}-choice.md"),
        PurePosixPath(f"tasks/{TASK_ID}-Choice.md"),
    ]
    monkeypatch.setattr(
        filesystem,
        "_item_paths",
        lambda _location, root: aliases if root == PurePosixPath("tasks") else [],
    )

    with pytest.raises(PathSafetyError, match="case-folding path alias"):
        canonical_input_paths(location_from_root(local_store))


@pytest.mark.parametrize(
    "relative",
    [
        PurePosixPath(f"tasks/{TASK_ID}-task.md"),
        PurePosixPath(f"decisions/{DECISION_ID}-decision.md"),
        PurePosixPath(f"archive/tasks/{TASK_ID}-archive.md"),
    ],
)
def test_raw_paths_accept_only_regular_nonsymlink_item_files(
    local_store: Path,
    relative: PurePosixPath,
) -> None:
    path = local_store.joinpath(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"raw")

    assert safe_raw_path(location_from_root(local_store), relative) == path

    path.unlink()
    target = local_store / "outside.md"
    target.write_bytes(b"outside")
    path.symlink_to(target)
    with pytest.raises(PathSafetyError):
        safe_raw_path(location_from_root(local_store), relative)


@pytest.mark.parametrize(
    "relative",
    [
        PurePosixPath("store.toml"),
        PurePosixPath("views/roadmap.md"),
        PurePosixPath("tasks/nested/item.md"),
        PurePosixPath("tasks/../store.toml"),
        PurePosixPath("/tasks/item.md"),
    ],
)
def test_raw_paths_reject_admin_views_nested_traversal_and_absolute_paths(
    local_store: Path,
    relative: PurePosixPath,
) -> None:
    with pytest.raises(PathSafetyError):
        safe_raw_path(location_from_root(local_store), relative)


@pytest.mark.parametrize("unsafe_root", ["tasks", "decisions", "archive/tasks", "views"])
def test_rejects_symlinks_below_canonical_and_view_roots(
    tmp_path: Path,
    local_store: Path,
    unsafe_root: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    path = local_store.joinpath(*PurePosixPath(unsafe_root).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathSafetyError):
        canonical_input_paths(location_from_root(local_store))


def test_rejects_symlinked_files_inside_the_view_root(
    tmp_path: Path,
    local_store: Path,
) -> None:
    outside = tmp_path / "outside-view.md"
    outside.write_text("outside\n")
    views = local_store / "views"
    views.mkdir()
    views.joinpath("roadmap.md").symlink_to(outside)

    with pytest.raises(PathSafetyError):
        canonical_input_paths(location_from_root(local_store))


def test_raw_prefix_lookup_is_filename_first_and_rejects_ambiguity() -> None:
    references = (
        RawReference(
            path=PurePosixPath(f"archive/tasks/{TASK_ID}-old.md"),
            revision=file_revision(b"old"),
            size=3,
        ),
        RawReference(
            path=PurePosixPath(f"tasks/{TASK_ID}-new.md"),
            revision=file_revision(b"new"),
            size=3,
        ),
        RawReference(
            path=PurePosixPath(f"decisions/{DECISION_ID}-choice.md"),
            revision=file_revision(b"choice"),
            size=6,
        ),
    )

    assert raw_reference_by_prefix(references, DECISION_ID).path.name.startswith(DECISION_ID)
    with pytest.raises(AmbiguousRawPrefixError) as captured:
        raw_reference_by_prefix(references, TASK_ID)
    assert [path.as_posix() for path in captured.value.paths] == [
        f"archive/tasks/{TASK_ID}-old.md",
        f"tasks/{TASK_ID}-new.md",
    ]


def test_item_and_store_revisions_hash_exact_bytes_with_unambiguous_sorted_pairs() -> None:
    raw_files = {
        PurePosixPath("store.toml"): b"store\n",
        PurePosixPath("tasks/item.md"): b"item\r\n",
    }
    item_digest = hashlib.sha256(b"item\r\n").hexdigest()
    store_digest = hashlib.sha256(b"store\n").hexdigest()
    expected = hashlib.sha256(
        len(b"store.toml").to_bytes(8, "big")
        + b"store.toml"
        + bytes.fromhex(store_digest)
        + len(b"tasks/item.md").to_bytes(8, "big")
        + b"tasks/item.md"
        + bytes.fromhex(item_digest)
    ).hexdigest()

    assert file_revision(b"item\r\n").root == f"sha256:{item_digest}"
    assert store_revision(raw_files).root == f"sha256:{expected}"
    assert store_revision(dict(reversed(tuple(raw_files.items())))).root == f"sha256:{expected}"
    revisions = {path: file_revision(raw) for path, raw in raw_files.items()}
    assert store_revision_from_file_revisions(revisions).root == f"sha256:{expected}"
