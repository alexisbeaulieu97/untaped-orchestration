from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from untaped_orchestration.application.curation import CurationReadService, CurationService
from untaped_orchestration.application.decisions import DecisionService
from untaped_orchestration.application.federation import (
    FederationRegistryService,
    FederationService,
)
from untaped_orchestration.application.item_relations import ChangeEvidence, ChangeLink
from untaped_orchestration.application.item_support import (
    MutationExecutionScope,
    MutationScope,
)
from untaped_orchestration.application.items import (
    CreateDecision,
    CreateTask,
    UpdateDecision,
    UpdateTask,
)
from untaped_orchestration.application.maintenance import (
    CheckStore,
    FormatStore,
    RecursiveMaintenanceService,
    RenderStore,
)
from untaped_orchestration.application.mutations import MutationExecutor
from untaped_orchestration.application.queries import QueryService
from untaped_orchestration.application.query_models import QueryScope
from untaped_orchestration.application.results import FederatedSnapshot, StoreLocation
from untaped_orchestration.application.tasks import TaskService
from untaped_orchestration.infrastructure.locking import FileLockManager
from untaped_orchestration.infrastructure.repository import FilesystemStoreRepository
from untaped_orchestration.infrastructure.views import MarkdownViewRenderer


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class AlreadyLocked:
    @contextmanager
    def acquire(
        self,
        locations: Sequence[StoreLocation],
        *,
        timeout: float,
    ) -> Iterator[None]:
        del locations, timeout
        yield


@dataclass(slots=True)
class CliContext:
    repository: FilesystemStoreRepository
    locks: FileLockManager
    views: MarkdownViewRenderer
    clock: SystemClock
    location: StoreLocation
    federation: FederationService
    scope: MutationScope
    executor: MutationExecutor

    @classmethod
    def resolve(cls, store: str | None) -> CliContext:
        repository = FilesystemStoreRepository()
        locks = FileLockManager()
        views = MarkdownViewRenderer()
        override = Path(store) if store is not None else None
        location = repository.discover(Path.cwd(), override)
        federation = FederationService(repository, locks)
        recursive_snapshot = federation.load(location, local=False, headers_only=True)
        unlocked_federation = FederationService(repository, AlreadyLocked())

        def recursive() -> FederatedSnapshot:
            return unlocked_federation.load(location, local=False, headers_only=False)

        def local() -> FederatedSnapshot:
            return unlocked_federation.load(location, local=True, headers_only=False)

        scope = MutationScope(
            MutationExecutionScope(
                tuple(store.location for store in recursive_snapshot.stores),
                location,
                recursive,
            ),
            MutationExecutionScope((location,), location, local),
        )
        executor = MutationExecutor(repository, repository, locks, views, projector=repository)
        return cls(
            repository,
            locks,
            views,
            SystemClock(),
            location,
            federation,
            scope,
            executor,
        )

    def queries(self) -> QueryService:
        return QueryService(
            QueryScope(
                recursive=lambda: self.federation.load(
                    self.location, local=False, headers_only=True
                ),
                local=lambda: self.federation.load(self.location, local=True, headers_only=True),
                recursive_run=lambda action: self.federation.run(
                    self.location,
                    local=False,
                    action=lambda lease: action(lease.snapshot, lease.reader),
                ),
                local_run=lambda action: self.federation.run(
                    self.location,
                    local=True,
                    action=lambda lease: action(lease.snapshot, lease.reader),
                ),
            ),
            self.repository,
            self.clock,
        )

    def tasks(self) -> TaskService:
        return TaskService(
            self.executor,
            self.repository,
            self.clock,
            self.scope,
        )

    def decisions(self) -> DecisionService:
        return DecisionService(
            self.executor,
            self.repository,
            self.clock,
            self.scope,
        )

    def curation(self) -> CurationService:
        return CurationService(
            self.executor,
            self.repository,
            self.clock,
            self.scope,
        )

    def curation_reads(self) -> CurationReadService:
        return CurationReadService(self.federation, self.location, self.clock)

    def registry(self) -> FederationRegistryService:
        return FederationRegistryService(
            self.repository,
            self.repository,
            self.locks,
            self.views,
            self.repository,
        )

    def maintenance(self) -> RecursiveMaintenanceService:
        formatter = FormatStore(
            self.repository,
            self.repository,
            AlreadyLocked(),
            self.views,
            self.repository,
        )
        renderer = RenderStore(
            self.repository,
            self.repository,
            AlreadyLocked(),
            self.views,
        )
        return RecursiveMaintenanceService(
            self.federation,
            self.repository,
            self.repository,
            self.views,
            local_formatter=formatter,
            local_renderer=renderer,
        )

    def check_store(self) -> CheckStore:
        return CheckStore(self.repository, self.locks, self.views)

    def create_task(self) -> CreateTask:
        return CreateTask(self.executor, self.repository, self.clock)

    def update_task(self) -> UpdateTask:
        return UpdateTask(self.executor, self.repository)

    def create_decision(self) -> CreateDecision:
        return CreateDecision(self.executor, self.repository, self.clock)

    def update_decision(self) -> UpdateDecision:
        return UpdateDecision(self.executor, self.repository)

    def links(self) -> ChangeLink:
        return ChangeLink(self.executor, self.repository)

    def evidence(self) -> ChangeEvidence:
        return ChangeEvidence(self.executor, self.repository)
