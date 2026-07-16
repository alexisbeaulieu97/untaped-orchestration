from __future__ import annotations

import json
from collections.abc import Callable

from untaped_orchestration.application.ports import StoreReader
from untaped_orchestration.application.query_models import SearchHit
from untaped_orchestration.application.query_projection import SafeProjection, row_for
from untaped_orchestration.domain.canonical import CanonicalItem, canonical_item_table
from untaped_orchestration.domain.models import ArchivedTask


def canonical_search_text(metadata: CanonicalItem, body: bytes) -> str:
    table = canonical_item_table(metadata)
    canonical = json.dumps(table, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{canonical}\n{body.decode('utf-8', errors='replace')}"


def stream_search(
    projection: SafeProjection,
    reader: StoreReader,
    query: str,
    *,
    limit: int,
    archived: bool,
    predicate: Callable[[CanonicalItem], bool] | None = None,
) -> tuple[tuple[SearchHit, ...], int]:
    needle = query.casefold()
    hits: list[SearchHit] = []
    total = 0
    for store in projection.snapshot.stores:
        if store.store is None:
            continue
        for record in store.records:
            if isinstance(record.metadata, ArchivedTask) is not archived:
                continue
            if predicate is not None and not predicate(record.metadata):
                continue
            body = reader.read_item_body(store.location, record.path)
            text = canonical_search_text(record.metadata, body)
            if needle not in text.casefold():
                continue
            total += 1
            if len(hits) < limit:
                hits.append(SearchHit(row_for(projection, store.store.id, record), text[:512]))
    return tuple(hits), total
