from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.builders import CHILD_STORE_ID, DECISION_ID, STORE_ID, TASK_ID
from untaped_orchestration.cli import (
    app,
    decision_commands,
    maintenance_commands,
    read_commands,
    relation_commands,
    store_commands,
    task_commands,
)

REVISION = f"sha256:{'a' * 64}"


@dataclass(frozen=True)
class _UniversalResult:
    data: tuple[()] = ()
    complete: bool = True
    truncated: bool = False
    diagnostics: tuple[()] = ()
    children: tuple[()] = ()
    checks: tuple[()] = ()
    valid: bool = True
    matches: bool = True


class _Recorder:
    def __init__(self, prefix: str, calls: list[tuple[str, tuple[object, ...], dict[str, object]]]):
        self._prefix = prefix
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        def invoke(*args: object, **kwargs: object) -> _UniversalResult:
            self._calls.append((f"{self._prefix}.{name}", args, kwargs))
            return _UniversalResult()

        return invoke


class _Context:
    def __init__(self, calls: list[tuple[str, tuple[object, ...], dict[str, object]]]):
        self._calls = calls
        self.scope = SimpleNamespace(
            recursive=SimpleNamespace(locations=(), load=lambda: None),
        )
        self.location = object()
        self.executor = object()
        self.views = object()
        self.locks = object()
        self.repository = _Recorder("repository", calls)

    def __getattr__(self, name: str) -> Any:
        return lambda: _Recorder(name, self._calls)


@dataclass(frozen=True)
class _Case:
    module: ModuleType
    argv: tuple[str, ...]
    method: str
    request_type: str | None
    fields: dict[str, object]


CASES = (
    _Case(read_commands, ("brief", "--local"), "queries.brief", "BriefRequest", {"local": True}),
    _Case(
        read_commands,
        (
            "list",
            "--kind",
            "task",
            "--stage",
            "inbox",
            "--tag",
            "cli",
            "--waiting-on",
            "alexis",
            "--local",
            "--limit",
            "17",
        ),
        "queries.list",
        "ListRequest",
        {
            "kind": "task",
            "stage": "inbox",
            "tag": "cli",
            "waiting_on": "alexis",
            "local": True,
            "limit": 17,
        },
    ),
    _Case(
        read_commands,
        ("show", TASK_ID, "--local"),
        "queries.show",
        "ShowRequest",
        {"item_id": TASK_ID, "local": True},
    ),
    _Case(
        read_commands,
        ("show", TASK_ID, "--raw"),
        "queries.show_raw",
        "RawShowRequest",
        {"item_id": TASK_ID},
    ),
    _Case(
        read_commands,
        ("inspect", f"tasks/{TASK_ID}-x.md", "--raw"),
        "repository.read_raw",
        None,
        {},
    ),
    _Case(
        read_commands,
        ("search", "needle", "--local", "--limit", "19"),
        "queries.search",
        "SearchRequest",
        {"query": "needle", "local": True, "history": False, "limit": 19},
    ),
    _Case(
        read_commands,
        ("trace", DECISION_ID, "--direction", "incoming", "--local", "--limit", "7"),
        "queries.trace",
        "TraceRequest",
        {"item_id": DECISION_ID, "direction": "incoming", "local": True, "limit": 7},
    ),
    _Case(
        read_commands,
        ("next", "--local", "--limit", "11"),
        "queries.next",
        "NextRequest",
        {"local": True, "limit": 11},
    ),
    _Case(
        read_commands,
        ("curate", "next", "--local", "--limit", "12"),
        "curation.next",
        "CurateNextRequest",
        {"local": True, "limit": 12},
    ),
    _Case(
        read_commands,
        ("history", "list", "--outcome", "delivered", "--tag", "done", "--local", "--limit", "13"),
        "queries.history_list",
        "HistoryListRequest",
        {"outcome": "delivered", "tag": "done", "local": True, "limit": 13},
    ),
    _Case(
        read_commands,
        ("history", "search", "old", "--local", "--limit", "14"),
        "queries.history_search",
        "HistorySearchRequest",
        {"query": "old", "local": True, "limit": 14},
    ),
    _Case(
        read_commands,
        ("history", "show", TASK_ID, "--local"),
        "queries.history_show",
        "HistoryShowRequest",
        {"item_id": TASK_ID, "local": True},
    ),
    _Case(
        task_commands,
        (
            "task",
            "create",
            "--id",
            TASK_ID,
            "--title",
            "Create",
            "--tag",
            "one",
            "--priority",
            "high",
            "--waiting-on",
            "alexis",
            "--if-store-revision",
            REVISION,
        ),
        "create_task.execute",
        "CreateTaskRequest",
        {
            "item_id": TASK_ID,
            "title": "Create",
            "body": b"",
            "tags": ("one",),
            "priority": "high",
            "waiting_on": ("alexis",),
            "expected_store_revision": REVISION,
        },
    ),
    _Case(
        task_commands,
        (
            "task",
            "update",
            TASK_ID,
            "--title",
            "Update",
            "--clear-tags",
            "--clear-waiting-on",
            "--if-revision",
            REVISION,
        ),
        "update_task.execute",
        "UpdateTaskRequest",
        {
            "item_id": TASK_ID,
            "expected_revision": REVISION,
            "force_current": False,
            "title": "Update",
            "tags": (),
            "waiting_on": (),
        },
    ),
    _Case(
        task_commands,
        (
            "task",
            "transition",
            TASK_ID,
            "--to",
            "planned",
            "--first",
            "--if-parent",
            "none",
            "--if-revision",
            REVISION,
            "--if-store-revision",
            REVISION,
        ),
        "tasks.transition",
        "TransitionTaskRequest",
        {
            "item_id": TASK_ID,
            "to_stage": "planned",
            "expected_parent": None,
            "expected_revision": REVISION,
            "expected_store_revision": REVISION,
            "force_current": False,
        },
    ),
    _Case(
        task_commands,
        (
            "task",
            "move",
            TASK_ID,
            "--parent",
            "none",
            "--last",
            "--if-parent",
            "none",
            "--force-current",
        ),
        "tasks.move",
        "MoveTaskRequest",
        {
            "item_id": TASK_ID,
            "parent": None,
            "expected_parent": None,
            "expected_revision": None,
            "expected_store_revision": None,
            "force_current": True,
        },
    ),
    _Case(
        task_commands,
        ("task", "review", TASK_ID, "--if-revision", REVISION),
        "tasks.review",
        "AcknowledgeRequest",
        {"item_id": TASK_ID, "expected_revision": REVISION, "force_current": False},
    ),
    _Case(
        task_commands,
        (
            "task",
            "close",
            TASK_ID,
            "--outcome",
            "delivered",
            "--note",
            "Done",
            "--if-revision",
            REVISION,
            "--if-store-revision",
            REVISION,
        ),
        "tasks.close",
        "CloseTaskRequest",
        {
            "item_id": TASK_ID,
            "outcome": "delivered",
            "note": "Done",
            "expected_revision": REVISION,
            "expected_store_revision": REVISION,
            "force_current": False,
        },
    ),
    _Case(
        decision_commands,
        (
            "decision",
            "create",
            "--id",
            DECISION_ID,
            "--title",
            "Decision",
            "--tag",
            "ruling",
            "--if-store-revision",
            REVISION,
        ),
        "create_decision.execute",
        "CreateDecisionRequest",
        {
            "item_id": DECISION_ID,
            "title": "Decision",
            "body": b"",
            "tags": ("ruling",),
            "expected_store_revision": REVISION,
        },
    ),
    _Case(
        decision_commands,
        (
            "decision",
            "update",
            DECISION_ID,
            "--title",
            "Clarified",
            "--clear-tags",
            "--force-current",
        ),
        "update_decision.execute",
        "UpdateDecisionRequest",
        {
            "item_id": DECISION_ID,
            "expected_revision": None,
            "force_current": True,
            "title": "Clarified",
            "tags": (),
        },
    ),
    _Case(
        decision_commands,
        (
            "decision",
            "supersede",
            "--id",
            DECISION_ID,
            "--title",
            "New",
            "--predecessor",
            "dec_019f0000000070008000000000000002",
            "--if-predecessor-revision",
            f"dec_019f0000000070008000000000000002={REVISION}",
            "--if-store-revision",
            REVISION,
        ),
        "decisions.supersede",
        "SupersedeDecisionRequest",
        {
            "successor_id": DECISION_ID,
            "title": "New",
            "expected_store_revision": REVISION,
            "force_current": False,
        },
    ),
    _Case(
        decision_commands,
        ("decision", "retire", DECISION_ID, "--note", "Obsolete", "--force-current"),
        "decisions.retire",
        "RetireDecisionRequest",
        {
            "item_id": DECISION_ID,
            "note": "Obsolete",
            "expected_revision": None,
            "expected_store_revision": None,
            "force_current": True,
        },
    ),
    _Case(
        relation_commands,
        (
            "link",
            "add",
            TASK_ID,
            "--relation",
            "depends-on",
            "--target-store",
            STORE_ID,
            "--target",
            DECISION_ID,
            "--if-revision",
            REVISION,
        ),
        "links.add",
        "LinkRequest",
        {
            "source_id": TASK_ID,
            "relation": "depends-on",
            "target_store_id": STORE_ID,
            "target_id": DECISION_ID,
            "expected_revision": REVISION,
            "force_current": False,
        },
    ),
    _Case(
        relation_commands,
        (
            "link",
            "remove",
            TASK_ID,
            "--relation",
            "depends-on",
            "--target-store",
            STORE_ID,
            "--target",
            DECISION_ID,
            "--force-current",
        ),
        "links.remove",
        "LinkRequest",
        {
            "source_id": TASK_ID,
            "relation": "depends-on",
            "target_store_id": STORE_ID,
            "target_id": DECISION_ID,
            "expected_revision": None,
            "force_current": True,
        },
    ),
    _Case(
        relation_commands,
        (
            "evidence",
            "add",
            DECISION_ID,
            "--relation",
            "verified-by",
            "--reference",
            "url:https://example.test/evidence",
            "--if-revision",
            REVISION,
        ),
        "evidence.add",
        "EvidenceRequest",
        {
            "item_id": DECISION_ID,
            "relation": "verified-by",
            "reference": "url:https://example.test/evidence",
            "expected_revision": REVISION,
            "force_current": False,
        },
    ),
    _Case(
        relation_commands,
        (
            "evidence",
            "remove",
            DECISION_ID,
            "--relation",
            "verified-by",
            "--reference",
            "url:https://example.test/evidence",
            "--force-current",
        ),
        "evidence.remove",
        "EvidenceRequest",
        {
            "item_id": DECISION_ID,
            "relation": "verified-by",
            "reference": "url:https://example.test/evidence",
            "expected_revision": None,
            "force_current": True,
        },
    ),
    _Case(
        maintenance_commands,
        ("curate", "acknowledge", TASK_ID, "--if-revision", REVISION),
        "curation.acknowledge",
        "AcknowledgeRequest",
        {"item_id": TASK_ID, "expected_revision": REVISION, "force_current": False},
    ),
    _Case(
        maintenance_commands,
        ("curate", "snooze", DECISION_ID, "--until", "2026-08-01", "--force-current"),
        "curation.snooze",
        "SnoozeRequest",
        {
            "item_id": DECISION_ID,
            "until": "2026-08-01",
            "expected_revision": None,
            "force_current": True,
        },
    ),
    _Case(
        store_commands,
        (
            "store",
            "child",
            "add",
            "--id",
            CHILD_STORE_ID,
            "--path",
            "../child",
            "--if-registry-revision",
            REVISION,
        ),
        "registry.add_child",
        "AddChildRequest",
        {
            "child_id": CHILD_STORE_ID,
            "path": "../child",
            "expected_registry_revision": REVISION,
            "force_current": False,
        },
    ),
    _Case(
        store_commands,
        ("store", "child", "remove", CHILD_STORE_ID, "--force-current"),
        "registry.remove_child",
        "RemoveChildRequest",
        {"child_id": CHILD_STORE_ID, "expected_registry_revision": None, "force_current": True},
    ),
    _Case(
        store_commands,
        ("store", "child", "list", "--limit", "9"),
        "registry.list_children",
        "ListChildrenRequest",
        {"limit": 9},
    ),
    _Case(
        maintenance_commands,
        ("check", "--local", "--require-children"),
        "maintenance.check",
        "RecursiveCheckRequest",
        {"local": True, "require_children": True},
    ),
    _Case(
        maintenance_commands,
        ("fmt", "--check", "--local"),
        "maintenance.fmt_check",
        "RecursiveFormatRequest",
        {"local": True},
    ),
    _Case(
        maintenance_commands,
        ("fmt", "--write", "--local", "--if-store-revision", REVISION),
        "maintenance.fmt_write",
        "RecursiveFormatRequest",
        {"local": True},
    ),
    _Case(maintenance_commands, ("render", "--check"), "maintenance.render_check", None, {}),
    _Case(maintenance_commands, ("render", "--write"), "maintenance.render_write", None, {}),
    _Case(
        maintenance_commands,
        (
            "repair",
            "duplicate",
            TASK_ID,
            "--if-active-revision",
            REVISION,
            "--if-archive-revision",
            REVISION,
        ),
        "tasks.repair_duplicate",
        "RepairDuplicateRequest",
        {
            "item_id": TASK_ID,
            "expected_active_revision": REVISION,
            "expected_archive_revision": REVISION,
            "apply": False,
        },
    ),
)


def _plain(value: object) -> object:
    if isinstance(value, PurePosixPath):
        return value
    if hasattr(value, "root"):
        return value.root
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    return value


def _expected_formats(case: _Case) -> tuple[str, ...]:
    if case.method in {"queries.show_raw", "repository.read_raw"}:
        return ("raw", "json")
    if case.method in {
        "queries.list",
        "queries.show",
        "queries.search",
        "queries.next",
        "curation.next",
        "queries.history_list",
        "queries.history_search",
        "queries.history_show",
        "registry.list_children",
    }:
        return ("table", "json", "pipe", "raw")
    return ("table", "json")


@pytest.mark.parametrize("case", CASES, ids=lambda case: " ".join(case.argv[:3]))
def test_each_leaf_translates_to_one_typed_service_request(monkeypatch, case: _Case) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    context = _Context(calls)
    monkeypatch.setattr(case.module.CliContext, "resolve", lambda store: context)

    def execute(_command: str, action: Any, **kwargs: object) -> None:
        assert kwargs["allowed"] == _expected_formats(case)
        action()

    monkeypatch.setattr(case.module, "run_command", execute)
    with pytest.raises(SystemExit) as raised:
        app(case.argv, exit_on_error=False)
    assert raised.value.code == 0

    assert [name for name, _, _ in calls] == [case.method]
    _, args, kwargs = calls[0]
    if case.request_type is None:
        return
    request = next(arg for arg in reversed(args) if type(arg).__name__ == case.request_type)
    assert type(request).__name__ == case.request_type
    for field, expected in case.fields.items():
        assert _plain(getattr(request, field)) == expected
    if case.method == "maintenance.fmt_write":
        assert _plain(kwargs["expected_store_revision"]) == REVISION


@pytest.mark.parametrize(
    ("argv", "service_name", "method", "request_type", "fields"),
    (
        (
            (
                "init",
                "/tmp/store",
                "--store-id",
                STORE_ID,
                "--name",
                "Store",
                "--timezone",
                "UTC",
                "--public",
            ),
            "InitializeStore",
            "execute",
            "InitRequest",
            {"store_id": STORE_ID, "name": "Store", "timezone": "UTC", "public": True},
        ),
        (
            (
                "repair",
                "frontmatter",
                f"tasks/{TASK_ID}-bad.md",
                "--frontmatter-file",
                "/tmp/frontmatter.toml",
                "--if-revision",
                REVISION,
            ),
            "RepairService",
            "frontmatter",
            "RepairFrontmatterRequest",
            {
                "path": PurePosixPath(f"tasks/{TASK_ID}-bad.md"),
                "expected_revision": REVISION,
                "apply": False,
            },
        ),
        (
            ("store", "import", "/tmp/import.toml", "--if-clean", "--apply"),
            "ImportService",
            "execute",
            "ImportRequest",
            {"manifest": "/tmp/import.toml", "apply": True, "if_clean": True},
        ),
    ),
)
def test_constructor_backed_leaves_translate_one_typed_request(
    monkeypatch,
    argv: tuple[str, ...],
    service_name: str,
    method: str,
    request_type: str,
    fields: dict[str, object],
) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    context = _Context(calls)
    monkeypatch.setattr(maintenance_commands.CliContext, "resolve", lambda store: context)

    class Service:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __getattr__(self, name: str) -> Any:
            def invoke(*args: object, **kwargs: object) -> _UniversalResult:
                calls.append((name, args, kwargs))
                return _UniversalResult()

            return invoke

    monkeypatch.setattr(maintenance_commands, service_name, Service)
    monkeypatch.setattr(
        maintenance_commands,
        "run_command",
        lambda _command, action, **_kwargs: action(),
    )
    with pytest.raises(SystemExit) as raised:
        app(argv, exit_on_error=False)
    assert raised.value.code == 0
    assert [name for name, _, _ in calls] == [method]
    request = next(arg for arg in calls[0][1] if type(arg).__name__ == request_type)
    for field, expected in fields.items():
        actual = _plain(getattr(request, field))
        normalized = actual.as_posix() if hasattr(actual, "as_posix") else actual
        expected_normalized = expected.as_posix() if hasattr(expected, "as_posix") else expected
        assert normalized == expected_normalized
