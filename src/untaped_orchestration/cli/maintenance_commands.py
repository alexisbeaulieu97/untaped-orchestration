from __future__ import annotations

from pathlib import Path, PurePosixPath

from cyclopts import App

from untaped_orchestration.application.bootstrap import InitializeStore, InitRequest
from untaped_orchestration.application.curation import AcknowledgeRequest, SnoozeRequest
from untaped_orchestration.application.import_operations import ImportRequest, ImportService
from untaped_orchestration.application.maintenance import (
    RecursiveCheckRequest,
    RecursiveFormatRequest,
)
from untaped_orchestration.application.repair_operations import (
    RepairFrontmatterRequest,
    RepairService,
)
from untaped_orchestration.application.tasks import RepairDuplicateRequest
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.cli.options import OutputFormat
from untaped_orchestration.cli.output import CommandResult, run_command
from untaped_orchestration.domain.ids import TaskId
from untaped_orchestration.domain.models import Revision
from untaped_orchestration.domain.time import CalendarDate
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


def _revision(value: str | None) -> Revision | None:
    return None if value is None else Revision(value)


def _guard(value: str | None, force_current: bool) -> tuple[Revision | None, bool]:
    if force_current == (value is not None):
        raise SystemExit(2)
    return _revision(value), force_current


def register(app: App) -> None:  # noqa: C901
    @app.command
    def init(
        path: Path,
        /,
        *,
        store_id: str,
        name: str,
        timezone: str,
        public: bool = False,
        decisions_only: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug

        def action() -> CommandResult:
            repository = FilesystemStoreRepository()
            receipt = InitializeStore(
                repository,
                repository,
                FileLockManager(),
                MarkdownViewRenderer(),
            ).execute(InitRequest(path, store_id, name, timezone, public, decisions_only))
            return CommandResult("init", receipt)

        run_command("init", action, fmt=format, allowed=("table", "json"), columns=columns)

    @app.command
    def check(
        *,
        require_children: bool = False,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            result = context.maintenance().check(
                RecursiveCheckRequest(context.location, local, require_children)
            )
            return CommandResult(
                "check",
                result.checks,
                complete=result.complete,
                diagnostics=result.diagnostics,
                exit_code=0 if result.valid else 1,
            )

        run_command("check", action, fmt=format, allowed=("table", "json"), columns=columns)

    @app.command
    def fmt(
        *,
        check: bool = False,
        write: bool = False,
        if_store_revision: str | None = None,
        store: str | None = None,
        local: bool = False,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug
        if check == write or (write and (not local or if_store_revision is None)):
            raise SystemExit(2)

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            request = RecursiveFormatRequest(context.location, local)
            result = (
                context.maintenance().fmt_check(request)
                if check
                else context.maintenance().fmt_write(
                    request, expected_store_revision=_revision(if_store_revision)
                )
            )
            return CommandResult("fmt", result, exit_code=0 if result.matches else 1)

        run_command("fmt", action, fmt=format, allowed=("table", "json"), columns=columns)

    @app.command
    def render(
        *,
        check: bool = False,
        write: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug
        if check == write:
            raise SystemExit(2)

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            result = (
                context.maintenance().render_check(context.location)
                if check
                else context.maintenance().render_write(context.location)
            )
            return CommandResult("render", result, exit_code=0 if result.matches else 1)

        run_command("render", action, fmt=format, allowed=("table", "json"), columns=columns)

    repair = app.command(App(name="repair"))
    assert isinstance(repair, App)

    @repair.command(name="frontmatter")
    def repair_frontmatter(
        path: PurePosixPath,
        /,
        *,
        frontmatter_file: Path,
        if_revision: str,
        body_file: Path | None = None,
        apply: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            service = RepairService(
                context.repository,
                context.repository,
                context.locks,
                context.views,
                duplicate_repair=context.tasks(),
            )
            result = service.frontmatter(
                RepairFrontmatterRequest(
                    context.location,
                    path,
                    frontmatter_file,
                    Revision(if_revision),
                    body_file,
                    apply,
                )
            )
            return CommandResult("repair frontmatter", result)

        run_command(
            "repair frontmatter",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @repair.command(name="duplicate")
    def repair_duplicate(
        item_id: str,
        /,
        *,
        if_active_revision: str,
        if_archive_revision: str,
        apply: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            result = context.tasks().repair_duplicate(
                RepairDuplicateRequest(
                    TaskId(item_id),
                    Revision(if_active_revision),
                    Revision(if_archive_revision),
                    apply,
                )
            )
            return CommandResult("repair duplicate", result)

        run_command(
            "repair duplicate",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    curate = app.resolved_commands()["curate"]

    @curate.command(name="acknowledge")
    def acknowledge(
        item_id: str,
        /,
        *,
        if_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_revision, force_current)

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            typed = (
                TaskId(item_id)
                if item_id.startswith("tsk_")
                else __import__(
                    "untaped_orchestration.domain.ids", fromlist=["DecisionId"]
                ).DecisionId(item_id)
            )
            result = context.curation().acknowledge(
                AcknowledgeRequest(typed, _revision(if_revision), force_current)
            )
            return CommandResult("curate acknowledge", result)

        run_command(
            "curate acknowledge",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    @curate.command(name="snooze")
    def snooze(
        item_id: str,
        /,
        *,
        until: str,
        if_revision: str | None = None,
        force_current: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug
        _guard(if_revision, force_current)

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            typed = (
                TaskId(item_id)
                if item_id.startswith("tsk_")
                else __import__(
                    "untaped_orchestration.domain.ids", fromlist=["DecisionId"]
                ).DecisionId(item_id)
            )
            result = context.curation().snooze(
                SnoozeRequest(
                    typed,
                    CalendarDate(until),
                    _revision(if_revision),
                    force_current,
                )
            )
            return CommandResult("curate snooze", result)

        run_command(
            "curate snooze",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )

    store_app = app.resolved_commands()["store"]

    @store_app.command(name="import")
    def import_store(
        manifest: Path,
        /,
        *,
        if_clean: bool = False,
        apply: bool = False,
        store: str | None = None,
        format: OutputFormat = "table",
        columns: tuple[str, ...] = (),
        debug: bool = False,
    ) -> None:
        del debug

        def action() -> CommandResult:
            context = CliContext.resolve(store)
            service = ImportService(
                context.repository,
                context.executor,
                context.views,
                locations=context.scope.recursive.locations,
                load=context.scope.recursive.load,
            )
            result = service.execute(ImportRequest(context.location, manifest, apply, if_clean))
            return CommandResult("store import", result)

        run_command(
            "store import",
            action,
            fmt=format,
            allowed=("table", "json"),
            columns=columns,
        )
