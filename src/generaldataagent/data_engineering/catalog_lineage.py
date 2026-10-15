"""Pinned dbt manifest ingestion into the unified lineage contract."""

import hashlib
import json
from pathlib import Path
from typing import Any

from generaldataagent.contracts.data_engineering import LineageAsset, LineageSnapshot

_SECTION_TYPES = {
    "sources": {"source": "source"},
    "nodes": {"model": "model", "seed": "table", "snapshot": "table"},
    "metrics": {"metric": "metric"},
    "semantic_models": {"semantic_model": "model"},
    "exposures": {"exposure": "dashboard"},
}
_IGNORED_DEPENDENCY_PREFIXES = ("test.", "analysis.", "operation.")


class DbtManifestError(ValueError):
    pass


class DbtManifestLineageLoader:
    """Reads one pinned manifest with a hard size limit and no external access."""

    def __init__(self, manifest_path: Path, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        self.manifest_path = manifest_path.resolve(strict=True)
        if not self.manifest_path.is_file():
            raise ValueError("manifest_path must be a file")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def load(self) -> LineageSnapshot:
        size = self.manifest_path.stat().st_size
        if size > self.max_bytes:
            raise DbtManifestError(f"dbt manifest exceeds the {self.max_bytes} byte limit")
        content = self.manifest_path.read_bytes()
        if len(content) > self.max_bytes:
            raise DbtManifestError(f"dbt manifest exceeds the {self.max_bytes} byte limit")
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DbtManifestError("dbt manifest is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise DbtManifestError("dbt manifest root must be an object")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise DbtManifestError("dbt manifest metadata is required")
        schema_version = metadata.get("dbt_schema_version")
        dbt_version = metadata.get("dbt_version")
        if not isinstance(schema_version, str) or not schema_version:
            raise DbtManifestError("dbt_schema_version is required")
        if not isinstance(dbt_version, str) or not dbt_version:
            raise DbtManifestError("dbt_version is required")

        raw_assets = self._collect_assets(payload)
        known_ids = set(raw_assets)
        assets: list[LineageAsset] = []
        unresolved: set[str] = set()
        for asset_id in sorted(raw_assets, key=str.casefold):
            asset_type, dependencies = raw_assets[asset_id]
            recognized_dependencies: list[str] = []
            for dependency in dependencies:
                if dependency in known_ids:
                    recognized_dependencies.append(dependency)
                elif not dependency.startswith(_IGNORED_DEPENDENCY_PREFIXES):
                    unresolved.add(dependency)
                    recognized_dependencies.append(dependency)
            assets.append(
                LineageAsset(
                    asset_id=asset_id,
                    asset_type=asset_type,
                    depends_on=tuple(dict.fromkeys(recognized_dependencies)),
                )
            )
        unresolved_dependencies = tuple(sorted(unresolved, key=str.casefold))
        return LineageSnapshot(
            manifest_hash=f"sha256:{hashlib.sha256(content).hexdigest()}",
            dbt_schema_version=schema_version,
            dbt_version=dbt_version,
            assets=tuple(assets),
            unresolved_dependencies=unresolved_dependencies,
            complete=not unresolved_dependencies,
        )

    @staticmethod
    def _collect_assets(payload: dict[str, Any]) -> dict[str, tuple[str, tuple[str, ...]]]:
        assets: dict[str, tuple[str, tuple[str, ...]]] = {}
        for section, type_mapping in _SECTION_TYPES.items():
            entries = payload.get(section, {})
            if entries is None:
                continue
            if not isinstance(entries, dict):
                raise DbtManifestError(f"dbt manifest section must be an object: {section}")
            for fallback_id, entry in entries.items():
                if not isinstance(entry, dict):
                    raise DbtManifestError(f"dbt manifest asset must be an object: {fallback_id}")
                resource_type = entry.get("resource_type")
                asset_type = type_mapping.get(resource_type)
                if asset_type is None:
                    continue
                asset_id = entry.get("unique_id", fallback_id)
                if not isinstance(asset_id, str) or not asset_id:
                    raise DbtManifestError("dbt manifest asset unique_id must be a string")
                if asset_id in assets:
                    raise DbtManifestError(f"duplicate dbt lineage asset: {asset_id}")
                depends_on = entry.get("depends_on", {})
                if not isinstance(depends_on, dict):
                    raise DbtManifestError(f"depends_on must be an object: {asset_id}")
                nodes = depends_on.get("nodes", [])
                if not isinstance(nodes, list) or any(not isinstance(item, str) for item in nodes):
                    raise DbtManifestError(f"depends_on.nodes must be strings: {asset_id}")
                assets[asset_id] = (asset_type, tuple(nodes))
        if not assets:
            raise DbtManifestError("dbt manifest contains no supported lineage assets")
        return assets
