from __future__ import annotations

from collections.abc import Callable

from cyclopts import App

from untaped_orchestration.application.item_support import EvidenceRequest, LinkRequest
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.cli.options import ColumnsOption, OutputFormat, usage_value
from untaped_orchestration.cli.output import CommandResult, run_command
from untaped_orchestration.domain.evidence import EvidenceReference, EvidenceRelation
from untaped_orchestration.domain.ids import DecisionId, StoreId, TaskId
from untaped_orchestration.domain.models import LinkRelation, Revision


def _item(value: str) -> TaskId | DecisionId:
    return usage_value(lambda: TaskId(value) if value.startswith("tsk_") else DecisionId(value))


def _revision(value: str | None) -> Revision | None:
    return None if value is None else usage_value(lambda: Revision(value))


def _guard(value: str | None, force: bool) -> None:
    if force == (value is not None):
        raise SystemExit(2)


def register(app: App) -> None:
    link = app.command(App(name="link"))
    assert isinstance(link, App)

    def register_link(name: str, change: Callable[..., object]) -> None:
        @link.command(name=name)
        def command(
            source: str,
            /,
            *,
            relation: LinkRelation,
            target_store: str,
            target: str,
            if_revision: str | None = None,
            force_current: bool = False,
            store: str | None = None,
            format: OutputFormat = "table",
            columns: ColumnsOption = (),
            debug: bool = False,
        ) -> None:
            del debug
            _guard(if_revision, force_current)

            def action() -> CommandResult:
                context = CliContext.resolve(store)
                result = change(
                    context.links(),
                    context.scope,
                    LinkRequest(
                        _item(source),
                        relation,
                        usage_value(lambda: StoreId(target_store)),
                        _item(target),
                        _revision(if_revision),
                        force_current,
                    ),
                )
                return CommandResult(f"link {name}", result)

            run_command(
                f"link {name}",
                action,
                fmt=format,
                allowed=("table", "json"),
                columns=columns,
            )

    register_link("add", lambda service, scope, request: service.add(scope, request))
    register_link("remove", lambda service, scope, request: service.remove(scope, request))

    evidence = app.command(App(name="evidence"))
    assert isinstance(evidence, App)

    def register_evidence(name: str, change: Callable[..., object]) -> None:
        @evidence.command(name=name)
        def command(
            item_id: str,
            /,
            *,
            relation: EvidenceRelation,
            reference: str,
            if_revision: str | None = None,
            force_current: bool = False,
            store: str | None = None,
            format: OutputFormat = "table",
            columns: ColumnsOption = (),
            debug: bool = False,
        ) -> None:
            del debug
            _guard(if_revision, force_current)

            def action() -> CommandResult:
                context = CliContext.resolve(store)
                result = change(
                    context.evidence(),
                    context.scope,
                    EvidenceRequest(
                        _item(item_id),
                        relation,
                        usage_value(lambda: EvidenceReference(reference)),
                        _revision(if_revision),
                        force_current,
                    ),
                )
                return CommandResult(f"evidence {name}", result)

            run_command(
                f"evidence {name}",
                action,
                fmt=format,
                allowed=("table", "json"),
                columns=columns,
            )

    register_evidence("add", lambda service, scope, request: service.add(scope, request))
    register_evidence("remove", lambda service, scope, request: service.remove(scope, request))
