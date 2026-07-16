from pathlib import Path
from types import SimpleNamespace

import pytest

from untaped_orchestration.application.federation import FederationService
from untaped_orchestration.application.results import StoreLocation
from untaped_orchestration.cli.context import CliContext
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository


def test_context_resolution_discovers_only_and_builds_lazy_scope_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = StoreLocation(Path("/work/store"), Path("/work/store"))
    events: list[str] = []

    def discover(self, start: Path, override: Path | None = None) -> StoreLocation:
        del self, start, override
        events.append("discover")
        return location

    def optimistic_locations(self, selected: StoreLocation) -> tuple[StoreLocation, ...]:
        del self
        assert selected == location
        events.append("optimistic")
        return (location,)

    def load(self, selected, *, local, headers_only):
        del self, selected, local, headers_only
        events.append("load")
        return SimpleNamespace(stores=(SimpleNamespace(location=location),))

    monkeypatch.setattr(FilesystemStoreRepository, "discover", discover)
    monkeypatch.setattr(
        FederationService,
        "optimistic_locations",
        optimistic_locations,
        raising=False,
    )
    monkeypatch.setattr(FederationService, "load", load)

    context = CliContext.resolve(None)

    assert events == ["discover"]
    local_scope = context.scope.selected_local()
    assert events == ["discover"]
    assert local_scope.locations == (location,)

    recursive_scope = context.scope.recursive()
    assert events == ["discover", "optimistic"]
    assert recursive_scope.locations == (location,)
