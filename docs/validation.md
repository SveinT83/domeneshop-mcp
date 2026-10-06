# Initial validation — 2026-10-06

Locally verified on Windows, Python 3.12.14, MCP SDK 1.30.0:

- 62 pytest cases passed, using a stateful HTTP API double.
- Actual MCP stdio subprocess startup, initialize, tool discovery and resource read passed.
- Streamable HTTP initialize, bearer rejection, Host and Origin checks passed in ASGI tests.
- MCP tool calls verified structured results and schema rejection.
- Preview-only behavior; exact create/update/delete readback; header-only create responses;
  stale plans; concurrent apply; partial failures; cancellation; write backup failure;
  no mutation retry; DNS restore; mail-record preservation; DDNS and domain scoping passed.
- Ruff lint and formatting passed; pip reported no broken requirements.
- Installable Python wheel built successfully.

Not yet verified: real Domeneshop account acceptance, public DNS propagation, production gateway
integration, Docker image execution (Docker unavailable locally). GitHub CI additionally runs
Linux and Windows on Python 3.11, 3.12 and 3.13; consult the actual workflow result for its status.

No live domain, DNS, forward or invoice changes were made for these tests.
