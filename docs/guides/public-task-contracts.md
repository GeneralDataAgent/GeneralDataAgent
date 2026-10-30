# Public task contract acceptance

Applications can validate an explicit analysis proposal against its authoritative public question
and requirements without loading an evaluator or a model framework. This accepts a supplied
proposal; it does not generate one or execute analysis.

```python
from generaldataagent.orchestration.task_semantics import accept_contract

question = "What is the mean journey duration?"
instructions = "Round the result to two decimal places."
spec = accept_contract(
    {
        "question": question,
        "public_requirements": instructions,
        "answer": {"columns": ["mean_duration"], "shape": "scalar"},
    },
    question=question,
    instructions=instructions,
)
assert spec.answer.decimals == 2
```

The existing acceptance rules preserve both public texts exactly, check sourced typed requirements
and answer-field rules, reject declared conflicts, and reconcile explicit public rounding. A
proposal contradicting the two-decimal instruction is rejected. Supported public formats retain
their existing behavior; acceptance does not establish that a proposal captures every business
meaning or that any answer is correct.

The local CLI uses the same function:

```bash
uv run --frozen generaldataagent-task-contract \
  --request examples/public-task-contract.json \
  --output .artifacts/accepted-task.json
```

Create the output directory first, or omit `--output` to emit JSON to standard output. An existing
output is never overwritten. The request is finite UTF-8 JSON, at most 1,000,000 bytes, with exactly
three keys: `question` and `instructions` strings, and a `proposal` object conforming to
`AnalysisTaskSpec`. The example shows the complete envelope. An invalid request exits with status
2 before creating an output; schema diagnostics retain field paths and error types without input
values. Successful output is the accepted `AnalysisTaskSpec` JSON. The request is read only.

The module entry point is also available with
`python -m generaldataagent.orchestration.task_contract_cli`. Neither entry point requires a data
source, provider, benchmark labels, credentials or a production configuration. It grants no query,
artifact or release authority and produces no execution receipt or acceptance score.

## Evaluation transport and compatibility

Native/JSON function-envelope normalization, canonical wire validation, `ActionSchemaError` and
`PROTOCOL_ACTION_KEYS` now live in `evaluation.action_protocol`. The bounded research agent imports
that adapter and the public semantic acceptor separately. The historical
`orchestration.analysis_plan` path retains the original public names as compatibility aliases.
Its public acceptor is available immediately; wire names are resolved lazily when requested, so
importing the acceptor through the old path does not load evaluation. New application code should
import `task_semantics` directly.

The moved functions retain their behavior and the wire action vocabulary is unchanged. New runs
fingerprint both new modules as well as the compatibility shim; an implementation change therefore
requires fresh comparison arms. Historical reports and hashes are not rewritten. This separation
does not add an injected general dispatcher, a natural-language API or production admission.
