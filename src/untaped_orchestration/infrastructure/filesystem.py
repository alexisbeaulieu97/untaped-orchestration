from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from untaped_orchestration.application.ports import RawReference, StoreEntry, StoreLocation
from untaped_orchestration.domain.models import Revision

STORE_ANCHOR = Path(".untaped/orchestration/store.toml")
ADMIN_PATHS = (
    PurePosixPath("store.toml"),
    PurePosixPath("registry.toml"),
    PurePosixPath("AGENTS.md"),
    PurePosixPath("CLAUDE.md"),
)
ITEM_ROOTS = (
    PurePosixPath("tasks"),
    PurePosixPath("decisions"),
    PurePosixPath("archive/tasks"),
)
VIEW_PATHS = frozenset(
    {
        PurePosixPath("views/roadmap.md"),
        PurePosixPath("views/backlog.md"),
        PurePosixPath("views/inbox.md"),
        PurePosixPath("views/decisions.md"),
    }
)


class StoreNotFoundError(FileNotFoundError):
    pass


class PathSafetyError(ValueError):
    def __init__(self, path: Path | PurePosixPath, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


class RawPrefixNotFoundError(LookupError):
    pass


class AmbiguousRawPrefixError(LookupError):
    def __init__(self, prefix: str, paths: Sequence[PurePosixPath]) -> None:
        self.prefix = prefix
        self.paths = tuple(paths)
        super().__init__(f"raw filename prefix {prefix!r} is ambiguous")


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_regular_nonsymlink(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _validate_existing_anchor(anchor: Path) -> None:
    if not _is_regular_nonsymlink(anchor):
        raise PathSafetyError(anchor, "store.toml anchor must be a regular nonsymlink file")


def location_from_root(root: Path) -> StoreLocation:
    absolute = _absolute_without_resolving(root)
    try:
        real_root = absolute.resolve(strict=True)
    except FileNotFoundError as error:
        if absolute.is_symlink():
            raise PathSafetyError(absolute, "store root is a broken symlink") from error
        raise StoreNotFoundError(f"store root does not exist: {absolute}") from error
    if not real_root.is_dir():
        raise PathSafetyError(absolute, "store root is not a directory")
    return StoreLocation(root=absolute, real_root=real_root)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_store_root(root: Path) -> StoreLocation:
    """Safely create only ``.untaped/orchestration`` below an existing target."""
    absolute = _absolute_without_resolving(root)
    if absolute.name != "orchestration" or absolute.parent.name != ".untaped":
        raise PathSafetyError(absolute, "init root must end in .untaped/orchestration")
    repository = absolute.parent.parent
    try:
        repository.resolve(strict=True)
    except FileNotFoundError as error:
        raise StoreNotFoundError(f"init target does not exist: {repository}") from error
    if not repository.is_dir():
        raise PathSafetyError(repository, "init target must be a directory")

    for directory in (absolute.parent, absolute):
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise PathSafetyError(directory, "store root chain must contain real directories")
        else:
            directory.mkdir()
            _fsync_directory(directory.parent)
    return location_from_root(absolute)


def _validate_location(location: StoreLocation) -> None:
    try:
        resolved = location.root.resolve(strict=True)
    except FileNotFoundError as error:
        raise StoreNotFoundError(f"store root does not exist: {location.root}") from error
    if resolved != location.real_root or not resolved.is_dir():
        raise PathSafetyError(location.root, "store location does not match its real-path identity")


def discover_location(start: Path, override: Path | None = None) -> StoreLocation:
    if override is not None:
        root = _absolute_without_resolving(override)
        location = location_from_root(root)
        anchor = root / "store.toml"
        if not _entry_exists(anchor):
            raise StoreNotFoundError(f"no regular store.toml anchor at explicit root: {root}")
        _validate_existing_anchor(anchor)
        return location

    current = _absolute_without_resolving(start)
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        root = candidate / STORE_ANCHOR.parent
        if not _entry_exists(root):
            continue
        location = location_from_root(root)
        anchor = root / "store.toml"
        if not _entry_exists(anchor):
            continue
        _validate_existing_anchor(anchor)
        return location
    raise StoreNotFoundError(f"no .untaped/orchestration/store.toml found above: {start}")


def registry_location(parent: StoreLocation, relative_path: str) -> StoreLocation:
    path = PurePosixPath(relative_path)
    if not relative_path or "\\" in relative_path or path.is_absolute() or relative_path == ".":
        raise PathSafetyError(path, "registry paths must be relative POSIX paths")
    candidate = parent.root.joinpath(*path.parts)
    return discover_location(parent.root, override=candidate)


def normalized_real_path_key(location: StoreLocation) -> str:
    return os.path.normcase(str(location.real_root)).casefold()


def _assert_relative(relative_path: PurePosixPath) -> None:
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise PathSafetyError(relative_path, "path must be a safe relative POSIX path")


def _walk_existing_components(root: Path, relative_path: PurePosixPath) -> Path:
    current = root
    for index, part in enumerate(relative_path.parts):
        current = current / part
        if not current.exists() and not current.is_symlink():
            continue
        if current.is_symlink():
            raise PathSafetyError(
                relative_path,
                "symlinks below the resolved store root are forbidden",
            )
        if index < len(relative_path.parts) - 1 and not current.is_dir():
            raise PathSafetyError(relative_path, "a path parent is not a directory")
    return current


def _validate_root_directory(location: StoreLocation, relative_path: PurePosixPath) -> Path | None:
    absolute = _walk_existing_components(location.real_root, relative_path)
    if not absolute.exists():
        return None
    if not absolute.is_dir():
        raise PathSafetyError(relative_path, "canonical root must be a real directory")
    return absolute


def _ignored_artifact(name: str) -> bool:
    return (
        name == ".lock"
        or name == ".DS_Store"
        or (name.startswith(".") and ".untaped-tmp-" in name)
        or name.endswith(("~", ".swp", ".swo", ".tmp"))
        or name.startswith((".#", "#"))
    )


def _item_paths(location: StoreLocation, root: PurePosixPath) -> list[PurePosixPath]:
    directory = _validate_root_directory(location, root)
    if directory is None:
        return []
    paths: list[PurePosixPath] = []
    for entry in directory.iterdir():
        relative = root / entry.name
        if entry.is_symlink():
            raise PathSafetyError(relative, "item entries must not be symlinks")
        if _ignored_artifact(entry.name):
            continue
        if not entry.is_file():
            raise PathSafetyError(relative, "item directories may contain only regular files")
        if entry.suffix != ".md":
            raise PathSafetyError(relative, "item files must use the .md suffix")
        paths.append(relative)
    return paths


def canonical_input_paths(location: StoreLocation) -> tuple[PurePosixPath, ...]:
    _validate_location(location)
    for root in (
        PurePosixPath("tasks"),
        PurePosixPath("decisions"),
        PurePosixPath("archive"),
        PurePosixPath("archive/tasks"),
        PurePosixPath("views"),
    ):
        _validate_root_directory(location, root)

    views = location.real_root / "views"
    if views.exists():
        for entry in views.iterdir():
            relative = PurePosixPath("views") / entry.name
            if entry.is_symlink() or not entry.is_file():
                raise PathSafetyError(
                    relative,
                    "view entries must be regular nonsymlink files",
                )
            if _ignored_artifact(entry.name):
                continue

    paths: list[PurePosixPath] = []
    for relative in ADMIN_PATHS:
        absolute = _walk_existing_components(location.real_root, relative)
        if not absolute.exists():
            continue
        if not _is_regular_nonsymlink(absolute):
            raise PathSafetyError(relative, "canonical inputs must be regular nonsymlink files")
        paths.append(relative)
    for root in ITEM_ROOTS:
        paths.extend(_item_paths(location, root))

    paths.sort(key=lambda value: value.as_posix())
    reject_casefold_path_aliases(paths)
    return tuple(paths)


def reject_casefold_path_aliases(paths: Iterable[PurePosixPath]) -> None:
    aliases: dict[str, PurePosixPath] = {}
    for path in paths:
        key = path.as_posix().casefold()
        prior = aliases.get(key)
        if prior is not None and prior != path:
            raise PathSafetyError(
                path,
                f"case-folding path alias conflicts with {prior.as_posix()}",
            )
        aliases[key] = path


def _is_exact_item_path(relative_path: PurePosixPath) -> bool:
    return relative_path.suffix == ".md" and any(
        relative_path.parts[:-1] == root.parts for root in ITEM_ROOTS
    )


def safe_raw_path(location: StoreLocation, relative_path: PurePosixPath) -> Path:
    _validate_location(location)
    _assert_relative(relative_path)
    if not _is_exact_item_path(relative_path):
        raise PathSafetyError(relative_path, "raw recovery is restricted to exact item roots")
    absolute = _walk_existing_components(location.real_root, relative_path)
    if not _is_regular_nonsymlink(absolute):
        raise PathSafetyError(relative_path, "raw recovery requires a regular nonsymlink file")
    return absolute


def safe_read_path(location: StoreLocation, relative_path: PurePosixPath) -> Path:
    _validate_location(location)
    _assert_relative(relative_path)
    absolute = _walk_existing_components(location.real_root, relative_path)
    if not _is_regular_nonsymlink(absolute):
        raise FileNotFoundError(relative_path.as_posix())
    return absolute


def store_entries(location: StoreLocation) -> tuple[StoreEntry, ...]:
    _validate_location(location)
    entries: list[StoreEntry] = []
    pending = [location.real_root]
    while pending:
        directory = pending.pop()
        for entry in sorted(
            directory.iterdir(), key=lambda value: (value.name.casefold(), value.name)
        ):
            relative = PurePosixPath(entry.relative_to(location.real_root).as_posix())
            if entry.is_symlink():
                entries.append(StoreEntry(relative, "symlink"))
            elif entry.is_dir():
                entries.append(StoreEntry(relative, "directory"))
                pending.append(entry)
            elif entry.is_file():
                entries.append(StoreEntry(relative, "file"))
            else:
                entries.append(StoreEntry(relative, "other"))
    reject_casefold_path_aliases(value.path for value in entries)
    return tuple(sorted(entries, key=lambda value: value.path.as_posix()))


def _is_writable_canonical_path(relative_path: PurePosixPath) -> bool:
    return (
        relative_path in ADMIN_PATHS
        or relative_path in VIEW_PATHS
        or _is_exact_item_path(relative_path)
    )


def safe_write_path(location: StoreLocation, relative_path: PurePosixPath) -> Path:
    _validate_location(location)
    _assert_relative(relative_path)
    if not _is_writable_canonical_path(relative_path):
        raise PathSafetyError(relative_path, "writes are restricted to canonical store paths")
    absolute = _walk_existing_components(location.real_root, relative_path)
    if absolute.exists() and (absolute.is_symlink() or not absolute.is_file()):
        raise PathSafetyError(relative_path, "write destination must be a regular file or absent")

    return absolute


def safe_delete_path(location: StoreLocation, relative_path: PurePosixPath) -> Path:
    _validate_location(location)
    _assert_relative(relative_path)
    if not _is_writable_canonical_path(relative_path):
        raise PathSafetyError(relative_path, "deletes are restricted to canonical store paths")
    absolute = _walk_existing_components(location.real_root, relative_path)
    if not _is_regular_nonsymlink(absolute):
        raise PathSafetyError(relative_path, "delete target must be a regular nonsymlink file")
    return absolute


def file_revision(raw: bytes) -> Revision:
    return Revision(f"sha256:{hashlib.sha256(raw).hexdigest()}")


def store_revision(files: Mapping[PurePosixPath, bytes]) -> Revision:
    return store_revision_from_file_revisions(
        {path: file_revision(raw) for path, raw in files.items()}
    )


def store_revision_from_file_revisions(
    revisions: Mapping[PurePosixPath, Revision],
) -> Revision:
    digest = hashlib.sha256()
    for path, revision in sorted(revisions.items(), key=lambda pair: pair[0].as_posix()):
        path_bytes = path.as_posix().encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(bytes.fromhex(revision.root.removeprefix("sha256:")))
    return Revision(f"sha256:{digest.hexdigest()}")


def raw_reference_by_prefix(
    references: Iterable[RawReference],
    prefix: str,
) -> RawReference:
    matches = tuple(
        sorted(
            (reference for reference in references if reference.path.name.startswith(prefix)),
            key=lambda reference: reference.path.as_posix(),
        )
    )
    if not matches:
        raise RawPrefixNotFoundError(prefix)
    if len(matches) > 1:
        raise AmbiguousRawPrefixError(prefix, tuple(reference.path for reference in matches))
    return matches[0]


type AtomicEvent = str


class AtomicFilesystem:
    """Durable single-file primitives with injectable post-boundary events."""

    def __init__(self, *, event_hook: Callable[[AtomicEvent], None] | None = None) -> None:
        self._event_hook = event_hook or (lambda _event: None)

    def _event(self, event: AtomicEvent) -> None:
        self._event_hook(event)

    @staticmethod
    def _fsync_parent(parent: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_parent_directories(self, *, root: Path, parent: Path) -> None:
        try:
            relative = parent.relative_to(root)
        except ValueError as error:
            raise PathSafetyError(parent, "write parent escapes the selected store root") from error

        current = root
        for part in relative.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if current.is_symlink() or not current.is_dir():
                    raise PathSafetyError(current, "write parent must be a real directory")
            else:
                current.mkdir()
                self._event(f"mkdir:{current.relative_to(root).as_posix()}")

            containing = current.parent
            self._fsync_parent(containing)
            containing_relative = containing.relative_to(root)
            label = containing_relative.as_posix() if containing_relative.parts else "."
            self._event(f"fsync-dir-parent:{label}")

    def replace_bytes(self, target: Path, content: bytes, *, root: Path) -> None:
        temporary: Path | None = None
        replaced = False
        try:
            self._ensure_parent_directories(root=root, parent=target.parent)
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{target.name}.untaped-tmp-",
                dir=target.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                self._event("open-temp")
                stream.write(content)
                stream.flush()
                self._event("flush")
                os.fsync(stream.fileno())
                self._event("fsync-temp")
            os.replace(temporary, target)
            replaced = True
            self._event("replace")
            self._fsync_parent(target.parent)
            self._event("fsync-parent")
            self._event("before-ack")
        finally:
            if temporary is not None and not replaced:
                temporary.unlink(missing_ok=True)

    def delete_file(self, target: Path) -> None:
        target.unlink()
        self._fsync_parent(target.parent)
        self._event("fsync-parent")
        self._event("before-ack")
