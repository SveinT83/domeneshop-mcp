# Initial validation — 2026-10-06

Locally verified on Windows, Python 3.12.14, MCP SDK 1.30.0:

- 76 pytest cases passed, including a stateful HTTP API double and identity reconciliation tests.
- Actual MCP stdio subprocess startup, initialize, tool discovery and resource read passed.
- Streamable HTTP initialize, bearer rejection, Host and Origin checks passed in ASGI tests.
- Separate stateless HTTP requests preserve the API client and pending plans through
  preview, plan lookup, apply and readback against the API double.
- MCP tool calls verified structured results and schema rejection.
- Preview-only behavior; exact create/update/delete readback; header-only create responses;
  stale plans; concurrent apply; partial failures; cancellation; write backup failure;
  no mutation retry; DNS restore; mail-record preservation; DDNS and domain scoping passed.
- Ruff lint and formatting passed; pip reported no broken requirements.
- Installable Python wheel built successfully.

Live read-only acceptance also passed through an actual MCP stdio subprocess on Linux. The test
covered domain listing and lookup, DNS listing and exact record lookup, forward listing, invoice
listing and exact invoice lookup, DNS audit, TXT plan preview, rejection by the disabled write gate,
and a final comparison proving the selected domain's DNS was unchanged. Credentials were injected
only into the temporary server process on the existing credential host, after checking the original
connection's user, tenant, execution and credential-management permissions. No credential values or
customer records are included in this repository.

The live API returned MX priority and SRV priority/weight/port as decimal strings. These response
fields are now normalized before validation/comparison; caller-supplied write inputs remain strict.
Regression tests cover audit, idempotent matching, copying and DS/CAA field distinctions.

Live Docker and LiteLLM 1.103.2 integration also passed. The container runs as UID 10001 with
a read-only root filesystem, persistent private state, a health check and automatic restart.
It has no published port and is reached by the gateway through their private Docker network.
An authenticated MCP client connected through the gateway's public HTTPS endpoint and verified
27 tools, domain/DNS reads, TXT preview and plan lookup, followed by an unchanged-DNS comparison.
The dedicated virtual key sees only this server's tools; model routes and an ungranted key were
rejected with HTTP 403. Missing upstream bearer authentication was rejected with HTTP 401.
Existing key permissions and server configurations were read back unchanged.

That end-to-end test exposed an HTTP lifecycle bug: the stateless MCP request lifespan closed the
shared API client after each request. Cleanup now belongs to the transport's process lifetime.
The HTTP regression test above covers the failure, and the corrected container passed the live test.

Not yet verified: real API writes or public DNS propagation. GitHub CI passed on Linux and Windows
with Python 3.11, 3.12 and 3.13 for the deployed runtime commit; consult the workflow for later changes.

Keycloak group reads and exact SSO-subject mapping were verified against the live realm.
The Senior team exposes all 27 tools. A temporary reader identity discovered nine tools,
read domains, and was denied a direct call to `apply_plan`; removal invalidated its key.
Gateway OAuth authorization with PKCE and refresh also passed. A synthetic identity lifecycle
test verified gateway deletion and rejection of the previously working key; no real human
account was deleted. The private issuer-pinned registry and unknown-404 safeguards are covered
by automated tests. Successful recurring timer runs were read back from systemd.

These results do not claim that a ChatGPT Work plugin has been installed or that every user's
Open WebUI connection has been authorized. Each client requires its own acceptance test.

No live domain, DNS, forward or invoice changes were made for these tests.
