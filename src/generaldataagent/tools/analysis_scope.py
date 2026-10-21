"""Resolve fixed-analysis inputs before reading any observations."""

import difflib
import sqlite3
from contextlib import closing

from generaldataagent.tools.sql_semantics import ColumnScopeError


class AnalysisColumnError(ColumnScopeError):
    def __init__(self, candidates):
        super().__init__(candidates)
        self.args = ("unknown analysis column; check scoped physical fields",)


def validate_analysis_columns(database, table, columns, *, allowed_relations):
    if table not in allowed_relations:
        raise ValueError("analysis relation is outside task scope")
    quoted = '"' + table.replace('"', '""') + '"'
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        physical = {row[1] for row in db.execute("PRAGMA table_info(" + quoted + ")")}
    missing = sorted(set(columns) - physical)
    if missing:
        # Suggestions are spellings from this exact authorized table only. They
        # do not establish semantic equivalence and are never selected for callers.
        candidates = []
        for column in missing:
            matches = difflib.get_close_matches(column, sorted(physical), n=5, cutoff=0.35)
            candidates.extend(name for name in matches if name not in candidates)
        raise AnalysisColumnError(candidates[:5])
