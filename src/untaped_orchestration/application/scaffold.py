from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from untaped_orchestration.application.ports import (
    MANAGED_VIEW_PATHS,
    StoreEntry,
    StoreLocation,
    StoreReader,
)
from untaped_orchestration.domain.diagnostics import Diagnostic, sort_diagnostics

STORE_PATH = PurePosixPath("store.toml")
REGISTRY_PATH = PurePosixPath("registry.toml")
AGENTS_PATH = PurePosixPath("AGENTS.md")
CLAUDE_PATH = PurePosixPath("CLAUDE.md")

AGENTS_BYTES = b"""# Local orchestration store

Use `untaped-orchestration` for all canonical reads and writes. Do not read generated
views as tool input. Keep unfinished tasks private, preserve revision guards, and get
explicit approval before pushes, merges, releases, publications, or external changes.
"""
CLAUDE_BYTES = b"@AGENTS.md\n"
INSTRUCTION_BYTES = {AGENTS_PATH: AGENTS_BYTES, CLAUDE_PATH: CLAUDE_BYTES}
REQUIRED_SCAFFOLD_PATHS = (STORE_PATH, REGISTRY_PATH, AGENTS_PATH, CLAUDE_PATH)
_ALLOWED_DIRECTORIES = {
    PurePosixPath("tasks"),
    PurePosixPath("decisions"),
    PurePosixPath("archive"),
    PurePosixPath("archive/tasks"),
    PurePosixPath("views"),
}
_ITEM_PARENTS = {("tasks",), ("decisions",), ("archive", "tasks")}


@dataclass(frozen=True, slots=True)
class ShapeInspection:
    entries: tuple[StoreEntry, ...]
    contents: dict[PurePosixPath, bytes]
    diagnostics: tuple[Diagnostic, ...]
    load_safe: bool


def _diagnostic(path: PurePosixPath, message: str, *, view: bool = False) -> Diagnostic:
    return Diagnostic(
        code="ORC008" if view else "ORC003",
        severity="error",
        path=path.as_posix(),
        field="path",
        message=message,
        hint=(
            "Remove the unsafe view entry and run render --write."
            if view
            else "Restore the exact scaffold or remove only the proven unsafe entry."
        ),
    )


def _entry_diagnostic(entry: StoreEntry) -> Diagnostic | None:
    path = entry.path
    name = path.name
    if ".untaped-tmp-" in name:
        return _diagnostic(path, "orphan atomic-write temporary exists")
    if path.parts and path.parts[0] == "views" and path != PurePosixPath("views"):
        if entry.kind != "file" or path not in MANAGED_VIEW_PATHS:
            return _diagnostic(path, "unsafe or unmanaged view entry exists", view=True)
        return None
    if entry.kind in {"symlink", "other"}:
        return _diagnostic(path, f"unsafe {entry.kind} entry exists")
    if entry.kind == "directory":
        return (
            None
            if path in _ALLOWED_DIRECTORIES
            else _diagnostic(path, "unexpected directory exists")
        )
    if path in {*REQUIRED_SCAFFOLD_PATHS, PurePosixPath(".lock"), *MANAGED_VIEW_PATHS}:
        return None
    if path.parts[:-1] in _ITEM_PARENTS:
        return (
            None if path.suffix == ".md" else _diagnostic(path, "item file must use the .md suffix")
        )
    if name == ".DS_Store" or name.endswith(("~", ".swp", ".swo", ".tmp")):
        return None
    if name.startswith((".#", "#")):
        return None
    return _diagnostic(path, "unexpected store file exists")


def validate_store_shape(
    entries: tuple[StoreEntry, ...],
    contents: dict[PurePosixPath, bytes],
) -> tuple[Diagnostic, ...]:
    by_path = {entry.path: entry for entry in entries}
    diagnostics: list[Diagnostic] = []
    for required in REQUIRED_SCAFFOLD_PATHS:
        entry = by_path.get(required)
        if entry is None:
            diagnostics.append(_diagnostic(required, "required scaffold file is missing"))
    for path, expected in INSTRUCTION_BYTES.items():
        if by_path.get(path) == StoreEntry(path, "file") and contents.get(path) != expected:
            diagnostics.append(_diagnostic(path, "instruction file bytes are not canonical"))

    diagnostics.extend(
        diagnostic for entry in entries if (diagnostic := _entry_diagnostic(entry)) is not None
    )
    return sort_diagnostics(diagnostics)


def shape_is_load_safe(entries: tuple[StoreEntry, ...]) -> bool:
    by_path = {entry.path: entry for entry in entries}
    if any(by_path.get(path) != StoreEntry(path, "file") for path in REQUIRED_SCAFFOLD_PATHS):
        return False
    for entry in entries:
        path = entry.path
        if entry.kind in {"symlink", "other"}:
            return False
        if path.parts[:-1] in _ITEM_PARENTS and (entry.kind != "file" or path.suffix != ".md"):
            return False
        if (
            path.parts
            and path.parts[0] == "views"
            and path != PurePosixPath("views")
            and (entry.kind != "file" or path not in MANAGED_VIEW_PATHS)
        ):
            return False
    return True


def inspect_store_shape(reader: StoreReader, location: StoreLocation) -> ShapeInspection:
    entries = reader.list_entries(location)
    contents: dict[PurePosixPath, bytes] = {}
    file_paths = {entry.path for entry in entries if entry.kind == "file"}
    for path in INSTRUCTION_BYTES:
        if path in file_paths:
            contents[path] = reader.read_file(location, path).content
    return ShapeInspection(
        entries=entries,
        contents=contents,
        diagnostics=validate_store_shape(entries, contents),
        load_safe=(shape_is_load_safe(entries)),
    )
