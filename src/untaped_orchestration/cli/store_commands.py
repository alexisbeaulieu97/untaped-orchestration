from __future__ import annotations

from cyclopts import App

from untaped_orchestration.application.federation import (
    AddChildRequest,
    ListChildrenRequest,
    RemoveChildRequest,
)
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.cli.options import (
    DEFAULT_LIMIT,
    ColumnsOption,
    OutputFormat,
    usage_value,
    validate_limit,
)
from untaped_orchestration.cli.output import CommandResult, run_command
from untaped_orchestration.domain.ids import StoreId
from untaped_orchestration.domain.models import Revision


def _guard(value: str | None, force: bool) -> None:
    if force == (value is not None):
        raise SystemExit(2)


def _store_id(value: str) -> StoreId:
    return usage_value(lambda: StoreId(value))


def _revision(value: str | None) -> Revision | None:
    return None if value is None else usage_value(lambda: Revision(value))


def register(app: App) -> None:
    store_app = app.command(App(name="store"))
    assert isinstance(store_app, App)
    child = store_app.command(App(name="child"))
    assert isinstance(child, App)

    @child.command(name="add")
    def add(
        *,
        id: str,
        path: str,
        if_registry_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_registry_revision, force_current)

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            receipt = context.registry().add_child(
                AddChildRequest(
                    context.location,
                    _store_id(id),
                    path,
                    _revision(if_registry_revision),
                    force_current,
                )
            )
            return CommandResult("store child add", receipt)

        run_command(
            "store child add",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @child.command(name="remove")
    def remove(
        child_id: str,
        /,
        *,
        if_registry_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_registry_revision, force_current)

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            receipt = context.registry().remove_child(
                RemoveChildRequest(
                    context.location,
                    _store_id(child_id),
                    _revision(if_registry_revision),
                    force_current,
                )
            )
            return CommandResult("store child remove", receipt)

        run_command(
            "store child remove",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @child.command(name="list")
    def list_children(
        *,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        limit: int = DEFAULT_LIMIT,
        debug: bool = False,
    ) -> None:
        del debug

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            result = context.registry().list_children(
                ListChildrenRequest(context.location, usage_value(lambda: validate_limit(limit)))
            )
            return CommandResult(
                "store child list",
                result.children,
                truncated=result.truncated,
                pipe_kind="orchestration.store",
            )

        run_command(
            "store child list",
            action,
            fmt=format,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )
