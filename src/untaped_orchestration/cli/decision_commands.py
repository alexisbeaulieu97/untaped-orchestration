from __future__ import annotations

from pathlib import Path

from cyclopts import App

from untaped_orchestration.application.decisions import (
    DecisionGuard,
    RetireDecisionRequest,
    SupersedeDecisionRequest,
)
from untaped_orchestration.application.item_support import (
    CreateDecisionRequest,
    UpdateDecisionRequest,
)
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.cli.options import (
    ColumnsOption,
    OutputFormat,
    read_body_file,
    usage_value,
)
from untaped_orchestration.cli.output import CommandResult, run_command
from untaped_orchestration.domain.ids import DecisionId, Slug
from untaped_orchestration.domain.models import Revision


def _body(path: Path | None) -> bytes:
    return b"" if path is None else read_body_file(path)


def _revision(value: str | None) -> Revision | None:
    return None if value is None else _required_revision(value)


def _required_revision(value: str) -> Revision:
    return usage_value(lambda: Revision(value))


def _decision_id(value: str) -> DecisionId:
    return usage_value(lambda: DecisionId(value))


def _slug(value: str) -> Slug:
    return usage_value(lambda: Slug(value))


def _guard(value: str | None, force: bool) -> None:
    if force == (value is not None):
        raise SystemExit(2)


def _predecessors(
    ids: tuple[DecisionId, ...],
    revisions: tuple[str, ...],
    force: bool,
) -> tuple[DecisionGuard, ...]:
    if len(ids) != len(set(ids)):
        raise SystemExit(2)
    if force:
        if revisions:
            raise SystemExit(2)
        return tuple(DecisionGuard(item_id, None) for item_id in ids)
    parsed = {}
    for value in revisions:
        item, separator, revision = value.partition("=")
        if not separator:
            raise SystemExit(2)
        parsed[_decision_id(item)] = _required_revision(revision)
    if set(parsed) != set(ids):
        raise SystemExit(2)
    return tuple(DecisionGuard(item_id, parsed[item_id]) for item_id in ids)


def register(app: App) -> None:
    decisions = app.command(App(name="decision"))
    assert isinstance(decisions, App)

    @decisions.command(name="create")
    def create(
        *,
        id: str,
        title: str,
        body_file: Path | None = None,
        tag: tuple[str, ...] = (),
        if_store_revision: str,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            result = context.create_decision().execute(
                context.scope,
                CreateDecisionRequest(
                    _decision_id(id),
                    title,
                    _body(body_file),
                    tuple(_slug(value) for value in tag),
                    _required_revision(if_store_revision),
                ),
            )
            return CommandResult("decision create", result)

        run_command(
            "decision create",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @decisions.command(name="update")
    def update(
        item_id: str,
        /,
        *,
        title: str | None = None,
        body_file: Path | None = None,
        tag: tuple[str, ...] | None = None,
        clear_tags: bool = False,
        if_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_revision, force_current)
        if tag is not None and clear_tags:
            raise SystemExit(2)
        if title is None and body_file is None and tag is None and not clear_tags:
            raise SystemExit(2)

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            result = context.update_decision().execute(
                context.scope,
                UpdateDecisionRequest(
                    _decision_id(item_id),
                    _revision(if_revision),
                    force_current,
                    title,
                    None if body_file is None else _body(body_file),
                    ()
                    if clear_tags
                    else (None if tag is None else tuple(_slug(value) for value in tag)),
                ),
            )
            return CommandResult("decision update", result)

        run_command(
            "decision update",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @decisions.command(name="supersede")
    def supersede(
        *,
        id: str,
        title: str,
        predecessor: tuple[str, ...],
        body_file: Path | None = None,
        tag: tuple[str, ...] = (),
        if_predecessor_revision: tuple[str, ...] = (),
        if_store_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_store_revision, force_current)
        guards = _predecessors(
            tuple(_decision_id(value) for value in predecessor),
            if_predecessor_revision,
            force_current,
        )

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            result = context.decisions().supersede(
                SupersedeDecisionRequest(
                    _decision_id(id),
                    title,
                    _body(body_file),
                    tuple(_slug(value) for value in tag),
                    guards,
                    _revision(if_store_revision),
                    force_current,
                )
            )
            return CommandResult("decision supersede", result)

        run_command(
            "decision supersede",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @decisions.command(name="retire")
    def retire(
        item_id: str,
        /,
        *,
        note: str,
        if_revision: str | None = None,
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
        run_command(
            "decision retire",
            lambda: CommandResult(
                "decision retire",
                CliContext.resolve(store)
                .decisions()
                .retire(
                    RetireDecisionRequest(
                        _decision_id(item_id),
                        note,
                        _revision(if_revision),
                        _revision(if_store_revision),
                        force_current,
                    )
                ),
            ),
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )
