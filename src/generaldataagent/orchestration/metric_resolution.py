"""Read-only resolution of a question against the certified metric catalog.

Resolution precedes query construction. It authorizes the catalog read by the actor's data
domains, returns candidates or an explicit ambiguity, and never creates a task, compiles a
query, writes an artifact, charges a budget, or invokes a model. Nothing here logs or persists
the question text.
"""

from generaldataagent.catalog import InMemoryMetricCatalog
from generaldataagent.contracts import (
    ActorContext,
    CertifiedMetricResolution,
    CertifiedMetricResolutionRequest,
    NaturalLanguageMetricQuery,
)
from generaldataagent.orchestration.questions import resolve_question


class CertifiedMetricResolver:
    """Resolve a question only against metrics the actor's data domains authorize."""

    def __init__(self, catalog: InMemoryMetricCatalog) -> None:
        if not isinstance(catalog, InMemoryMetricCatalog):
            raise TypeError("certified metric resolution requires a certified catalog")
        self.catalog = catalog

    def resolve(
        self, request: CertifiedMetricResolutionRequest, actor: ActorContext
    ) -> CertifiedMetricResolution:
        resolution = resolve_question(
            self.catalog,
            NaturalLanguageMetricQuery(
                question=request.question,
                metric_version=request.metric_version,
            ),
            actor,
        )
        return CertifiedMetricResolution(
            status=resolution.status,
            candidates=resolution.candidates,
            clarification_question=resolution.clarification_question,
        )
