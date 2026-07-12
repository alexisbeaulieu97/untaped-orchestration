from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from cyclopts import App

from untaped_orchestration.application.curation import CurateNextRequest
from untaped_orchestration.application.query_models import (
    BriefRequest,
    HistoryListRequest,
    HistorySearchRequest,
    HistoryShowRequest,
    ListRequest,
    NextRequest,
    QueryResult,
    RawShowRequest,
    SearchRequest,
    ShowRequest,
    TraceDirection,
    TraceRequest,
)
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.cli.options import DEFAULT_LIMIT, OutputFormat, validate_limit
from untaped_orchestration.cli.output import CommandResult, run_command
from untaped_orchestration.domain.graph import DecisionState
from untaped_orchestration.domain.ids import DecisionId, TaskId
from untaped_orchestration.domain.models import ItemKind, TaskOutcome, TaskStage


def _item_id(value: str) -> TaskId | DecisionId:
    return TaskId(value) if value.startswith("tsk_") else DecisionId(value)


def _result[T](
    command: str,
    value: QueryResult[T],
    *,
    kind: str | None = None,
) -> CommandResult:
    return CommandResult(
        command,
        value.data,
        complete=value.complete,
        truncated=value.truncated,
        diagnostics=value.diagnostics,
        pipe_kind=kind,
    )


def register(app: App) -> None:  # noqa: C901
    @app.command
    def brief(
        *,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug
        run_command(
            "brief",
            lambda: _result(
                "brief",
                CliContext.resolve(store).queries().brief(BriefRequest(local)),
            ),
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @app.command(name="list")
    def list_items(
        *,
        kind: Literal["task", "decision"] | None = None,
        stage: TaskStage | None = None,
        decision_state: DecisionState | None = None,
        tag: str | None = None,
        waiting_on: str | None = None,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        limit: int = DEFAULT_LIMIT,
        debug: bool = False,
    ) -> None:
        del debug
        request = ListRequest(
            ItemKind(kind) if kind is not None else None,
            stage,
            decision_state,
            tag,
            waiting_on,
            local,
            validate_limit(limit),
        )
        run_command(
            "list",
            lambda: _result(
                "list",
                CliContext.resolve(store).queries().list(request),
                kind=(
                    "orchestration.task"
                    if kind == "task"
                    else "orchestration.decision"
                    if kind == "decision"
                    else None
                ),
            ),
            fmt=format,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )

    @app.command
    def show(
        item_id: str,
        /,
        *,
        raw: bool = False,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat | None = None,
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug
        context = CliContext.resolve(store)
        if raw:
            if columns:
                raise SystemExit(2)
            selected = "raw" if format is None else format
            run_command(
                "show",
                lambda: _result(
                    "show", context.queries().show_raw(RawShowRequest(_item_id(item_id)))
                ),
                fmt=selected,
                allowed=("raw", "json"),
                binary_recovery=True,
            )
            return
        selected = "table" if format is None else format
        run_command(
            "show",
            lambda: _result(
                "show",
                context.queries().show(ShowRequest(_item_id(item_id), local)),
                kind=(
                    "orchestration.task" if item_id.startswith("tsk_") else "orchestration.decision"
                ),
            ),
            fmt=selected,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )

    @app.command
    def inspect(
        path: PurePosixPath,
        /,
        *,
        raw: bool,
        store: str | None = None,
        format: OutputFormat | None = None,
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug
        if not raw or columns:
            raise SystemExit(2)
        context = CliContext.resolve(store)
        selected = "raw" if format is None else format
        run_command(
            "inspect",
            lambda: CommandResult("inspect", context.repository.read_raw(context.location, path)),
            fmt=selected,
            allowed=("raw", "json"),
            binary_recovery=True,
        )

    @app.command
    def search(
        query: str,
        /,
        *,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        limit: int = DEFAULT_LIMIT,
        debug: bool = False,
    ) -> None:
        del debug
        request = SearchRequest(query, local, False, validate_limit(limit))
        run_command(
            "search",
            lambda: _result(
                "search",
                CliContext.resolve(store).queries().search(request),
                kind=None,
            ),
            fmt=format,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )

    @app.command
    def trace(
        item_id: str,
        /,
        *,
        direction: TraceDirection = TraceDirection.BOTH,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        limit: int = DEFAULT_LIMIT,
        debug: bool = False,
    ) -> None:
        del debug
        request = TraceRequest(_item_id(item_id), direction, local, validate_limit(limit))
        run_command(
            "trace",
            lambda: _result("trace", CliContext.resolve(store).queries().trace(request)),
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @app.command(name="next")
    def next_items(
        *,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        limit: int = DEFAULT_LIMIT,
        debug: bool = False,
    ) -> None:
        del debug
        request = NextRequest(local, validate_limit(limit))
        run_command(
            "next",
            lambda: _result(
                "next",
                CliContext.resolve(store).queries().next(request),
                kind="orchestration.task",
            ),
            fmt=format,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )

    curate = app.command(App(name="curate"))
    assert isinstance(curate, App)

    @curate.command(name="next")
    def curate_next(
        *,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        limit: int = DEFAULT_LIMIT,
        debug: bool = False,
    ) -> None:
        del debug
        run_command(
            "curate next",
            lambda: CommandResult(
                "curate next",
                CliContext.resolve(store)
                .curation()
                .next(CurateNextRequest(local, validate_limit(limit))),
            ),
            fmt=format,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )

    history = app.command(App(name="history"))
    assert isinstance(history, App)

    @history.command(name="list")
    def history_list(
        *,
        outcome: TaskOutcome | None = None,
        tag: str | None = None,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        limit: int = DEFAULT_LIMIT,
        debug: bool = False,
    ) -> None:
        del debug
        request = HistoryListRequest(outcome, tag, local, validate_limit(limit))
        run_command(
            "history list",
            lambda: _result(
                "history list",
                CliContext.resolve(store).queries().history_list(request),
                kind="orchestration.task",
            ),
            fmt=format,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )

    @history.command(name="search")
    def history_search(
        query: str,
        /,
        *,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        limit: int = DEFAULT_LIMIT,
        debug: bool = False,
    ) -> None:
        del debug
        request = HistorySearchRequest(query, local, validate_limit(limit))
        run_command(
            "history search",
            lambda: _result(
                "history search",
                CliContext.resolve(store).queries().history_search(request),
                kind="orchestration.task",
            ),
            fmt=format,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )

    @history.command(name="show")
    def history_show(
        item_id: str,
        /,
        *,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug
        run_command(
            "history show",
            lambda: _result(
                "history show",
                CliContext.resolve(store)
                .queries()
                .history_show(HistoryShowRequest(TaskId(item_id), local)),
                kind="orchestration.task",
            ),
            fmt=format,
            allowed=("table", "json", "pipe", "raw"),
            columns=columns,
        )
