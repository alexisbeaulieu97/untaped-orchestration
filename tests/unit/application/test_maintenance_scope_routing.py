from __future__ import annotations

from pathlib import Path

import pytest

from untaped_orchestration.application.maintenance import (
    RecursiveCheckRequest,
    RecursiveFormatRequest,
    RecursiveMaintenanceService,
)
from untaped_orchestration.application.results import StoreLocation
from untaped_orchestration.domain.models import Revision


class Routed(Exception):
    pass


class RecordingFederation:
    def __init__(self) -> None:
        self.locals: list[bool] = []

    def run(self, location, *, local, action, **kwargs):
        del location, action, kwargs
        self.locals.append(local)
        raise Routed


@pytest.mark.parametrize(
    ("operation", "expected_local"),
    [
        ("check-default", False),
        ("check-local", True),
        ("fmt-default", False),
        ("fmt-local", True),
        ("fmt-write", True),
        ("render-check", True),
        ("render-write", True),
    ],
)
def test_maintenance_routes_federation_scope(operation: str, expected_local: bool) -> None:
    federation = RecordingFederation()
    service = RecursiveMaintenanceService(
        federation,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        local_formatter=object(),  # type: ignore[arg-type]
        local_renderer=object(),  # type: ignore[arg-type]
    )
    location = StoreLocation(Path("/work/root"), Path("/work/root"))

    with pytest.raises(Routed):
        match operation:
            case "check-default":
                service.check(RecursiveCheckRequest(location))
            case "check-local":
                service.check(RecursiveCheckRequest(location, local=True))
            case "fmt-default":
                service.fmt_check(RecursiveFormatRequest(location))
            case "fmt-local":
                service.fmt_check(RecursiveFormatRequest(location, local=True))
            case "fmt-write":
                service.fmt_write(
                    RecursiveFormatRequest(location, local=True),
                    expected_store_revision=Revision("sha256:" + "a" * 64),
                )
            case "render-check":
                service.render_check(location)
            case "render-write":
                service.render_write(location)
            case _:
                raise AssertionError(operation)

    assert federation.locals == [expected_local]
