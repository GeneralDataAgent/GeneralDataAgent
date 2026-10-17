"""Deterministic text egress checks; reject unsupported content rather than skip scanning."""

import re
from dataclasses import dataclass

from generaldataagent.contracts import ArtifactClassification


class DLPDeniedError(PermissionError):
    pass


@dataclass(frozen=True)
class TextDLPScanner:
    """Conservative text pilot; detection reasons never include matched sensitive values."""

    block_personal_data: bool = True
    max_bytes: int = 10 * 1024 * 1024

    def check(self, content: bytes) -> None:
        if len(content) > self.max_bytes:
            raise DLPDeniedError("dlp_size_limit")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DLPDeniedError("dlp_unsupported_encoding") from exc
        patterns = {
            "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            "credential": (
                r"(?i)(?:api[_-]?key|password|secret|access[_-]?token)"
                r"""["']?\s*[:=]\s*["']?[^\s"',}]{8,}"""
            ),
            "bearer": r"(?i)\bBearer\s+[A-Za-z0-9_.~+/-]{16,}",
        }
        if self.block_personal_data:
            patterns.update(
                {
                    "email": r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
                    "phone": r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)",
                }
            )
        for code, pattern in patterns.items():
            if re.search(pattern, text):
                raise DLPDeniedError(f"dlp_{code}")

    def check_export(self, content: bytes, classification: ArtifactClassification) -> None:
        if classification not in {ArtifactClassification.PUBLIC, ArtifactClassification.INTERNAL}:
            raise DLPDeniedError("dlp_classification_denied")
        self.check(content)
