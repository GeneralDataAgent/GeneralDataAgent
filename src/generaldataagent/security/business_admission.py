"""Live, independently signed per-destination business action admission."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from generaldataagent.evaluation.stage_five_external import (
    QualifiedActionBundle,
    verify_action_qualification,
)
from generaldataagent.security.admission import load_release_keys


class FileActionAdmission:
    def __init__(
        self,
        *,
        bundle_path: Path,
        trust_path: Path,
        kind: str,
        revision: str,
        configuration_hash: str,
        environment: Literal["staging", "production"],
        destination: str,
        owner: str,
    ):
        self.bundle_path, self.trust_path = bundle_path, trust_path
        self.kind, self.revision, self.configuration_hash = kind, revision, configuration_hash
        self.environment, self.destination, self.owner = environment, destination, owner

    def allowed(self, *, now: datetime | None = None) -> bool:
        try:
            raw = self.bundle_path.read_bytes()
            if len(raw) > 1048576:
                return False
            bundle = QualifiedActionBundle.model_validate_json(raw)
            if (
                bundle.evidence.destination != self.destination
                or bundle.evidence.owner != self.owner
            ):
                return False
            return not verify_action_qualification(
                bundle,
                kind=self.kind,
                revision=self.revision,
                configuration_hash=self.configuration_hash,
                environment=self.environment,
                trusted_keys=load_release_keys(self.trust_path),
                now=now or datetime.now(UTC),
            )
        except Exception:
            return False
