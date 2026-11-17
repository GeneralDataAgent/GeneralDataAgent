# Governed read-only MCP

This adapter lets one reviewed MCP server contribute read-only tools without letting the remote
server decide risk, roles or permissions. It is **off by default**: `create_app()` installs no MCP
route, and the local adapter is rejected in production mode.

## Admission order

1. **Fixed deployment profile** (`McpServerProfile`): server ID, `stdio` transport, tool prefix
   `mcp.<server_id>`, server version, timeout, response-size limit and the environment variable that
   names the credential. The credential value is read at use time and never stored in a contract.
2. **Discovery** (`discover_tools`) opens one bounded session and returns `McpDiscovery`: candidates
   with canonical input schemas and description hashes. Descriptions and remote annotations are
   untrusted text; they never assign risk or permission.
3. **Local admission** (`McpServerAdmission`) pins the profile, the exact schema hash and a local
   decision for each tool: action `read`, `idempotent=True`, risk R0–R3 and allowed roles. Remote
   tools that cannot be expressed as idempotent reads are not admitted in this slice.
4. **Capability registry**: the admission digest is enrolled as a `connector` capability for the
   tenant, project and data domains. The registry is checked again on every read.
5. **Gateway mapping**: each admitted tool is registered once as
   `mcp.<server_id>.<normalized_remote_name>`. The policy rule is built from the local admission, so
   an R2/R3 read requires exact approval even when the remote tool describes itself as safe.

## Read path

`POST /v1/analysis/mcp/{remote_tool}/read` (development only) authenticates the session, builds the
`ToolRequest`, and calls `ToolGateway.invoke`. Before the remote call the service:

- re-checks the registry admission for the requesting actor;
- re-discovers the tool list and fails closed on any schema, description or tool-list drift;
- opens at most one session per server, bound to the requesting tenant/project/user;
- validates arguments against the admitted schema subset (no remote evaluation);
- enforces the tool timeout, response-byte limit and DLP scan.

A successful read is written as an `mcp_read_result` artifact scoped to the creating actor. The
response is `McpReadEvidence` — server/version/schema/admission hashes, argument and response hashes
and the artifact reference. Raw content is not returned inline and is not copied into audit.

## Deterministic failures

| Condition | Adapter error | HTTP |
| --- | --- | --- |
| child process cannot start | `McpServerUnavailable` | 502 |
| credential missing or rejected | `McpAuthFailed` | 503 |
| initialize version differs from profile | `McpVersionMismatch` | 409 |
| tool list/schema/description changed | `McpSchemaDrift` | 409 |
| two remote names map to one local name | `McpToolConflict` | 409 |
| tool not in the admission | `McpToolNotAdmitted` | 404 |
| arguments violate the admitted schema | `McpRequestRejected` | 422 |
| response exceeds the size limit | `McpResponseTooLarge` | 413 |
| response fails DLP | `McpResponseDenied` | 422 |
| timeout | `McpTimeout` | 504 |
| cancel | `CancelledError` after closing the session | — |
| another tenant tries to reuse the active session | `McpSessionScopeError` | 409 |
| malformed/mismatched JSON-RPC | `McpProtocolError` | 502 |

Timeout and cancellation close the session before the error leaves the adapter. Application shutdown
closes the active session. This slice performs **no automatic retries**; a future retry may apply only
to a tool admitted as idempotent.

## Local fixture

`tests/mcp_fixture_server.py` is a repository-owned newline-delimited JSON-RPC server with synthetic
data. Tests cover discovery, admission, an actual read through the API, schema drift, malicious
descriptions, name conflicts, auth/version failures, oversized and DLP-denied returns, argument
rejection, cross-tenant reuse, timeout, cancellation and shutdown. It is a protocol fixture, not a
connection to any enterprise system.

## Boundaries

- No write, mutation, shell or filesystem tool is admitted.
- The local adapter is not qualified for production; `create_app(production_mode=True, mcp_read=...)`
  raises `local_mcp_read_not_qualified_for_production`.
- Session reuse is intentionally strict: while one actor scope holds the active session, another
  tenant is refused rather than given a shared remote session.
- `independent_review` remains `not_run` and `production_accepted` remains `false`.
