"""Content-addressed local artifact storage for development."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from generaldataagent.contracts import ArtifactClassification, ArtifactRef

_SAFE_TYPE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$")


class LocalArtifactStore:
    """Stores artifacts under a task-scoped root and returns immutable references."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        *,
        task_id: UUID,
        artifact_type: str,
        content: bytes,
        created_by: str,
        classification: ArtifactClassification = ArtifactClassification.INTERNAL,
        lineage: tuple[UUID, ...] = (),
        extension: str = "bin",
    ) -> ArtifactRef:
        if not _SAFE_TYPE.fullmatch(artifact_type):
            raise ValueError("artifact_type contains unsafe characters")
        if not re.fullmatch(r"[a-zA-Z0-9]{1,12}", extension):
            raise ValueError("extension contains unsafe characters")

        artifact_id = uuid4()
        task_dir = (self.root / str(task_id)).resolve()
        if self.root not in task_dir.parents:
            raise ValueError("task path escapes the artifact root")
        task_dir.mkdir(parents=True, exist_ok=True)

        content_hash = hashlib.sha256(content).hexdigest()
        filename = f"{artifact_id}.{extension}"
        path = task_dir / filename
        path.write_bytes(content)

        return ArtifactRef(
            artifact_id=artifact_id,
            task_id=task_id,
            uri=f"artifact://local/{task_id}/{filename}",
            artifact_type=artifact_type,
            content_hash=f"sha256:{content_hash}",
            classification=classification,
            created_by=created_by,
            lineage=lineage,
            size_bytes=len(content),
        )

    def put_json(
        self,
        *,
        task_id: UUID,
        artifact_type: str,
        value: Any,
        created_by: str,
        classification: ArtifactClassification = ArtifactClassification.INTERNAL,
        lineage: tuple[UUID, ...] = (),
    ) -> ArtifactRef:
        content = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return self.put_bytes(
            task_id=task_id,
            artifact_type=artifact_type,
            content=content,
            created_by=created_by,
            classification=classification,
            lineage=lineage,
            extension="json",
        )

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        prefix = "artifact://local/"
        if not ref.uri.startswith(prefix):
            raise ValueError("artifact is not stored by this backend")
        relative = ref.uri.removeprefix(prefix)
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact path escapes the configured root")
        content = path.read_bytes()
        actual = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if actual != ref.content_hash:
            raise ValueError("artifact content hash mismatch")
        return content

    def discard(self, ref: ArtifactRef) -> bool:
        """Remove one artifact this store wrote, for a failed multi-artifact write.

        Artifact file names carry a fresh UUID and live under the declaring task,
        so a discard can only ever remove the reference it is given. A missing
        file is reported as ``False`` rather than raised: a rollback must be safe
        to call twice when one attempt was interrupted.
        """
        prefix = "artifact://local/"
        if not ref.uri.startswith(prefix):
            raise ValueError("artifact is not stored by this backend")
        path = (self.root / ref.uri.removeprefix(prefix)).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact path escapes the configured root")
        if path.parent != (self.root / str(ref.task_id)).resolve():
            raise ValueError("artifact does not belong to the declaring task")
        if not path.exists():
            return False
        path.unlink()
        return True
