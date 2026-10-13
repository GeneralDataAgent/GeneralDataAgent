"""Deterministic downstream impact analysis over an explicit lineage graph."""

from collections import deque
from collections.abc import Iterable

from generaldataagent.contracts.data_engineering import (
    ImpactAnalysis,
    ImpactedAsset,
    LineageAsset,
)


def analyze_impact(
    assets: Iterable[LineageAsset],
    changed_assets: Iterable[str],
) -> ImpactAnalysis:
    catalog: dict[str, LineageAsset] = {}
    for asset in assets:
        if asset.asset_id in catalog:
            raise ValueError(f"duplicate lineage asset: {asset.asset_id}")
        catalog[asset.asset_id] = asset

    changed = tuple(dict.fromkeys(changed_assets))
    if not changed:
        raise ValueError("impact analysis requires at least one changed asset")
    unknown = set(changed) - catalog.keys()
    if unknown:
        raise ValueError(f"unknown changed assets: {sorted(unknown)}")
    unknown_dependencies = {
        dependency
        for asset in catalog.values()
        for dependency in asset.depends_on
        if dependency not in catalog
    }
    if unknown_dependencies:
        raise ValueError(f"unknown lineage dependencies: {sorted(unknown_dependencies)}")

    downstream: dict[str, list[str]] = {asset_id: [] for asset_id in catalog}
    for asset in catalog.values():
        for dependency in asset.depends_on:
            downstream[dependency].append(asset.asset_id)
    for values in downstream.values():
        values.sort(key=str.casefold)

    queue = deque((asset_id, (asset_id,)) for asset_id in changed)
    best_paths: dict[str, tuple[str, ...]] = {}
    has_cycles = False
    while queue:
        current, path = queue.popleft()
        for dependent in downstream[current]:
            if dependent in path:
                has_cycles = True
                continue
            candidate = (*path, dependent)
            existing = best_paths.get(dependent)
            if existing is None or (len(candidate), candidate) < (len(existing), existing):
                best_paths[dependent] = candidate
                queue.append((dependent, candidate))

    for item in changed:
        best_paths.pop(item, None)
    impacted = tuple(
        ImpactedAsset(
            asset_id=asset_id,
            asset_type=catalog[asset_id].asset_type,
            distance=len(path) - 1,
            path=path,
        )
        for asset_id, path in sorted(
            best_paths.items(),
            key=lambda item: (len(item[1]), item[0].casefold()),
        )
    )
    return ImpactAnalysis(
        changed_assets=changed,
        impacted_assets=impacted,
        has_cycles=has_cycles,
    )
