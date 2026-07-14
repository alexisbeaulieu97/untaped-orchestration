from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from tests.builders import DECISION_ID, STORE_ID, TASK_ID, decision_bytes, task_bytes
from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.maintenance import (
    CheckStore,
    FormatStore,
    InvalidStoreState,
    RenderStore,
    RevisionConflict,
)
from untaped_orchestration.infrastructure.codec import CanonicalStoreFormatter
from untaped_orchestration.infrastructure.filesystem import PathSafetyError, location_from_root
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class RecordingWriter:
    def __init__(self, delegate: FilesystemStoreRepository) -> None:
        self.delegate = delegate
        self.replacements: list[PurePosixPath] = []
        self.deletions: list[PurePosixPath] = []

    def prepare(self, root: Path):
        return self.delegate.prepare(root)

    def replace(self, location, change) -> None:
        self.replacements.append(change.path)
        self.delegate.replace(location, change)

    def delete(self, location, change) -> None:
        self.deletions.append(change.path)
        self.delegate.delete(location, change)


class FailingViews:
    def managed_paths(self):
        return MarkdownViewRenderer().managed_paths()

    def expected(self, snapshot):
        del snapshot
        raise OSError("renderer unavailable")


class ExplodingViews(FailingViews):
    def expected(self, snapshot):
        del snapshot
        raise AssertionError("invalid state must not be rendered")


class TypedFailingViews(FailingViews):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def expected(self, snapshot):
        del snapshot
        raise self.error


class FailAfterDurableViewWrite(RecordingWriter):
    def __init__(self, delegate: FilesystemStoreRepository, stop_after: int) -> None:
        super().__init__(delegate)
        self.stop_after = stop_after
        self.writes = 0

    def replace(self, location, change) -> None:
        super().replace(location, change)
        self.writes += 1
        if self.writes == self.stop_after:
            raise OSError("view acknowledgement lost")


def _initialized(tmp_path: Path):
    target = tmp_path / "repository"
    target.mkdir()
    repository = FilesystemStoreRepository()
    locks = FileLockManager()
    views = MarkdownViewRenderer()
    InitializeStore(repository, repository, locks, views).execute(
        InitRequest(target, STORE_ID, "Store", "UTC")
    )
    root = target / ".untaped" / "orchestration"
    return root, repository, locks, views


def test_check_reports_missing_and_stale_views_with_sorted_orc008_diagnostics(
    tmp_path: Path,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    root.joinpath("views/backlog.md").unlink()
    root.joinpath("views/inbox.md").write_text("hand edited\n")
    location = location_from_root(root)

    result = CheckStore(repository, locks, views).execute(location)

    assert not result.valid
    assert not result.views_current
    assert [(value.code, value.path) for value in result.diagnostics] == [
        ("ORC008", "views/backlog.md"),
        ("ORC008", "views/inbox.md"),
    ]


def test_check_and_check_modes_never_call_the_writer(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    location = location_from_root(root)
    recording = RecordingWriter(repository)

    check = CheckStore(repository, locks, views).execute(location)
    fmt = FormatStore(repository, recording, locks, views, CanonicalStoreFormatter()).check(
        location
    )
    render = RenderStore(repository, recording, locks, views).check(location)

    assert check.valid
    assert fmt.matches
    assert render.matches
    assert recording.replacements == []


def test_fmt_check_preserves_typed_path_diagnostic_from_view_comparison(tmp_path: Path) -> None:
    root, repository, locks, _ = _initialized(tmp_path)
    location = location_from_root(root)
    error = PathSafetyError(PurePosixPath("views/roadmap.md"), "unsafe generated view")

    with pytest.raises(PathSafetyError) as captured:
        FormatStore(
            repository,
            repository,
            locks,
            TypedFailingViews(error),
            CanonicalStoreFormatter(),
        ).check(location)

    assert captured.value is error


def test_render_write_is_a_fixpoint_and_repairs_every_applicable_view(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    location = location_from_root(root)
    root.joinpath("views/roadmap.md").write_text("stale\n")

    first = RenderStore(repository, repository, locks, views).write(location)
    second = RenderStore(repository, repository, locks, views).write(location)

    assert first.applied
    assert first.views_current
    assert PurePosixPath("views/roadmap.md") in first.changed_paths
    assert not second.applied
    assert second.matches
    assert second.changed_paths == ()


def test_fmt_check_compares_full_bytes_and_write_preserves_item_body(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    item = root / "decisions" / f"{DECISION_ID}-choice.md"
    item.parent.mkdir()
    body = b"Opaque\r\nbody with \\ and | bytes.\n"
    raw = decision_bytes()
    delimiter = raw.index(b"+++\n", 4) + 4
    noncanonical = raw[:4] + b"# removable comment\n" + raw[4:delimiter] + body
    item.write_bytes(noncanonical)
    store = root / "store.toml"
    store.write_bytes(b"# removable comment\n" + store.read_bytes())
    location = location_from_root(root)
    formatter = FormatStore(repository, repository, locks, views, CanonicalStoreFormatter())

    checked = formatter.check(location)
    revision = repository.load_local(location, headers_only=True).store_revision
    written = formatter.write(location, expected_store_revision=revision)

    assert not checked.matches
    assert {value.path for value in checked.comparisons if not value.matches} == {
        PurePosixPath("store.toml"),
        PurePosixPath(f"decisions/{DECISION_ID}-choice.md"),
    }
    assert written.canonical_applied
    assert item.read_bytes().endswith(body)
    assert b"removable comment" not in item.read_bytes()
    assert b"removable comment" not in store.read_bytes()
    assert formatter.check(location).matches


def test_fmt_write_rejects_a_stale_store_guard_before_writing(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    store = root / "store.toml"
    store.write_bytes(b"# comment\n" + store.read_bytes())
    recording = RecordingWriter(repository)
    location = location_from_root(root)

    with pytest.raises(RevisionConflict):
        FormatStore(repository, recording, locks, views, CanonicalStoreFormatter()).write(
            location, expected_store_revision="sha256:" + "0" * 64
        )

    assert recording.replacements == []
    assert store.read_bytes().startswith(b"# comment\n")


def test_fmt_refuses_invalid_metadata_without_rewriting_any_file(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    store = root / "store.toml"
    invalid = store.read_bytes().replace(b'visibility = "private"', b'visibility = "invalid"')
    store.write_bytes(invalid)
    recording = RecordingWriter(repository)
    location = location_from_root(root)
    revision = repository.load_local(location, headers_only=True).store_revision

    with pytest.raises(InvalidStoreState):
        FormatStore(repository, recording, locks, views, CanonicalStoreFormatter()).write(
            location, expected_store_revision=revision
        )

    assert recording.replacements == []
    assert store.read_bytes() == invalid


def test_check_reports_invalid_store_metadata_without_crashing(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    store = root / "store.toml"
    store.write_bytes(
        store.read_bytes().replace(b'visibility = "private"', b'visibility = "invalid"')
    )

    result = CheckStore(repository, locks, views).execute(location_from_root(root))

    assert not result.valid
    assert result.store_id == STORE_ID
    assert any(
        value.code == "ORC002" and value.path == "store.toml" for value in result.diagnostics
    )


def test_malformed_administrative_metadata_preserves_revisions_without_identity(
    tmp_path: Path,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    root.joinpath("store.toml").write_bytes(b"not = [valid\n")
    root.joinpath("registry.toml").write_bytes(b"also = [invalid\n")
    location = location_from_root(root)

    checked = CheckStore(repository, locks, views).execute(location)

    assert checked.store_id is None
    assert checked.store_revision is not None
    assert checked.registry_revision is not None
    assert [(value.code, value.path) for value in checked.diagnostics] == [
        ("ORC001", "registry.toml"),
        ("ORC001", "store.toml"),
    ]
    for operation in (
        lambda: RenderStore(repository, repository, locks, views).write(location),
        lambda: FormatStore(repository, repository, locks, views, CanonicalStoreFormatter()).write(
            location, expected_store_revision=checked.store_revision
        ),
    ):
        with pytest.raises(InvalidStoreState) as captured:
            operation()
        assert captured.value.diagnostics == checked.diagnostics
        assert captured.value.result == checked


def test_fmt_view_failure_preserves_canonical_write_and_reports_stale_views(
    tmp_path: Path,
) -> None:
    root, repository, locks, _ = _initialized(tmp_path)
    store = root / "store.toml"
    store.write_bytes(b"# comment\n" + store.read_bytes())
    location = location_from_root(root)
    revision = repository.load_local(location, headers_only=True).store_revision

    result = FormatStore(
        repository, repository, locks, FailingViews(), CanonicalStoreFormatter()
    ).write(location, expected_store_revision=revision)

    assert result.canonical_applied
    assert not result.views_current
    assert b"# comment" not in store.read_bytes()


def test_decision_only_conversion_diagnoses_and_deletes_all_sensitive_task_views(
    tmp_path: Path,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    sensitive = b"private unfinished acquisition target\n"
    for name in ("roadmap.md", "backlog.md", "inbox.md"):
        root.joinpath("views", name).write_bytes(sensitive)
    store = root / "store.toml"
    store.write_bytes(store.read_bytes().replace(b"active_tasks = true", b"active_tasks = false"))
    location = location_from_root(root)

    before = CheckStore(repository, locks, views).execute(location)
    rendered = RenderStore(repository, repository, locks, views).write(location)
    after = CheckStore(repository, locks, views).execute(location)

    stale = {
        PurePosixPath("views/roadmap.md"),
        PurePosixPath("views/backlog.md"),
        PurePosixPath("views/inbox.md"),
    }
    assert {value.path for value in before.diagnostics if value.code == "ORC008"} == {
        *(path.as_posix() for path in stale),
        "views/decisions.md",
    }
    assert stale <= set(rendered.intended_paths)
    assert stale <= set(rendered.changed_paths)
    assert all(not root.joinpath(*path.parts).exists() for path in stale)
    assert tuple(path.name for path in root.joinpath("views").iterdir()) == ("decisions.md",)
    assert after.valid
    assert after.views_current


def test_check_keeps_partial_scaffold_and_orphan_temp_diagnostics_after_view_repair(
    tmp_path: Path,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    root.joinpath("views/roadmap.md").unlink()
    location = location_from_root(root)

    RenderStore(repository, repository, locks, views).write(location)
    root.joinpath("registry.toml").unlink()
    root.joinpath("AGENTS.md").unlink()
    root.joinpath("CLAUDE.md").unlink()
    orphan = root / ".store.toml.untaped-tmp-orphan"
    orphan.write_bytes(b"partial")
    snapshot = repository.load_local(location, headers_only=False)
    for path, raw in views.expected(snapshot).items():
        root.joinpath(*path.parts).write_bytes(raw)
    result = CheckStore(repository, locks, views).execute(location)

    assert not result.valid
    assert [(value.code, value.path, value.message) for value in result.diagnostics] == [
        ("ORC003", orphan.name, "orphan atomic-write temporary exists"),
        ("ORC003", "AGENTS.md", "required scaffold file is missing"),
        ("ORC003", "CLAUDE.md", "required scaffold file is missing"),
        ("ORC003", "registry.toml", "required scaffold file is missing"),
    ]
    assert not any(value.code == "ORC008" for value in result.diagnostics)

    with pytest.raises(InvalidStoreState) as captured:
        FormatStore(repository, repository, locks, views, CanonicalStoreFormatter()).write(
            location,
            expected_store_revision=repository.load_local(
                location, headers_only=True
            ).store_revision,
        )
    assert captured.value.diagnostics
    assert any(value.path == "registry.toml" for value in captured.value.diagnostics)


def test_check_reports_a_missing_anchor_without_attempting_semantic_load(
    tmp_path: Path,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    root.joinpath("store.toml").unlink()

    result = CheckStore(repository, locks, views).execute(location_from_root(root))

    assert not result.valid
    assert [(value.code, value.path) for value in result.diagnostics] == [("ORC003", "store.toml")]
    assert result.store_id is None
    assert result.store_revision is None
    assert result.registry_revision is not None


@pytest.mark.parametrize(
    ("relative", "entry", "message"),
    [
        (relative, entry, message)
        for relative in ("store.toml", "registry.toml", "AGENTS.md", "CLAUDE.md")
        for entry, message in (
            ("directory", "unexpected directory exists"),
            ("symlink", "unsafe symlink entry exists"),
            ("nonregular", "unsafe other entry exists"),
        )
    ],
)
def test_required_scaffold_nonfile_returns_one_primary_diagnostic_and_refusals(
    tmp_path: Path,
    relative: str,
    entry: str,
    message: str,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    path = root / relative
    path.unlink()
    if entry == "directory":
        path.mkdir()
    elif entry == "symlink":
        path.symlink_to(tmp_path)
    else:
        os.mkfifo(path)
    location = location_from_root(root)

    checked = CheckStore(repository, locks, views).execute(location)

    assert not checked.valid
    assert (checked.store_id is None) is (relative == "store.toml")
    assert checked.store_revision is None
    assert (checked.registry_revision is None) is (relative == "registry.toml")
    assert [(value.code, value.path, value.message) for value in checked.diagnostics] == [
        ("ORC003", relative, message)
    ]

    for operation in (
        lambda: RenderStore(repository, repository, locks, views).write(location),
        lambda: FormatStore(repository, repository, locks, views, CanonicalStoreFormatter()).write(
            location, expected_store_revision="sha256:" + "0" * 64
        ),
    ):
        with pytest.raises(InvalidStoreState) as captured:
            operation()
        assert captured.value.diagnostics == checked.diagnostics
        assert captured.value.result == checked


@pytest.mark.parametrize("kind", ["decision", "task"])
def test_check_aggregates_duplicate_identity_without_calling_renderer(
    tmp_path: Path,
    kind: str,
) -> None:
    root, repository, locks, _ = _initialized(tmp_path)
    item_id, raw, directory = (
        (DECISION_ID, decision_bytes(), root / "decisions")
        if kind == "decision"
        else (TASK_ID, task_bytes(), root / "tasks")
    )
    directory.mkdir()
    directory.joinpath(f"{item_id}-first.md").write_bytes(raw)
    directory.joinpath(f"{item_id}-second.md").write_bytes(raw)

    result = CheckStore(repository, locks, ExplodingViews()).execute(location_from_root(root))

    assert not result.valid
    duplicates = [
        value for value in result.diagnostics if value.code == "ORC003" and value.field == "id"
    ]
    assert len(duplicates) == 2


def test_check_reports_malformed_item_without_calling_renderer(tmp_path: Path) -> None:
    root, repository, locks, _ = _initialized(tmp_path)
    decisions = root / "decisions"
    decisions.mkdir()
    decisions.joinpath(f"{DECISION_ID}-broken.md").write_bytes(b"+++\nnot = [valid\n")

    result = CheckStore(repository, locks, ExplodingViews()).execute(location_from_root(root))

    assert not result.valid
    assert any(value.code == "ORC001" for value in result.diagnostics)


def test_fmt_deletes_inapplicable_managed_views_after_capability_change(
    tmp_path: Path,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    store = root / "store.toml"
    store.write_bytes(store.read_bytes().replace(b"active_tasks = true", b"active_tasks = false"))
    location = location_from_root(root)
    revision = repository.load_local(location, headers_only=True).store_revision
    recording = RecordingWriter(repository)

    result = FormatStore(repository, recording, locks, views, CanonicalStoreFormatter()).write(
        location, expected_store_revision=revision
    )

    expected_deletions = {
        PurePosixPath("views/roadmap.md"),
        PurePosixPath("views/backlog.md"),
        PurePosixPath("views/inbox.md"),
    }
    assert set(recording.deletions) == expected_deletions
    assert expected_deletions <= set(result.changed_paths)
    assert result.views_current


@pytest.mark.parametrize("service", ["fmt", "render"])
def test_write_refusal_preserves_exact_validation_diagnostics(
    tmp_path: Path,
    service: str,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    decisions = root / "decisions"
    decisions.mkdir()
    decisions.joinpath(f"{DECISION_ID}-first.md").write_bytes(decision_bytes())
    decisions.joinpath(f"{DECISION_ID}-second.md").write_bytes(decision_bytes())
    location = location_from_root(root)

    with pytest.raises(InvalidStoreState) as captured:
        if service == "fmt":
            revision = repository.load_local(location, headers_only=True).store_revision
            FormatStore(repository, repository, locks, views, CanonicalStoreFormatter()).write(
                location, expected_store_revision=revision
            )
        else:
            RenderStore(repository, repository, locks, views).write(location)

    assert captured.value.diagnostics
    assert not captured.value.result.valid
    assert captured.value.result.diagnostics == captured.value.diagnostics
    assert (
        captured.value.diagnostics
        == CheckStore(repository, locks, views).execute(location).diagnostics
    )


def test_partial_render_failure_reports_every_durably_matching_view_change(
    tmp_path: Path,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    location = location_from_root(root)
    for name in ("roadmap.md", "backlog.md", "inbox.md", "decisions.md"):
        root.joinpath("views", name).write_text("stale\n")
    failing = FailAfterDurableViewWrite(repository, stop_after=2)

    result = RenderStore(repository, failing, locks, views).write(location)

    expected_order = views.managed_paths()
    assert result.intended_paths == expected_order
    assert result.changed_paths == expected_order[:2]
    assert tuple(value.matches for value in result.comparisons) == (True, True, False, False)
    assert not result.views_current


@pytest.mark.parametrize(
    ("relative", "entry", "code"),
    [
        ("tasks/bad.txt", "file", "ORC003"),
        ("decisions/bad.md", "symlink", "ORC003"),
        ("archive/tasks/pipe.md", "nonregular", "ORC003"),
        ("views/bad.txt", "file", "ORC008"),
        ("views/nested", "directory", "ORC008"),
        ("unexpected", "directory", "ORC003"),
    ],
)
def test_check_reports_unsafe_shape_before_tolerant_semantic_load(
    tmp_path: Path,
    relative: str,
    entry: str,
    code: str,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    path = root.joinpath(*PurePosixPath(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    if entry == "file":
        path.write_bytes(b"unexpected\n")
    elif entry == "symlink":
        path.symlink_to(tmp_path)
    elif entry == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)

    result = CheckStore(repository, locks, views).execute(location_from_root(root))

    assert not result.valid
    assert [(value.code, value.path) for value in result.diagnostics] == [(code, relative)]
    assert result.store_id == STORE_ID
    assert (result.store_revision is None) is (relative != "unexpected")
    assert result.registry_revision is not None


def test_fmt_and_render_refusal_carry_unsafe_shape_diagnostics(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    unsafe = root / "decisions" / "linked.md"
    unsafe.parent.mkdir()
    unsafe.symlink_to(tmp_path)
    location = location_from_root(root)
    checked = CheckStore(repository, locks, views).execute(location)

    for operation in (
        lambda: RenderStore(repository, repository, locks, views).write(location),
        lambda: FormatStore(repository, repository, locks, views, CanonicalStoreFormatter()).write(
            location,
            expected_store_revision=repository.read_file(
                location, PurePosixPath("store.toml")
            ).revision,
        ),
    ):
        with pytest.raises(InvalidStoreState) as captured:
            operation()
        assert captured.value.diagnostics == checked.diagnostics


@pytest.mark.parametrize(
    ("relative", "raw"),
    [
        ("AGENTS.md", b"changed instructions\n"),
        ("CLAUDE.md", b"@OTHER.md\n"),
    ],
)
def test_exact_instruction_bytes_are_required_and_render_cannot_repair_them(
    tmp_path: Path,
    relative: str,
    raw: bytes,
) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    baseline = repository.load_local(location_from_root(root), headers_only=True).store_revision
    root.joinpath(relative).write_bytes(raw)
    location = location_from_root(root)

    checked = CheckStore(repository, locks, views).execute(location)
    with pytest.raises(InvalidStoreState) as captured:
        RenderStore(repository, repository, locks, views).write(location)
    after = CheckStore(repository, locks, views).execute(location)

    assert [(value.code, value.path) for value in checked.diagnostics] == [("ORC003", relative)]
    assert captured.value.diagnostics == checked.diagnostics
    assert after.diagnostics == checked.diagnostics
    assert checked.store_revision != baseline


def test_check_accepts_a_safe_outer_symlink_store_root(tmp_path: Path) -> None:
    root, repository, locks, views = _initialized(tmp_path)
    alias = tmp_path / "linked-store"
    alias.symlink_to(root, target_is_directory=True)

    result = CheckStore(repository, locks, views).execute(location_from_root(alias))

    assert result.valid
