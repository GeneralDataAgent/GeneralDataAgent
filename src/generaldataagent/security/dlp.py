"""Deterministic text egress checks; reject unsupported content rather than skip scanning."""

import json
import re
import unicodedata
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
        # Inspect the semantic JSON representation as well as the original text: keys and
        # values encoded with JSON escapes must not become invisible to the text rules.
        candidates = [text]
        if text.lstrip().startswith(("{", "[")):
            depth, quoted, escaped = 0, False, False
            for char in text:
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "{[":
                    depth += 1
                    if depth > 64:
                        raise DLPDeniedError("dlp_structure_limit")
                elif char in "}]":
                    depth -= 1
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                value = None  # Non-JSON text still receives the normal text checks.
            except (RecursionError, ValueError):
                raise DLPDeniedError("dlp_structure_limit") from None
            if value is not None:
                try:
                    candidates.append(json.dumps(value, ensure_ascii=False, allow_nan=False))
                except (RecursionError, ValueError):
                    raise DLPDeniedError("dlp_structure_limit") from None
        normalized = []
        for candidate in candidates:
            candidate = unicodedata.normalize("NFKC", candidate)
            normalized.append(
                "".join(char for char in candidate if unicodedata.category(char) != "Cf")
            )
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
                    "phone": r"(?<![A-Za-z0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?![A-Za-z0-9])",
                }
            )
        for code, pattern in patterns.items():
            if any(re.search(pattern, candidate) for candidate in normalized):
                raise DLPDeniedError(f"dlp_{code}")

    def check_export(self, content: bytes, classification: ArtifactClassification) -> None:
        if classification not in {ArtifactClassification.PUBLIC, ArtifactClassification.INTERNAL}:
            raise DLPDeniedError("dlp_classification_denied")
        self.check(content)
