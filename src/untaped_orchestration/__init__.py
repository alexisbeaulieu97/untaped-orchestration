"""Git-native typed orchestration for repository tasks and decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cyclopts import App

__all__ = ["app"]


def __getattr__(name: str) -> App:
    """Load the CLI application only when callers request it."""
    if name == "app":
        from untaped_orchestration.cli import app  # noqa: PLC0415

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
