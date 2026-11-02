"""SQL-output adapter for the benchmark-independent candidate selection runtime."""

from generaldataagent.evaluation.repair_budget import RepairBudget
from generaldataagent.orchestration.sql_search import (
    STRATEGIES,
    SearchBudgetExceeded,
    SearchModelBudget,
    select_sql_candidates,
)
from generaldataagent.tools import SQLiteReadOnlyExecutor
from generaldataagent.tools.sql_guard import SqlGuard
from generaldataagent.tools.sql_semantics import validate_column_scope


def run_sql_search(agent, task, database):
    from generaldataagent.catalog.semantic_context import semantic_context
    from generaldataagent.evaluation.stage_six import BoundedSQLAgent, database_context

    meter = SearchModelBudget(
        agent.model,
        max_calls=agent.max_steps,
        max_seconds=agent.max_seconds,
        max_tokens=agent.max_tokens,
        max_cost_usd=agent.max_cost_usd,
    )
    context, relations = database_context(database, public_text=task.question)
    schema = {
        "database": context,
        "semantic_context": semantic_context(
            database,
            allowed_relations=relations,
            max_chars=2000,
            public_text=task.question + "\n" + task.instructions,
        ),
    }
    branch_steps = (agent.max_steps - 3) // 3
    branches, trace = [], []
    repairs = RepairBudget(max_repairs=agent.max_repairs, max_identical=agent.max_identical_repairs)
    failure = None
    selection = None
    for index, strategy in enumerate(STRATEGIES):
        try:
            meter.require()
        except SearchBudgetExceeded as exc:
            failure = str(exc)
            break
        branch = BoundedSQLAgent(
            meter,
            max_steps=branch_steps,
            max_seconds=meter.remaining_seconds,
            max_tokens=None,  # The shared meter enforces the whole-task token ceiling.
            max_cost_usd=agent.max_cost_usd,
            max_repairs=agent.max_repairs,
            max_identical_repairs=agent.max_identical_repairs,
            repair_budget=repairs,
            tool_profile=agent.tool_profile,
            generation_strategy=strategy,
        ).run(task, database)
        branches.append(branch)
        trace.extend({**event, "candidate": index} for event in branch["trace"])
        if branch["failure"] == "model_error":
            failure = meter.last_refusal or "model_error"
            break
        # Budget refusals raised by the shared meter are caught by the existing
        # branch loop as model_error. The meter's state retains the actual cause.
        try:
            meter.require()
        except SearchBudgetExceeded as exc:
            failure = str(exc)
            break

    guard = SqlGuard(dialect="sqlite")

    def execute(sql):
        meter.require()
        guard.inspect(sql, allowed_relations=relations)
        validate_column_scope(database, sql, allowed_relations=relations)
        return SQLiteReadOnlyExecutor(
            database,
            max_rows=10000,
            max_cells=10000,
            timeout_seconds=min(5, meter.remaining_seconds),
        ).execute(sql)

    if failure is None:
        try:
            selection = select_sql_candidates(
                question=task.question,
                instructions=task.instructions,
                schema=schema,
                candidates=[b["final_sql"] if b["failure"] is None else None for b in branches],
                execute=execute,
                model=meter,
            )
        except SearchBudgetExceeded as exc:
            failure = str(exc)
        except (ValueError, TypeError, KeyError):
            failure = "sql_selection_invalid"
        except Exception as exc:
            trace.append({"status": "error", "error": type(exc).__name__, "phase": "sql_selection"})
            failure = "model_error"
    chosen = selection["selected"] if selection else None
    if chosen is None and failure is None:
        failure = "sql_candidates_exhausted"
    result = dict(branches[chosen] if chosen is not None else branches[0] if branches else {})
    result.update(
        answers={},
        final_sql=branches[chosen]["final_sql"] if chosen is not None and failure is None else None,
        failure=failure,
        trace=trace,
        experience_used=[],
        repair_budget=repairs.report(),
        sql_search={
            "strategy": "diverse-tournament-v1",
            "candidate_count": len(branches),
            "branch_step_ceiling": branch_steps,
            "reserved_selection_calls": 3,
            "candidates": [
                {"final_sql": b["final_sql"], "failure": b["failure"]} for b in branches
            ],
            "selection": selection,
        },
        **meter.telemetry(),
    )
    return result
