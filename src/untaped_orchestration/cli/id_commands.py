from __future__ import annotations

import sys
from typing import Literal
from uuid import uuid7

from cyclopts import App

from untaped_orchestration.cli.options import ColumnsOption, OutputFormat, validate_format
from untaped_orchestration.cli.output import CommandResult, emit_encoded, encode_result


def register(app: App) -> None:
    ids = app.command(App(name="id", help="Allocate caller-stable typed IDs."))
    assert isinstance(ids, App)

    @ids.command(name="new")
    def new_id(
        kind: Literal["store", "task", "decision"],
        /,
        *,
        format: OutputFormat = "table",
        columns: ColumnsOption = (),
        debug: bool = False,
    ) -> None:
        del debug
        try:
            fmt = validate_format(format, allowed=("table", "json", "raw"))
        except ValueError as error:
            sys.stderr.write(f"error: {error}\n")
            raise SystemExit(2) from error
        if fmt == "raw" and columns:
            sys.stderr.write("error: id new --format raw does not accept --columns/-c\n")
            raise SystemExit(2)
        prefix = {"store": "sto", "task": "tsk", "decision": "dec"}[kind]
        item_id = f"{prefix}_{uuid7().hex}"
        emit_encoded(
            encode_result(
                CommandResult("id new", {"kind": kind, "id": item_id}),
                fmt=fmt,
                columns=columns,
            )
        )
