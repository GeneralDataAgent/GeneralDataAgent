"""Compatibility imports; use task_semantics or evaluation.action_protocol for new code."""

from generaldataagent.orchestration.task_semantics import accept_contract

_WIRE_NAMES = (
    "PROTOCOL_ACTION_KEYS",
    "ActionSchemaError",
    "normalize_protocol_action",
    "validate_action",
)
__all__ = ["accept_contract", *_WIRE_NAMES]


def __getattr__(name):
    if name in _WIRE_NAMES:
        from generaldataagent.evaluation import action_protocol

        return getattr(action_protocol, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
