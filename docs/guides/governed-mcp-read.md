# Governed read-only MCP

This adapter lets one reviewed MCP server contribute read-only tools without letting the remote
server decide risk, roles or permissions. It is **off by default**: `create_app()` installs no MCP
route, and the local adapter is rejected in production mode.

## Admission order

1. **Fixed deployment profile** (`McpServerProfile`): server ID, transport (`stdio` or loopback
   `http`), tool prefix `mcp.<server_id>`, server version, timeout, response-size limit and the
   environment variable that names the credential. An HTTP profile also pins an absolute endpoint;
   only `http://127.0.0.1:<port>/<path>` and `http://[::1]:<port>/<path>` are accepted, with no
   userinfo, query or fragment. The credential value is read at use time and never stored in a
   contract.
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
`ToolRequest`, and calls `ToolGateway.invoke`. The stdio session sends `notifications/initialized` after validating
the server version and before it reads the tool list. Before the remote call the service:

- re-checks the registry admission for the requesting actor;
- opens at most one session per server, bound to the requesting tenant/project/user;
- re-discovers the tool list on the connection that will make the read and fails closed on any
  schema, description or tool-list drift;
- validates arguments against the admitted schema subset (no remote evaluation);
- enforces the tool timeout, response-byte limit and DLP scan.

A successful read is written as an `mcp_read_result` artifact scoped to the creating actor. The
response is `McpReadEvidence` — server/version/schema/admission hashes, argument and response hashes
and the artifact reference. Raw content is not returned inline and is not copied into audit.

## HTTP transport (loopback)

`HttpMcpTransport` speaks streamable HTTP to one repository-selected loopback endpoint and implements
the same `start/request/notify/close` contract as the stdio transport. It is not wired into
`create_app()`; deployment code still chooses the transport factory, and the local adapter remains
rejected in production mode.

- Each JSON-RPC message is one `POST` with `Content-Type: application/json`,
  `Accept: application/json, text/event-stream` and `Connection: close`; the response must be exactly
  one of those two media types, optionally with `charset=utf-8`. Any other type, parameters,
  `Content-Encoding` other than identity, redirect status or non-MCP status fails closed.
- Bodies are bounded by `max_response_bytes` while streaming, both for `Content-Length`, chunked and
  read-until-close framing. Truncated bodies, invalid chunk framing, folded or oversized headers and
  malformed status lines raise deterministic adapter errors; nothing is decoded past the cap.
- `text/event-stream` responses are parsed as `event: message` frames. Server notifications without an
  id are skipped up to a fixed count; a non-message event, malformed frame or mismatched id fails the
  exchange.
- The transport opens one connection per request, serializes requests under one lock and keeps at most
  one request in flight. `McpSessionManager` still admits only one active session per server and
  refuses a second tenant or scope.
- `Mcp-Session-Id` returned by `initialize` is validated and replayed on later requests, together with
  `MCP-Protocol-Version`. A request timeout or cancellation closes the socket under the in-flight
  read; `close()` is idempotent.
- A read that fails before any response byte is received (connection refused, write failure, or a
  connection closed before the status line) is attempted once more, and only once. The retry applies
  to the idempotent `initialize`, `tools/list` and `tools/call` exchanges; every retry emits
  `mcp.transport.retried` and reserves one additional task tool call, so a retry can fail closed on
  budget. HTTP error statuses, authentication, redirects, protocol or framing errors, oversized
  bodies, DLP denial, timeouts and cancellation are never retried.
- The endpoint is validated again when the transport is built. A credential containing control
  characters is refused before a header is written. Environment proxy variables are never consulted
  because the client uses a direct socket, and redirects are never followed.

`http_transport_factory(profile, credential)` builds this transport from an HTTP profile; it refuses a
stdio profile. Because the admission digest covers the whole profile, choosing a transport or
changing an endpoint requires a fresh local admission and capability enrollment. Real servers, TLS
termination and credential rotation remain outside this slice.

## Deterministic failures

| Condition | Adapter error | HTTP |
| --- | --- | --- |
| child process cannot start, or loopback connection refused | `McpServerUnavailable` | 502 |
| redirect returned by the endpoint | `McpServerUnavailable` (`mcp_redirect_refused`) | 502 |
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
| malformed/mismatched JSON-RPC, media type, header or chunk framing | `McpProtocolError` | 502 |

Timeout and cancellation close the session before the error leaves the adapter. Application shutdown
closes the active session. The only automatic retry is the single no-response-byte retry described
above; it applies only to idempotent reads and is audited and budgeted. A future broader retry policy
must still be limited to tools admitted as idempotent.

## Local fixtures

`tests/mcp_fixture_server.py` is a repository-owned newline-delimited JSON-RPC server with synthetic
data. Tests cover discovery, admission, an actual read through the API, schema drift, malicious
descriptions, name conflicts, auth/version failures, oversized and DLP-denied returns, argument
rejection, cross-tenant reuse, timeout, cancellation and shutdown.

`tests/mcp_http_fixture_server.py` is a synthetic `ThreadingHTTPServer` bound to `127.0.0.1` with
modes for SSE, redirects, wrong media types, compression, oversized `Content-Length` and chunked
bodies, truncated bodies, slow responses, auth failures, session headers, a connection closed before
any response byte (once or always) and malformed framing. Tests also assert one in-flight request per
session, cancellation closing the socket, the single retry on a no-response-byte failure, no retry
after a response byte or HTTP status, retry budget charging, and that proxy environment variables are
ignored. Both are protocol fixtures, not connections to any enterprise system.

## Boundaries

- No write, mutation, shell or filesystem tool is admitted.
- The HTTP transport accepts only repository-chosen loopback endpoints; real server addresses,
  TLS, credential rotation and independent review are still external gates.
- The local adapter is not qualified for production; `create_app(production_mode=True, mcp_read=...)`
  raises `local_mcp_read_not_qualified_for_production`.
- Session reuse is intentionally strict: while one actor scope holds the active session, another
  tenant is refused rather than given a shared remote session.
- `independent_review` remains `not_run` and `production_accepted` remains `false`.
