"""Opt-in Stage 7 prompt and reproducible system fingerprints (no scoring labels)."""

from pathlib import Path

from generaldataagent.evaluation.stage_six import digest

STAGE7_PROMPT = """
Stage 7 requires a public task contract before computation. First return only
{"contract":{"entity":"requested entity","grain":["id"],"count_basis":"entities",
"denominator":null,"order_by":null,"order_direction":null,
"answer":{"columns":["requested column"],"shape":"rows","ties":"unspecified"}}}.
Omit question and public_requirements: the runtime binds both exact public texts automatically.
All other fields are optional. Do not invent unspecified requirements.
contract fields: entity (string), grain (list of column names), filters (list of strings),
time_range/formula/denominator/order_by (string or null), order_direction (asc/desc or null),
count_basis (entities/records/unspecified), conflicts (list of strings), answer (object).
answer fields: columns (list of requested names), shape (scalar/rows/set), unit (string or null),
decimals (integer or null), ties (all/single/unspecified), empty (allow/reject), nullable (boolean),
minimum/maximum (number or null). Public rounding requirements must be followed.
Use conflicts for conflicting public requirements; do not silently resolve them.
Use only supported schema fields. Never use reference answers, reference SQL or benchmark IDs.
The contract is a proposed interpretation, not proof of understanding. Check original text again.
For oldest by birthday use ascending birth date; compute age explicitly if age is requested.
Preserve all requested ties and complete sets. Inspect category/date formats and exact boundaries.
SQL actions must be read-only. final_sql is reserved for SQL-output tasks.
For answers, first compute full-data evidence, then return an empty answers object and bindings:
{"answers":{},"evidence_steps":[2],"bindings":{"name":[2,["rows",0,0]]}}.
For analysis bind a scalar result path, e.g. [2,["statistics","x","sample_std"]].
Every output key must bind a scalar from a successful observed result. Values are assembled by the
runtime; do not guess from samples. Declare decimals only when explicitly required in public text.
Rounding uses decimal half-even. Full precision is retained in the calculation evidence.
For complete ID sets use {"collect":{"sql":"SELECT DISTINCT id FROM table WHERE ..."}}.
This returns total_count, complete and items, bounded to 10000 items, and rejects LIMIT/OFFSET.
Bind [step,["items"]] and declare answer.shape="set"; the runtime checks completeness.
Sets larger than the collection limit fail explicitly; LIMIT previews cannot
stand in for complete answers. You have the same six actions and 24000-token total budget.
"""


def stage7_fingerprint(root: Path) -> dict:
    files = [
        "contracts/analysis.py",
        "orchestration/analysis_plan.py",
        "catalog/semantic_context.py",
        "tools/sql_semantics.py",
        "evaluation/stage_seven.py",
    ]
    return {name: digest(root / "src/generaldataagent" / name) for name in files}
