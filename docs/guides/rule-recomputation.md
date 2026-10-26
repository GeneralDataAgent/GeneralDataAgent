# Rule workflow recomputation

`RuleWorkflowRuntime.resume()` requires the original request, source hashes and
workflow version. A changed source still raises `rule_workflow_binding_changed`.
To recalculate after a source update, explicitly create a new task:

```python
child_thread, state = runtime.recompute(parent_thread, request, actor)
```

The parent must be completed or have a failed verification outcome. Interrupted
or exception-stopped parents must first be resumed under their original binding.
The request, workflow version, user, tenant, project and domain set must match the
parent. The new thread cannot reuse any registered task ID. Its checkpoint stores
`parent_thread_id` and `reused_stages`; parent checkpoints and artifacts are left
intact. This is a new execution, not an update to the previous claim.

Trusted application wiring can declare `RuleWorkflowServices.context_sources`.
When context is first computed, the wrapper fingerprints the complete request,
workflow version and those source hashes. Only `monthly_context` can be reused,
and only if the stored declaration and fingerprint match the current inputs.
Missing or unknown dependencies, legacy checkpoints without a fingerprint, or a
changed declaration cause full recomputation. Service code must declare all
context dependencies and change `workflow_version` when its semantics change.

The Annex services declare documents and, when injected, payments as context
sources. An annex-only update can therefore reuse context. Rule matching,
computation, verification and delivery always run again. Verification rederives
context from the authoritative inputs even when context was reused. Every node
retains the source checks before and after execution; a source change during
execution fails closed.

For `GovernedRuleRunner`, issue an authorized `ToolRequest` with tool name
`rules.execute`, action `recompute`, a **new** `task_id`, and arguments:

```python
{
    "parent_thread_id": parent_thread,
    "request": request.model_dump(mode="json"),
}
```

The normal policy, actor, audit, artifact and composition checks apply. Product
compositions require a separately registered budget for the new task. The new
workflow evidence includes lineage and reused stages. Its execution receipt
references only the child's evidence and current source hashes. The action in
the receipt remains `rules.execute`; audit requests distinguish `recompute` from
`run` and `resume`.

Source hashes establish content identity, not source authenticity or production
freshness. This facility does not establish production acceptance or independent
business verification.
