from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import MISSING, fields, replace
from pathlib import Path
from typing import cast

import pytest

from tests.builders import CHILD_STORE_ID, STORE_ID
from untaped_orchestration.application.federation import FederationService, UnidentifiedStoreError
from untaped_orchestration.application.ports import StoreLockTimeout
from untaped_orchestration.application.results import LoadedRecord, StoreLocation, StoreSnapshot
from untaped_orchestration.domain.models import Registry, Revision, StoreConfig
from untaped_orchestration.infrastructure.codec import RegistryCodec, StoreConfigCodec

SECOND_CHILD_ID = "sto_019f0000000070008000000000000002"
GRANDCHILD_ID = "sto_019f0000000070008000000000000003"


def _revision(seed: str) -> Revision:
    return Revision(f"sha256:{seed * 64}")


def _store(store_id: str) -> StoreConfig:
    raw = f"""schema = "untaped.orchestration.store/v1"
id = "{store_id}"
name = "Store"
visibility = "private"
timezone = "UTC"

[capabilities]
active_tasks = true

[curation]
inbox_review_days = 7
in_progress_review_days = 14

[brief]
pinned_decisions = []
max_decision_body_bytes = 4096
max_total_body_bytes = 16384
max_rows_per_section = 10
max_total_bytes = 32768
""".encode()
    return StoreConfigCodec().parse(raw)


def _registry(store_id: str, *children: tuple[str, str]) -> Registry:
    records = "".join(
        f'\n[[children]]\nid = "{child_id}"\npath = "{path}"\n' for child_id, path in children
    )
    return RegistryCodec().parse(
        (
            f'schema = "untaped.orchestration.registry/v1"\nstore_id = "{store_id}"\n{records}'
        ).encode()
    )


def _snapshot(
    root: Path,
    store_id: str,
    *children: tuple[str, str],
    real_root: Path | None = None,
    store_revision_seed: str = "a",
    registry_revision_seed: str = "b",
) -> StoreSnapshot:
    return StoreSnapshot(
        location=StoreLocation(root=root, real_root=real_root or root),
        store=_store(store_id),
        registry=_registry(store_id, *children),
        records=(),
        load_diagnostics=(),
        raw_index=(),
        store_revision=_revision(store_revision_seed),
        registry_revision=_revision(registry_revision_seed),
        store_config_revision=_revision(store_revision_seed),
    )


class ScriptedReader:
    def __init__(self, snapshots: Sequence[StoreSnapshot]) -> None:
        self._by_root = {snapshot.location.root: snapshot for snapshot in snapshots}
        self._discoveries: dict[Path, StoreLocation | Exception] = {
            root: snapshot.location for root, snapshot in self._by_root.items()
        }
        self.loads: list[tuple[Path, bool]] = []
        self.discovers: list[Path] = []

    def set_discovery(self, root: Path, value: StoreLocation | Exception) -> None:
        self._discoveries[root] = value

    def set_snapshot(self, snapshot: StoreSnapshot) -> None:
        self._by_root[snapshot.location.root] = snapshot

    def discover(self, start: Path, override: Path | None = None) -> StoreLocation:
        del start
        assert override is not None
        self.discovers.append(override)
        normalized = Path(os.path.normpath(override))
        value = self._discoveries.get(normalized)
        if value is None:
            raise FileNotFoundError(override)
        if isinstance(value, Exception):
            raise value
        return value

    def load_local(self, location: StoreLocation, *, headers_only: bool) -> StoreSnapshot:
        self.loads.append((location.root, headers_only))
        return self._by_root[location.root]

    def read_raw(self, location: StoreLocation, relative_path: object) -> object:
        raise AssertionError((location, relative_path))


class RecordingLocks:
    def __init__(
        self,
        *,
        on_enter: Callable[[], None] | None = None,
        timeout_location: StoreLocation | None = None,
    ) -> None:
        self.on_enter = on_enter
        self.timeout_location = timeout_location
        self.calls: list[tuple[tuple[StoreLocation, ...], float]] = []

    @contextmanager
    def acquire(
        self,
        locations: Sequence[StoreLocation],
        *,
        timeout: float,
    ) -> Iterator[None]:
        ordered = tuple(locations)
        self.calls.append((ordered, timeout))
        if self.timeout_location is not None:
            raise StoreLockTimeout(self.timeout_location)
        if self.on_enter is not None:
            self.on_enter()
        yield


def test_local_load_locks_and_rereads_only_the_selected_store_with_default_timeout() -> None:
    root = Path("/work/root")
    child = Path("/work/child")
    selected = _snapshot(root, STORE_ID, (CHILD_STORE_ID, "../child"))
    reader = ScriptedReader((selected, _snapshot(child, CHILD_STORE_ID)))
    locks = RecordingLocks()

    result = FederationService(reader, locks).load(
        selected.location,
        local=True,
        headers_only=False,
    )

    assert result.completeness.complete
    assert result.stores == (selected,)
    assert reader.discovers == [root]
    assert reader.loads == [(root, True), (root, False), (root, True)]
    assert locks.calls == [((selected.location,), 10.0)]


def test_run_exposes_file_reader_only_inside_the_ordered_lock_lease() -> None:
    root = Path("/work/root")
    selected = _snapshot(root, STORE_ID)
    reader = ScriptedReader((selected,))
    active = False

    class LeaseLocks:
        @contextmanager
        def acquire(self, locations, *, timeout):
            nonlocal active
            del locations, timeout
            active = True
            try:
                yield
            finally:
                active = False

    def action(lease):
        assert active
        assert lease.reader is reader
        return lease.snapshot

    result = FederationService(reader, LeaseLocks()).run(
        selected.location,
        local=True,
        action=action,
    )

    assert result.selected.location == selected.location
    assert reader.loads == [(root, True), (root, True), (root, True)]
    assert not active


def test_timeout_and_changed_anchor_never_expose_a_file_reader() -> None:
    root = Path("/work/root")
    selected = _snapshot(root, STORE_ID)
    timeout_reader = ScriptedReader((selected,))
    timeout = FederationService(
        timeout_reader,
        RecordingLocks(timeout_location=selected.location),
    ).run(
        selected.location,
        local=True,
        action=lambda lease: lease,
    )
    assert timeout.reader is None
    assert not timeout.snapshot.completeness.complete

    changed_reader = ScriptedReader((selected,))

    def change_anchor() -> None:
        changed_reader.set_snapshot(replace(selected, registry_revision=_revision("c")))

    changed = FederationService(changed_reader, RecordingLocks(on_enter=change_anchor)).run(
        selected.location,
        local=True,
        action=lambda lease: lease,
    )
    assert changed.reader is None
    assert not changed.snapshot.completeness.complete


def test_missing_registered_child_appearing_at_lock_entry_closes_the_lease() -> None:
    root = Path("/work/root")
    child_root = Path("/work/child")
    selected = _snapshot(root, STORE_ID, (CHILD_STORE_ID, "../child"))
    child = _snapshot(child_root, CHILD_STORE_ID)
    reader = ScriptedReader((selected, child))
    reader.set_discovery(child_root, FileNotFoundError(child_root))

    def appear() -> None:
        reader.set_discovery(child_root, child.location)

    lease = FederationService(reader, RecordingLocks(on_enter=appear)).run(
        selected.location,
        local=False,
        action=lambda value: value,
    )

    assert lease.reader is None
    assert not lease.snapshot.completeness.complete
    assert any(value.diagnostic.code == "ORC007" for value in lease.snapshot.completeness.entries)


def test_known_participant_disappearing_at_lock_entry_closes_the_lease() -> None:
    root = Path("/work/root")
    child_root = Path("/work/child")
    selected = _snapshot(root, STORE_ID, (CHILD_STORE_ID, "../child"))
    child = _snapshot(child_root, CHILD_STORE_ID)
    reader = ScriptedReader((selected, child))

    def disappear() -> None:
        reader.set_discovery(child_root, FileNotFoundError(child_root))

    lease = FederationService(reader, RecordingLocks(on_enter=disappear)).run(
        selected.location,
        local=False,
        action=lambda value: value,
    )

    assert lease.reader is None
    assert not lease.snapshot.completeness.complete
    assert any(value.diagnostic.code == "ORC007" for value in lease.snapshot.completeness.entries)


def test_recursive_resolution_uses_explicit_depth_first_and_global_lock_order() -> None:
    root = Path("/work/z-root")
    child = Path("/work/m-child")
    grandchild = Path("/work/a-grandchild")
    ambient = Path("/work/ambient")
    selected = _snapshot(root, STORE_ID, (CHILD_STORE_ID, "../m-child"))
    child_snapshot = _snapshot(
        child,
        CHILD_STORE_ID,
        (GRANDCHILD_ID, "../a-grandchild"),
    )
    grandchild_snapshot = _snapshot(grandchild, GRANDCHILD_ID)
    reader = ScriptedReader(
        (selected, child_snapshot, grandchild_snapshot, _snapshot(ambient, SECOND_CHILD_ID))
    )
    locks = RecordingLocks()

    result = FederationService(reader, locks).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert result.completeness.complete
    assert [snapshot.location.real_root for snapshot in result.stores] == [
        grandchild,
        child,
        root,
    ]
    assert [location.real_root for location in locks.calls[0][0]] == [grandchild, child, root]
    assert ambient not in reader.discovers
    assert all(headers_only for _, headers_only in reader.loads)
    assert reader.loads.count((root, True)) == 3
    assert reader.loads.count((child, True)) == 3
    assert reader.loads.count((grandchild, True)) == 3


def test_casefold_path_alias_is_incomplete_and_never_added_to_the_lock_set() -> None:
    root = Path("/work/root")
    upper = Path("/work/Child")
    lower = Path("/work/child")
    selected = _snapshot(
        root,
        STORE_ID,
        (CHILD_STORE_ID, "../Child"),
        (SECOND_CHILD_ID, "../child"),
    )
    reader = ScriptedReader(
        (
            selected,
            _snapshot(upper, CHILD_STORE_ID),
            _snapshot(lower, SECOND_CHILD_ID),
        )
    )
    locks = RecordingLocks()

    result = FederationService(reader, locks).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert result.completeness.missing_store_ids == (SECOND_CHILD_ID,)
    assert result.completeness.entries[0].expected_store_id.root == SECOND_CHILD_ID
    assert "case-fold" in result.completeness.entries[0].diagnostic.message
    assert result.completeness.entries[0].diagnostic.severity == "error"
    assert [location.real_root for location in locks.calls[0][0]] == [upper, root]


def test_timeout_uses_expected_id_for_the_affected_store_and_returns_partial_data() -> None:
    root = Path("/work/root")
    child = Path("/work/child")
    selected = _snapshot(root, STORE_ID, (CHILD_STORE_ID, "../child"))
    child_snapshot = _snapshot(child, CHILD_STORE_ID)
    reader = ScriptedReader((selected, child_snapshot))
    locks = RecordingLocks(timeout_location=child_snapshot.location)

    result = FederationService(reader, locks, lock_timeout=0.25).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert result.completeness.missing_store_ids == (CHILD_STORE_ID,)
    assert result.completeness.entries[0].diagnostic.code == "ORC007"
    assert result.completeness.entries[0].diagnostic.severity == "error"
    assert result.stores == (child_snapshot, selected)
    assert locks.calls[0][1] == 0.25


@pytest.mark.parametrize("changed_surface", ["anchor", "registry"])
def test_changed_anchor_or_registry_during_lock_acquisition_is_never_accepted_complete(
    changed_surface: str,
) -> None:
    root = Path("/work/root")
    selected = _snapshot(root, STORE_ID)
    reader = ScriptedReader((selected,))
    changed = _snapshot(
        root,
        STORE_ID,
        store_revision_seed="c" if changed_surface == "anchor" else "a",
        registry_revision_seed="d" if changed_surface == "registry" else "b",
    )
    locks = RecordingLocks(on_enter=lambda: reader.set_snapshot(changed))

    result = FederationService(reader, locks).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert result.completeness.missing_store_ids == (STORE_ID,)
    assert result.completeness.entries[0].diagnostic.code == "ORC007"
    assert result.completeness.entries[0].diagnostic.severity == "error"
    assert "changed" in result.completeness.entries[0].diagnostic.message


def test_symlink_retarget_between_resolution_and_locked_reread_is_incomplete() -> None:
    root = Path("/work/root-link")
    first_real = Path("/real/first")
    second_real = Path("/real/second")
    selected = _snapshot(root, STORE_ID, real_root=first_real)
    reader = ScriptedReader((selected,))
    locks = RecordingLocks(
        on_enter=lambda: reader.set_discovery(
            root,
            StoreLocation(root=root, real_root=second_real),
        )
    )

    result = FederationService(reader, locks).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert result.completeness.missing_store_ids == (STORE_ID,)
    assert "path changed" in result.completeness.entries[0].diagnostic.message
    assert result.selected == selected
    assert result.stores == (selected,)


def test_child_symlink_retarget_retains_only_the_optimistically_resolved_edge_and_records() -> None:
    root = Path("/work/root")
    child_link = Path("/work/child-link")
    first_real = Path("/real/first-child")
    second_real = Path("/real/second-child")
    record = cast(LoadedRecord, object())
    selected = _snapshot(root, STORE_ID, (CHILD_STORE_ID, "../child-link"))
    child = replace(
        _snapshot(child_link, CHILD_STORE_ID, real_root=first_real),
        records=(record,),
    )
    reader = ScriptedReader((selected, child))
    locks = RecordingLocks(
        on_enter=lambda: reader.set_discovery(
            child_link,
            StoreLocation(root=child_link, real_root=second_real),
        )
    )

    result = FederationService(reader, locks).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert result.completeness.missing_store_ids == (CHILD_STORE_ID,)
    assert result.selected.registry == selected.registry
    assert result.stores == (child, selected)
    assert result.stores[0].records == (record,)


def test_selected_store_without_recoverable_anchor_or_registry_identity_fails_explicitly() -> None:
    root = Path("/work/root")
    unidentified = StoreSnapshot(
        location=StoreLocation(root=root, real_root=root),
        store=None,
        registry=None,
        records=(),
        load_diagnostics=(),
        raw_index=(),
        store_revision=_revision("a"),
        registry_revision=None,
        store_config_revision=_revision("c"),
    )
    reader = ScriptedReader((unidentified,))

    with pytest.raises(UnidentifiedStoreError) as captured:
        FederationService(reader, RecordingLocks()).load(
            unidentified.location,
            local=False,
            headers_only=True,
        )

    assert captured.value.location == unidentified.location


@pytest.mark.parametrize("change", ["add-child", "remove-child"])
def test_changed_registry_never_exposes_unresolved_under_lock_graph_or_records(
    change: str,
) -> None:
    root = Path("/work/root")
    child = Path("/work/child")
    unresolved = cast(LoadedRecord, object())
    if change == "add-child":
        optimistic = _snapshot(root, STORE_ID)
        changed = replace(
            _snapshot(
                root,
                STORE_ID,
                (CHILD_STORE_ID, "../child"),
                registry_revision_seed="d",
            ),
            records=(unresolved,),
        )
        snapshots = (optimistic, _snapshot(child, CHILD_STORE_ID))
        expected_stores = (optimistic,)
    else:
        optimistic = _snapshot(root, STORE_ID, (CHILD_STORE_ID, "../child"))
        child_snapshot = _snapshot(child, CHILD_STORE_ID)
        changed = replace(
            _snapshot(root, STORE_ID, registry_revision_seed="d"),
            records=(unresolved,),
        )
        snapshots = (optimistic, child_snapshot)
        expected_stores = (child_snapshot, optimistic)
    reader = ScriptedReader(snapshots)
    locks = RecordingLocks(on_enter=lambda: reader.set_snapshot(changed))

    result = FederationService(reader, locks).load(
        optimistic.location,
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert result.selected == optimistic
    assert result.selected.registry == optimistic.registry
    assert result.selected.records == ()
    assert result.stores == expected_stores


def test_store_config_revision_is_a_required_snapshot_invariant() -> None:
    revision_field = next(
        field for field in fields(StoreSnapshot) if field.name == "store_config_revision"
    )

    assert revision_field.default is MISSING


@pytest.mark.parametrize("registry_state", ["missing", "wrong-id"])
def test_invalid_selected_registry_is_error_severity_in_recursive_mode(
    registry_state: str,
) -> None:
    root = Path("/work/root")
    selected = _snapshot(root, STORE_ID)
    registry = None if registry_state == "missing" else _registry(CHILD_STORE_ID)
    selected = replace(selected, registry=registry)

    result = FederationService(ScriptedReader((selected,)), RecordingLocks()).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert not result.completeness.complete
    assert result.completeness.entries[0].diagnostic.severity == "error"


def test_generic_timeout_with_a_location_shape_is_not_mistaken_for_port_timeout() -> None:
    root = Path("/work/root")
    selected = _snapshot(root, STORE_ID)
    reader = ScriptedReader((selected,))

    class ShapedTimeout(TimeoutError):
        def __init__(self, location: StoreLocation) -> None:
            self.location = location
            super().__init__("not the application lock timeout")

    class ShapedTimeoutLocks:
        @contextmanager
        def acquire(
            self,
            locations: Sequence[StoreLocation],
            *,
            timeout: float,
        ) -> Iterator[None]:
            del timeout
            raise ShapedTimeout(locations[0])
            yield

    with pytest.raises(ShapedTimeout):
        FederationService(reader, ShapedTimeoutLocks()).load(
            selected.location,
            local=False,
            headers_only=True,
        )


@pytest.mark.parametrize(
    ("discovery_error", "expected_severity"),
    [
        (FileNotFoundError("unavailable"), "warning"),
        (ValueError("invalid registered path"), "error"),
    ],
)
def test_child_discovery_severity_distinguishes_availability_from_invalidity(
    discovery_error: Exception,
    expected_severity: str,
) -> None:
    root = Path("/work/root")
    candidate = Path("/work/child")
    selected = _snapshot(root, STORE_ID, (CHILD_STORE_ID, "../child"))
    reader = ScriptedReader((selected,))
    reader.set_discovery(candidate, discovery_error)

    result = FederationService(reader, RecordingLocks()).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert result.completeness.entries[0].diagnostic.severity == expected_severity


def test_untrusted_children_are_locked_in_global_order_but_missing_children_are_not() -> None:
    root = Path("/work/z-root")
    malformed_root = Path("/work/a-malformed")
    missing_root = Path("/work/b-missing")
    wrong_root = Path("/work/m-wrong")
    selected = _snapshot(
        root,
        STORE_ID,
        (CHILD_STORE_ID, "../a-malformed"),
        (SECOND_CHILD_ID, "../m-wrong"),
        (GRANDCHILD_ID, "../b-missing"),
    )
    malformed = replace(_snapshot(malformed_root, CHILD_STORE_ID), store=None)
    wrong = _snapshot(wrong_root, GRANDCHILD_ID)
    reader = ScriptedReader((selected, malformed, wrong))
    reader.set_discovery(missing_root, FileNotFoundError("missing"))
    locks = RecordingLocks()

    result = FederationService(reader, locks).load(
        selected.location,
        local=False,
        headers_only=True,
    )

    assert result.stores == (selected,)
    assert [location.real_root for location in locks.calls[0][0]] == [
        malformed_root,
        wrong_root,
        root,
    ]
    assert missing_root not in {location.real_root for location in locks.calls[0][0]}
