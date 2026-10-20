"""Qualify declared key context against the pinned BIRD database snapshots, without models."""

import argparse
import json
import sqlite3
from pathlib import Path

from generaldataagent.evaluation.stage_six import digest, verify_sources
from generaldataagent.evaluation.stage_six_sql_precision import schema_relationships


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "data/stage6"
    verify_sources(source)
    manifest_path = source / "bird/database-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    rows = []
    for item in manifest["databases"]:
        name = Path(item["path"]).parent.name
        path = source / "bird/databases" / name / (name + ".sqlite")
        before = digest(path)
        if before != item["sha256"]:
            raise ValueError("official snapshot fingerprint changed")
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        context = schema_relationships(path, allowed_relations=names)
        if digest(path) != before:
            raise ValueError("schema extraction changed the database")
        rows.append(
            {
                "database": name,
                "sha256": before,
                "tables": len(names),
                "tables_with_primary_key": len(context["primary_keys"]),
                "foreign_key_column_mappings": len(context["foreign_keys"]),
                "context": context,
            }
        )
    if len(rows) != 11 or len({r["database"] for r in rows}) != 11:
        raise ValueError("incomplete pinned BIRD cohort")
    result = {
        "kind": "declared_sql_schema_qualification",
        "databases": len(rows),
        "foreign_key_column_mappings": sum(r["foreign_key_column_mappings"] for r in rows),
        "all_database_hashes_unchanged": True,
        "source_manifest_hash": digest(source / "sources.json"),
        "database_manifest_hash": digest(manifest_path),
        "results": rows,
        "agent_accuracy": None,
        "production_accepted": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "results"}))


if __name__ == "__main__":
    main()
