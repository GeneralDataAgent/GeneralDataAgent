"""CSV policy v2: retain leading-zero identifiers and integers beyond exact double range.

Other numeric columns remain REAL to retain the baseline's SQLite arithmetic semantics.
"""

import csv
import math
import re
import sqlite3
from pathlib import Path

_NUMBER = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_INTEGER = re.compile(r"^[+-]?[0-9]+$")
_GROUPED = re.compile(r"^[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?$")
_LEADING_ZERO = re.compile(r"^[+-]?0[0-9]+$")


def classify_column(values: list[str]) -> str:
    present = [value.strip() for value in values if value.strip()]
    for value in present:
        if _LEADING_ZERO.fullmatch(value):
            return "TEXT"
        normalized = value.replace(",", "") if _GROUPED.fullmatch(value) else value
        if not _NUMBER.fullmatch(normalized):
            return "TEXT"
        if _INTEGER.fullmatch(normalized) and abs(int(normalized)) > 2**53:
            return "TEXT"
        if not math.isfinite(float(normalized)):
            return "TEXT"
    return "REAL"


def build_identifier_preserving_database(files: dict[str, Path], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError("database already exists; use a fresh snapshot path")

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    with sqlite3.connect(target) as db:
        for table, path in files.items():
            with path.open(encoding="utf-8-sig", newline="") as stream:
                columns, *rows = list(csv.reader(stream))
            columns = [value or f"unnamed_{i}" for i, value in enumerate(columns)]
            if len({value.casefold() for value in columns}) != len(columns):
                raise ValueError("duplicate CSV columns")
            if any(len(row) != len(columns) for row in rows):
                raise ValueError("ragged CSV")
            types = [classify_column([row[i] for row in rows]) for i in range(len(columns))]
            definition = ",".join(
                quote(name) + " " + kind for name, kind in zip(columns, types, strict=True)
            )
            db.execute(f"CREATE TABLE {quote(table)} ({definition})")
            values = [
                tuple(
                    None
                    if not value.strip()
                    else float(value.replace(",", ""))
                    if types[i] == "REAL"
                    else value
                    for i, value in enumerate(row)
                )
                for row in rows
            ]
            placeholders = ",".join("?" for _ in columns)
            db.executemany(f"INSERT INTO {quote(table)} VALUES ({placeholders})", values)
