# Research findings and design principles

This document separates tutorial observations, target architecture, and engineering decisions. Framework features do not establish application capabilities or production guarantees. It describes the design baseline researched on 2026-10-13; implementation and acceptance status live in the [external progress records](../../../docs/validation/README.md).

## Reviewed material

- [Chapter 1: From Agent Framework to Agent Harness](https://datawhalechina.github.io/deepagents-in-action/chapters/ch01-agent-harness/)
- [Chapter 3: Virtual filesystem](https://datawhalechina.github.io/deepagents-in-action/chapters/ch03-virtual-filesystem/)
- [Chapter 5: Subagents and context isolation](https://datawhalechina.github.io/deepagents-in-action/chapters/ch05-subagents/)
- [Chapter 6: Asynchronous subagents](https://datawhalechina.github.io/deepagents-in-action/chapters/ch06-async-subagents/)
- [Chapter 9: Human in the Loop](https://datawhalechina.github.io/deepagents-in-action/chapters/ch09-human-in-the-loop/)
- The user-provided seven-layer Data Agent diagram.

The tutorials describe Deep Agents v0.7 semantics. Pin actual dependency versions and verify APIs with minimal examples before relying on them.

## Tutorial observations

1. The stack separates Runtime, Framework, and Harness. LangGraph supplies durable execution, streaming, state, and interruption recovery. LangChain supplies model, tool, agent-loop, and middleware abstractions. Deep Agents assembles filesystem access, context management, subagents, Skills, and memory above them.
2. Context engineering keeps large inputs and intermediate results in a retrievable virtual filesystem instead of copying everything into the main conversation.
3. Context quarantine isolates subtask conversations. The parent receives concise results; agents sharing a backend can exchange intermediate artifacts through files.
4. Specialist system prompts and Skills are not automatically inherited. Explicit tools or permissions can replace parent configuration rather than merge it. Review each role's effective configuration.
5. Use LangGraph for complex, fixed, branching, or tightly controlled processes. Specialists can be declared as dictionaries or integrated as `CompiledSubAgent` graphs.
6. HITL recovery requires a checkpointer and the same thread ID. External writes, production changes, expensive computation, and publication belong behind applicable approvals.
7. `FilesystemBackend` and `LocalShellBackend` suit trusted local development, not an unisolated multi-user production service. Production code execution needs a separate sandbox.

## Seven business layers

| Layer | Question | Responsibility |
|---|---|---|
| Access | Which data and workspaces are accessible? | Connectors, identity propagation, isolation, capability declarations |
| Semantics and ontology | What do data and metrics mean? | Catalogs, metric definitions, lineage, labels, knowledge graphs, governance signals |
| Planning and orchestration | What runs, who owns it, and in what order? | Clarification, task DAGs, routing, budgets, retries, human checkpoints |
| Execution | How are operations actually performed? | SQL, Python, Spark, dbt, notebooks, statistics, model tools |
| Verification and reflection | Are results correct, reproducible, and explainable? | Quality, query review, statistical checks, provenance, replay |
| Governance and security | Which actions are permitted and auditable? | Identity, least privilege, redaction, sandboxing, approvals, audit, lifecycle |
| Delivery | How are results made useful? | Tables, charts, reports, notebooks, APIs, notifications, approved follow-up actions |

These are capability boundaries, not a requirement for seven services or seven agents. Much of semantics, verification, and governance must be deterministic.

## Design principles

### Establish a controlled loop before expanding autonomy

Start with read-only sources. Production writes, external sends, model publication, and workflow triggers require explicit approval, idempotency, and a rollback design.

### Agents propose; the control plane enforces

Agents may plan, generate SQL, select methods, and explain results. They cannot grant permissions, skip validation, disable audit, or obtain secrets. Authorization executes outside the agent loop.

### Exchange artifacts through contracts

Use versioned schemas for plans, datasets, queries, analysis results, model runs, and claims. Store large objects in artifact storage; exchange URIs, summaries, checksums, and lineage rather than unrestricted prose.

### Make high-risk workflows deterministic

Exploration can use dynamic delegation. Production changes, training releases, backfills, and external actions use fixed LangGraph nodes, branches, and approvals that dynamic agents cannot bypass.

### Trace every conclusion

Bind conclusions to the question, semantic definition, data snapshot, query or code revision, execution result, validation, and generating model version. Conclusions without evidence remain hypotheses.

### Default to least privilege and isolation

Give each role only the tools and data domains it needs. Use short-lived credentials, isolated execution, and separate tenant, user, project, and task namespaces.

### Keep verification independent

The query generator does not approve its own query; the trainer does not decide production admission. Independent validators or verifier roles perform checks, with deterministic assertions controlling critical gates.

## Facts, design decisions, and open questions

Virtual filesystems, subagents, middleware, pluggable backends, and HITL are tutorial-described features. Supervisor coordination, fixed high-risk graphs, and artifact storage as the large-object bus are application design decisions. Organization-specific discovery and validation tasks are tracked separately in the [design validation backlog](../../../docs/progress/design-validation-backlog.md).
