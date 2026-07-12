from __future__ import annotations

import pytest

from tests.builders import DECISION_ID, STORE_ID, TASK_ID
from untaped_orchestration.cli import app

REVISION = f"sha256:{'b' * 64}"


def _commands(current, prefix: str = "") -> set[str]:
    values = set()
    for name, nested in current.resolved_commands().items():
        if name.startswith("-"):
            continue
        path = f"{prefix} {name}".strip()
        values.add(path)
        values.update(_commands(nested, path))
    return values


def test_complete_command_tree_is_registered() -> None:
    rendered = _commands(app)
    for command in (
        "id new",
        "init",
        "brief",
        "list",
        "show",
        "inspect",
        "search",
        "trace",
        "next",
        "curate next",
        "history list",
        "history search",
        "history show",
        "task create",
        "task update",
        "task transition",
        "task move",
        "task review",
        "task close",
        "decision create",
        "decision update",
        "decision supersede",
        "decision retire",
        "link add",
        "link remove",
        "evidence add",
        "evidence remove",
        "curate acknowledge",
        "curate snooze",
        "store child add",
        "store child remove",
        "store child list",
        "store import",
        "check",
        "fmt",
        "render",
        "repair frontmatter",
        "repair duplicate",
    ):
        assert command in rendered


def test_id_new_requires_no_store_and_rejects_non_id_kind(capsys) -> None:
    with __import__("pytest").raises(SystemExit) as raised:
        app(["id", "new", "task", "--format", "raw"], exit_on_error=False)
    assert raised.value.code == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("tsk_")
    with __import__("pytest").raises(Exception):
        app.parse_args(["id", "new", "unknown"], exit_on_error=False, print_error=False)


@pytest.mark.parametrize(
    "argv",
    (
        ("list", "--limit", "0"),
        ("show", "not-an-id", "--format", "json"),
        ("show", TASK_ID, "--raw", "--format", "table"),
    ),
)
def test_cli_validation_precedes_store_resolution_and_exits_two(
    argv: tuple[str, ...], capsys
) -> None:
    with pytest.raises(SystemExit) as raised:
        app(argv, exit_on_error=False)
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "no .untaped/orchestration" not in captured.err


def test_id_new_raw_rejects_columns(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        app(
            ("id", "new", "task", "--format", "raw", "--columns", "kind"),
            exit_on_error=False,
        )
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""


@pytest.mark.parametrize(
    "argv",
    (
        ("task", "update", TASK_ID, "--title", "x", "--if-revision", REVISION, "--force-current"),
        ("task", "review", TASK_ID),
        ("decision", "update", DECISION_ID, "--title", "x"),
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
        ),
        (
            "evidence",
            "remove",
            DECISION_ID,
            "--relation",
            "verified-by",
            "--reference",
            "url:https://example.test",
        ),
        ("curate", "acknowledge", TASK_ID, "--if-revision", REVISION, "--force-current"),
        ("store", "child", "remove", STORE_ID),
    ),
)
def test_existing_item_leaves_require_exactly_one_guard_mode(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as raised:
        app(argv, exit_on_error=False)
    assert raised.value.code == 2


@pytest.mark.parametrize(
    "argv",
    (
        (
            "task",
            "transition",
            TASK_ID,
            "--to",
            "planned",
            "--before",
            TASK_ID,
            "--if-parent",
            "none",
            "--if-revision",
            REVISION,
            "--if-store-revision",
            REVISION,
        ),
        (
            "task",
            "move",
            TASK_ID,
            "--parent",
            "none",
            "--last",
            "--if-parent",
            "none",
            "--if-revision",
            REVISION,
            "--if-store-revision",
            REVISION,
            "--if-anchor-revision",
            REVISION,
        ),
        (
            "task",
            "transition",
            TASK_ID,
            "--to",
            "planned",
            "--before",
            TASK_ID,
            "--if-parent",
            "none",
            "--force-current",
            "--if-anchor-revision",
            REVISION,
        ),
    ),
)
def test_relative_placement_anchor_guard_contract_is_usage_validated(
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as raised:
        app(argv, exit_on_error=False)
    assert raised.value.code == 2


@pytest.mark.parametrize(
    "argv",
    (
        (
            "task",
            "close",
            TASK_ID,
            "--outcome",
            "superseded",
            "--note",
            "replaced",
            "--if-revision",
            REVISION,
            "--if-store-revision",
            REVISION,
        ),
        (
            "task",
            "close",
            TASK_ID,
            "--outcome",
            "delivered",
            "--note",
            "done",
            "--successor",
            TASK_ID,
            "--if-successor-revision",
            REVISION,
            "--force-current",
        ),
    ),
)
def test_close_successor_options_match_superseded_outcome(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as raised:
        app(argv, exit_on_error=False)
    assert raised.value.code == 2
