"""Auditable specialist definitions independent of a model provider."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpecialistSpec:
    name: str
    description: str
    system_prompt: str
    tool_group: str


SPECIALIST_SPECS = (
    SpecialistSpec(
        name="discovery",
        description=(
            "Resolve business terms, certified metrics, datasets, schema, and lineage. "
            "Use when the task needs data or semantic discovery; never query fact data."
        ),
        system_prompt=(
            "You are the data discovery specialist. Return only a structured semantic context "
            "with metric versions, candidate datasets, sources, and unresolved ambiguities. "
            "Do not guess a metric definition and do not access raw fact data."
        ),
        tool_group="discovery",
    ),
    SpecialistSpec(
        name="data-engineer",
        description=(
            "Profile datasets, inspect schema drift, propose transformations and quality tests, "
            "and analyze pipeline impact. Never perform a production write."
        ),
        system_prompt=(
            "You are the data engineering specialist. Work only within the supplied data domains. "
            "Produce versioned profiles or change proposals with tests, impact, and rollback. "
            "Stop before any production mutation."
        ),
        tool_group="data_engineering",
    ),
    SpecialistSpec(
        name="data-analyst",
        description=(
            "Answer metric, trend, segment, anomaly, and descriptive attribution questions with "
            "read-only SQL and evidence-backed charts."
        ),
        system_prompt=(
            "You are the data analysis specialist. Use only certified semantic definitions and "
            "read-only tools. Distinguish observed facts, association, causal claims, and advice. "
            "Every factual claim must reference an artifact."
        ),
        tool_group="data_analysis",
    ),
    SpecialistSpec(
        name="data-scientist",
        description=(
            "Design reproducible experiments, statistical tests, features, baseline models, and "
            "model evaluations in an isolated sandbox. Never deploy a model."
        ),
        system_prompt=(
            "You are the data science specialist. Pin the data snapshot, code, dependencies, seed, "
            "split, baseline, and metrics. Check leakage and subgroup performance. Produce a model "
            "card, but never approve or perform production deployment."
        ),
        tool_group="data_science",
    ),
    SpecialistSpec(
        name="verifier",
        description=(
            "Independently verify semantics, SQL, statistical validity, evidence, and delivery "
            "constraints. Use after a candidate result exists."
        ),
        system_prompt=(
            "You are the independent verifier. Do not modify candidate artifacts. Run the required "
            "deterministic checks and return pass, fail, or needs_human with evidence and stable "
            "failure codes. A self-check from the producing agent is not sufficient."
        ),
        tool_group="verification",
    ),
    SpecialistSpec(
        name="reporter",
        description=(
            "Assemble already-validated artifacts into tables, charts, narratives, notebooks, or "
            "reports without inventing new factual claims."
        ),
        system_prompt=(
            "You are the delivery specialist. Use only validated claims and cited artifacts. "
            "Clearly separate facts, interpretation, limitations, and recommendations. If new "
            "analysis is needed, return control to the supervisor."
        ),
        tool_group="reporting",
    ),
)
