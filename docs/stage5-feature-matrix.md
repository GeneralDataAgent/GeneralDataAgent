# Stage 5 original-plan implementation mapping

Updated: 2026-10-19. Branch: `feat/stage5-business-loop`.

Scope comes from the workspace roadmap's “阶段 5 业务闭环与规模化” and acceptance section
“阶段 5 验收 业务闭环”. The original candidates are **drafts/requests**. Repository implementation
and isolated verification are complete for that scope; no live/production acceptance is implied.

| Planned capability | Implemented behavior | Code / focused evidence |
|---|---|---|
| Dashboard draft | Fixed metric/evidence references and disabled-query panel drafts delivered to a GitHub Issues human-review queue | `business/github_review.py`; `test_stage5_github_reviews.py` |
| Ticket draft | Exact title/description/priority review request, readback and separately approved closure | Same review receiver; three kinds exercise the same lifecycle independently |
| Scheduling change PR | Approved base snapshot and one JSON integer-interval change on a new branch, draft PR, full readback, separate closure | `business/github.py`; `test_stage5_github.py` |
| Model registration request | Experiment/model-card reference review request with `deployment_allowed=false`; no model activation | `contracts/business.py`, `business/github_review.py`; review receiver cases |
| Enterprise IM notification | Exact Feishu sender/recipient/content binding, send/readback, reconciliation and separately confirmed recall | `business/feishu.py`; `test_stage5_feishu.py` |
| Periodic report | Fresh certified queries, immutable bounded report packages, frozen Feishu report IM publication and workload-bound polling | `business/schedules.py`, `report_refresh.py`, `worker.py`; report/worker/workload cases |
| Per-action lifecycle and controls | Exact independent approval, idempotency, expiry/cancel, resource revision, uncertain-write quarantine, separately approved compensation, owner/scope/switch/rate controls | `business/actions.py`; six-kind 42-case frozen baseline and receiver fault suites |
| Cost, latency, routing and cache | Admitted native routing/shared durable budgets/usage, scoped encrypted cache, held-out measurement verification | `llm/orchestration.py`, `llm/routed.py`, `business/optimization.py`; orchestration/optimization/routed-analysis cases |
| Feedback and evaluation governance | Registered immutable result references, authenticated feedback, two independent votes, immutable datasets and task/result holdout exclusion | `business/governance.py`, `production_feedback.py`; governance and signed feedback assembly cases |
| Agent/Skill/connector admission | Pinned definition/version/evidence/scope/expiry, quality/security review, use-time admission and permanent revocation | `business/extensions.py`, `governance.py`, agent/connector assembly; extension/governance cases |
| Review/control workflow | Scoped Chinese action console and separate authenticated reviewer console/approval authority; requester client cannot grant approvals | `/business`, separate authority `/review`; console/browser and approval-service cases |
| Deployment assembly | Trusted report factory, one-shot/continuous process, graceful stop, disabled Linux templates; conditional qualified action/feedback API ports with independent signed audit | `services/report_worker.py`, `business/production.py`, `production_feedback.py`; process/production cases |

Direct live BI publishing, executable dashboard queries, actual model-version registration/deployment,
GitHub workflow/YAML/cron editing, and cloud-document/file publishing are not performed by these
review drafts. They are separate optional extensions, not hidden claims made by the implemented
roadmap candidates. Source/card/evidence hashes in review drafts remain references requiring review.

The real-environment gates remain open: target repository/recipient and reviewer/identity enrollment,
independent audit/retention/backup operations, genuine signed release and per-receiver measurements,
real compensation trials, measured business gains and held-out provider billing/P95 comparisons.
The operator has chosen to keep actual integration closed while completing code/isolated verification.
No test fixture or local passing suite closes those gates. See [operator dependencies](stage5-remaining-work.md)
and [validation evidence](validation/stage5-business-loop.md).
