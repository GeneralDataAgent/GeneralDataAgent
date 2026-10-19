# Stage 5 remaining implementation and operator dependencies

Updated: 2026-10-19. Development branch: `feat/stage5-business-loop`.

## Implemented in this continuation

1. Metered narrative routing/cache is part of the certified analysis node reached by Supervisor
   tools. SQL/authorization remain fresh. Supervisor and specialist native model calls can now
   opt into shared durable budgets, per-call admission/routing, native output caps and provider
   usage audit; automatic unmetered summarization is excluded.
2. Periodic schedules can refresh certified data, generate immutable report packages and request
   exact separate approvals. Fixed and rolling windows, crash-safe frozen content, cancellation
   during generation and artifact checks are implemented. An explicit current-at-proposal revision
   policy supports missed/cancelled periods while preserving existing exact approvals on replay.
3. Feishu notification and frozen-report IM publication, receiver readback, durable hints, no-resend quarantine,
   read-only reconciliation and separately confirmed recall code are implemented. One explicitly
   authorized bot-to-self message and same-receipt replay were tested live. Recall is only locally tested.
4. Agent/Skill/connector admission is enforced in normal assembly and use paths. Changed definitions,
   unauthorized scope, expiration and revocation block use.
5. A durable background report worker now polls with fresh scoped sessions, expiry, cancellation,
   multi-process leases, restart recovery and sanitized status. A pinned service-JWT adapter now
   supplies revocable short-lived sessions constrained to the enrolled schedule and its exact report
   occurrences. It only proposes drafts.
6. Aggregate acceptance now verifies independently signed held-out optimization measurements and
   the existing Stage 4 release proof, rather than permanently returning incomplete.

7. A GitHub scheduling receiver now pins the repository/account IDs, base commit and exact before/after
   bytes for one non-secret JSON schedule manifest. Exact-approved branch/commit/draft-PR creation,
   full receiver readback, durable partial-write hints, read-only PR reconciliation and separately
   approved closure of the unchanged unmerged draft are implemented and isolated-tested. No real
   target was configured and no real GitHub PR was created.

## Remaining code/integration work

| Work | Present boundary | Input needed |
|---|---|---|
| Real Dashboard/ticket/registry/cloud-document or file publication receivers | Typed local drafts and lifecycle exist; Feishu notification/report IM and GitHub JSON schedule draft-PR adapters are implemented. The latter two have isolated validation only; Dashboard/ticket/registry/cloud-document or file adapters are still missing | Choose target systems and test resources; qualify each receiver independently |
| GitHub schedule target enrollment | One configured JSON object key can change an integer interval in an existing regular UTF-8 file, at most 8 KiB; no YAML/workflow/cron editor | Identify repository, numeric repository/account IDs, base branch, JSON file and interval key path. Keep credentials in the protected CLI store; review exact proposed bytes and repository automation before any write |
| Autonomous periodic service execution | Durable polling, mounted service-JWT validation, schedule-bound revocable session issuance and API/action constraints exist; no user token is stored or renewed | Enroll the actual issuer/audience/keys/service delegation and deploy with a process manager; each external action still needs explicit exact approval |
| Full orchestration-model routing and metering | Narration and opt-in native Supervisor/specialist routing, shared orchestration budget and usage audit are implemented | Enroll approved provider/model configurations and run the real held-out billing/latency benchmark before enabling; deployment routes within one native provider family |
| Production business API assembly | Pilot works and qualification checks exist; production factory still rejects local business/feedback state | Independently operated approval/audit/retention infrastructure and a qualified production receiver; do not remove this guard just to make deployment pass |
| End-user review/control UI | Scoped APIs and trusted operator review ports exist | Product review flow and authenticated external approval service; no public endpoint can grant an approval |

## Operator work before expanding the Feishu pilot

- Choose the long-term recipient/test group and add the bot if a group is used. Bot authentication
  already works; the current bot was not a member of any group during the read-only check.
- Every real send must have recipient, exact content and sender identity confirmed. The current
  consent covers the single completed test message only. Do not reuse it for reports/bulk sends.
- Real recall requires a new explicit high-risk confirmation and matching approval; it has not been
  authorized or exercised here. If the CLI cannot provide deleted-state evidence, leave it unresolved.
- Enroll real model/provider routes using genuine quality/security reviewers and operator-owned
  non-secret configuration fingerprints. Tests do not stand in for those reviewers.

## Required external evidence, not missing local code

Per-receiver independent trials, successful real compensation, complete audit/retention drills,
measured business improvement, held-out billing/P95 comparison, and independent Stage 4 release
admission remain open. The qualifiers now accept these inputs and verify source/configuration/
environment/dataset binding, finite validity, signed independent reviewers and threshold results.
The single Feishu connectivity sample cannot satisfy those production gates.
