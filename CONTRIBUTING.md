# Contributing

Open an issue describing the use case or bug before substantial changes. Keep fixes focused and
include tests for externally visible behavior. Never include real API credentials, customer DNS
exports or private invoice URLs in issues, logs, fixtures or commits.

Install with `python -m pip install -e '.[dev]'`. Run pytest and Ruff as described in the README.
Use the fake upstream API for tests. Real API writes need a domain you own, explicit authorization,
a before snapshot and independent readback. Do not run destructive acceptance tests on customer DNS.

Keep stdio output valid MCP. All mutations must go through the plan/apply engine; DDNS is a write
even though the upstream uses GET. New tools need accurate MCP annotations and a documented API
mapping. Do not retry mutations after a timeout or disable TLS verification.

For a suspected vulnerability, use the repository's private vulnerability reporting if available;
otherwise contact the maintainer privately before opening a public issue containing exploit details.
