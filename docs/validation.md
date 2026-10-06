# Initial validation — 2026-10-06

Locally verified on Windows, Python 3.12.14, MCP SDK 1.30.0:

- 65 pytest cases passed, using a stateful HTTP API double.
- Actual MCP stdio subprocess startup, initialize, tool discovery and resource read passed.
- Streamable HTTP initialize, bearer rejection, Host and Origin checks passed in ASGI tests.
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

Not yet verified: real API writes, public DNS propagation, production gateway
integration, Docker image execution (Docker unavailable locally). GitHub CI additionally runs
Linux and Windows on Python 3.11, 3.12 and 3.13; consult the actual workflow result for its status.

No live domain, DNS, forward or invoice changes were made for these tests.
