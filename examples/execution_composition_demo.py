"""Show the explicit research/product composition split without an evaluation run.

Both compositions own the same four ports — actor, artifacts, audit, budgets.
Only the product one requires the ports that make a refusal possible. This demo
builds each in a temporary directory and prints what was admitted, then shows
that dropping one control is refused at construction rather than at run time.
"""

import base64
import hashlib
import json
import tempfile
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.contracts import ActorContext
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.verified_audit import (
    AuditReceipt,
    VerifiedAuditSink,
    receipt_statement,
)
from generaldataagent.orchestration.composition import CompositionError, ExecutionComposition
from generaldataagent.policy.budgets import TaskBudgetLedger

ACTOR = ActorContext(
    user_id="demo-analyst",
    tenant_id="demo-tenant",
    project_id="demo-project",
    data_domains=("synthetic",),
)


class DemoSigningAudit:
    """Local stand-in for the independent audit service: it signs every append."""

    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        self.now = datetime.now(UTC)
        self.sequence = 0

    def __call__(self, content: bytes) -> AuditReceipt:
        self.sequence += 1
        receipt = AuditReceipt(
            event_id=json.loads(content)["event_id"],
            event_sha256=hashlib.sha256(content).hexdigest(),
            sequence=self.sequence,
            recorded_at=self.now,
            signer_id="demo-audit",
            signature="",
        )
        signature = base64.b64encode(self.private.sign(receipt_statement(receipt))).decode()
        return receipt.model_copy(update={"signature": signature})

    def sink(self):
        return VerifiedAuditSink(
            transport=self,
            trusted_keys={"demo-audit": self.private.public_key()},
            pseudonym_key=b"demo-pseudonym-key-not-for-production",
            clock=lambda: self.now,
        )


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        from pathlib import Path

        root = Path(folder)
        research = ExecutionComposition(
            mode="research",
            actor=ACTOR,
            artifacts=LocalArtifactStore(root / "research-artifacts"),
            audit=JsonlAuditSink(root / "research-audit.jsonl"),
        )
        print(f"research: mode={research.mode} production_ready={research.production_ready}")
        research_ports = f"{type(research.artifacts).__name__}, {type(research.audit).__name__}"
        print(f"research ports: {research_ports}")

        audit = DemoSigningAudit()
        product = ExecutionComposition(
            mode="product",
            actor=ACTOR,
            artifacts=GovernedArtifactStore(
                root / "product-artifacts",
                index_path=root / "product-index.sqlite",
                actor=ACTOR,
            ),
            audit=audit.sink(),
            budgets=TaskBudgetLedger(root / "product-budgets.sqlite"),
        )
        print(f"product: mode={product.mode} production_ready={product.production_ready}")
        print(f"product ports: {type(product.artifacts).__name__}, {type(product.audit).__name__}")
        print(f"product budgets: {type(product.budgets).__name__}")

        for label, kwargs in (
            ("governed artifacts", {"artifacts": LocalArtifactStore(root / "plain")}),
            ("verified audit", {"audit": JsonlAuditSink(root / "plain.jsonl")}),
            ("task budgets", {"budgets": None}),
        ):
            values = (
                dict(
                    mode="product",
                    actor=ACTOR,
                    artifacts=product.artifacts,
                    audit=product.audit,
                    budgets=product.budgets,
                )
                | kwargs
            )
            try:
                ExecutionComposition(**values)
            except CompositionError as exc:
                print(f"product without {label}: refused with {exc.code}")


if __name__ == "__main__":
    main()
