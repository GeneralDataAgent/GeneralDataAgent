"""Compatibility exports for the neutral experience derivation service.

Input traces remain unauthenticated development evidence. Product capture and
benchmark consumers share the same pure template without depending on each other.
"""

from generaldataagent.contracts.experience import ExtractedExperience
from generaldataagent.experience.extraction import (
    extract_experiences,
    extract_rule_experience,
    rule_repair_context_hash,
)

__all__ = [
    "ExtractedExperience",
    "extract_experiences",
    "extract_rule_experience",
    "rule_repair_context_hash",
]
