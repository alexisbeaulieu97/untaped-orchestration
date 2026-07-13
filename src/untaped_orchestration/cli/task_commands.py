from __future__ import annotations

from pathlib import Path

from cyclopts import App

from untaped_orchestration.application.curation import AcknowledgeRequest
from untaped_orchestration.application.item_support import CreateTaskRequest, UpdateTaskRequest
from untaped_orchestration.application.ports import ExternalFileReader
from untaped_orchestration.application.tasks import (
    CloseTaskRequest,
    MoveTaskRequest,
    TransitionTaskRequest,
)
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.cli.options import (
    ColumnsOption,
    OutputFormat,
    read_body_file,
    usage_value,
)
from untaped_orchestration.cli.output import CommandResult, run_command
from untaped_orchestration.domain.ids import Slug, TaskId
from untaped_orchestration.domain.models import Revision, TaskOutcome, TaskPriority, TaskStage
from untaped_orchestration.domain.ordering import PlacementAnchor, PlacementAnchorKind


def _body(reader: ExternalFileReader, path: Path | None) -> bytes:
    return b"" if path is None else read_body_file(reader, path)


def _parent(value: str) -> TaskId | None:
    return None if value == "none" else _task_id(value)


def _revision(value: str | None) -> Revision | None:
    return None if value is None else _required_revision(value)


def _required_revision(value: str) -> Revision:
    return usage_value(lambda: Revision(value))


def _task_id(value: str) -> TaskId:
    return usage_value(lambda: TaskId(value))


def _slug(value: str) -> Slug:
    return usage_value(lambda: Slug(value))


def _guard(value: str | None, force: bool) -> None:
    if force == (value is not None):
        raise SystemExit(2)


def _placement(
    first: bool,
    last: bool,
    before: TaskId | None,
    after: TaskId | None,
) -> PlacementAnchor:
    selected = sum((first, last, before is not None, after is not None))
    if selected > 1:
        raise SystemExit(2)
    if before is not None:
        return PlacementAnchor(PlacementAnchorKind.BEFORE, before)
    if after is not None:
        return PlacementAnchor(PlacementAnchorKind.AFTER, after)
    return PlacementAnchor(PlacementAnchorKind.FIRST if first else PlacementAnchorKind.LAST)


def _placement_guard(
    before: str | None,
    after: str | None,
    if_anchor_revision: str | None,
    force_current: bool,
) -> None:
    relative = before is not None or after is not None
    if force_current:
        if if_anchor_revision is not None:
            raise SystemExit(2)
    elif relative != (if_anchor_revision is not None):
        raise SystemExit(2)


def register(app: App) -> None:  # noqa: C901
    tasks = app.command(App(name="task"))
    assert isinstance(tasks, App)

    @tasks.command(name="create")
    def create(
        *,
        id: str,
        title: str,
        body_file: Path | None = None,
        tag: tuple[str, ...] = (),
        priority: TaskPriority = TaskPriority.NORMAL,
        waiting_on: tuple[str, ...] = (),
        if_store_revision: str,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            body = _body(context.repository, body_file)
            result = context.create_task().execute(
                context.scope,
                CreateTaskRequest(
                    _task_id(id),
                    title,
                    body,
                    tuple(_slug(value) for value in tag),
                    priority,
                    tuple(_slug(value) for value in waiting_on),
                    _required_revision(if_store_revision),
                ),
            )
            return CommandResult("task create", result)

        run_command("task create", action, fmt=format, allowed=("table", "json"), columns=columns)

    @tasks.command(name="update")
    def update(
        item_id: str,
        /,
        *,
        title: str | None = None,
        body_file: Path | None = None,
        priority: TaskPriority | None = None,
        tag: tuple[str, ...] | None = None,
        clear_tags: bool = False,
        waiting_on: tuple[str, ...] | None = None,
        clear_waiting_on: bool = False,
        if_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_revision, force_current)
        if (tag is not None and clear_tags) or (waiting_on is not None and clear_waiting_on):
            raise SystemExit(2)
        if all(
            (
                title is None,
                body_file is None,
                priority is None,
                tag is None,
                not clear_tags,
                waiting_on is None,
                not clear_waiting_on,
            )
        ):
            raise SystemExit(2)

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            body = None if body_file is None else _body(context.repository, body_file)
            result = context.update_task().execute(
                context.scope,
                UpdateTaskRequest(
                    _task_id(item_id),
                    _revision(if_revision),
                    force_current,
                    title,
                    body,
                    priority,
                    ()
                    if clear_tags
                    else (None if tag is None else tuple(_slug(value) for value in tag)),
                    ()
                    if clear_waiting_on
                    else (
                        None if waiting_on is None else tuple(_slug(value) for value in waiting_on)
                    ),
                ),
            )
            return CommandResult("task update", result)

        run_command("task update", action, fmt=format, allowed=("table", "json"), columns=columns)

    def placement_command(
        command: str,
        request: TransitionTaskRequest | MoveTaskRequest,
        store: str | None,
        format: OutputFormat,
        columns: tuple[str, ...],
    ) -> None:
        run_command(
            command,
            lambda: CommandResult(
                command,
                (
                    CliContext.resolve(store).tasks().transition(request)
                    if isinstance(request, TransitionTaskRequest)
                    else CliContext.resolve(store).tasks().move(request)
                ),
            ),
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @tasks.command(name="transition")
    def transition(
        item_id: str,
        /,
        *,
        to: TaskStage,
        revisit_when: str | None = None,
        first: bool = False,
        last: bool = False,
        before: str | None = None,
        after: str | None = None,
        if_parent: str,
        if_revision: str | None = None,
        if_store_revision: str | None = None,
        if_anchor_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_revision, force_current)
        _guard(if_store_revision, force_current)
        _placement_guard(before, after, if_anchor_revision, force_current)
        placement_command(
            "task transition",
            TransitionTaskRequest(
                _task_id(item_id),
                to,
                _parent(if_parent),
                _revision(if_revision),
                _revision(if_store_revision),
                _placement(
                    first,
                    last,
                    None if before is None else _task_id(before),
                    None if after is None else _task_id(after),
                ),
                revisit_when,
                _revision(if_anchor_revision),
                force_current,
            ),
            store,
            format,
            columns,
        )

    @tasks.command(name="move")
    def move(
        item_id: str,
        /,
        *,
        parent: str,
        first: bool = False,
        last: bool = False,
        before: str | None = None,
        after: str | None = None,
        if_parent: str,
        if_revision: str | None = None,
        if_store_revision: str | None = None,
        if_anchor_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_revision, force_current)
        _guard(if_store_revision, force_current)
        _placement_guard(before, after, if_anchor_revision, force_current)
        placement_command(
            "task move",
            MoveTaskRequest(
                _task_id(item_id),
                _parent(parent),
                _parent(if_parent),
                _revision(if_revision),
                _revision(if_store_revision),
                _placement(
                    first,
                    last,
                    None if before is None else _task_id(before),
                    None if after is None else _task_id(after),
                ),
                _revision(if_anchor_revision),
                force_current,
            ),
            store,
            format,
            columns,
        )

    @tasks.command(name="review")
    def review(
        item_id: str,
        /,
        *,
        if_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_revision, force_current)
        run_command(
            "task review",
            lambda: CommandResult(
                "task review",
                CliContext.resolve(store)
                .tasks()
                .review(
                    AcknowledgeRequest(_task_id(item_id), _revision(if_revision), force_current)
                ),
            ),
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @tasks.command(name="close")
    def close(
        item_id: str,
        /,
        *,
        outcome: TaskOutcome,
        note: str,
        successor: str | None = None,
        if_revision: str | None = None,
        if_successor_revision: str | None = None,
        if_store_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_revision, force_current)
        _guard(if_store_revision, force_current)
        superseded = outcome is TaskOutcome.SUPERSEDED
        if superseded != (successor is not None):
            raise SystemExit(2)
        if force_current:
            if if_successor_revision is not None:
                raise SystemExit(2)
        elif superseded != (if_successor_revision is not None):
            raise SystemExit(2)
        run_command(
            "task close",
            lambda: CommandResult(
                "task close",
                CliContext.resolve(store)
                .tasks()
                .close(
                    CloseTaskRequest(
                        _task_id(item_id),
                        outcome,
                        note,
                        _revision(if_revision),
                        _revision(if_store_revision),
                        None if successor is None else _task_id(successor),
                        _revision(if_successor_revision),
                        force_current,
                    )
                ),
            ),
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )
