# Domeneshop MCP

An open-source [Model Context Protocol](https://modelcontextprotocol.io/) server for
[Domeneshop](https://api.domeneshop.no/docs/). Use domain names instead of remembering IDs,
preview DNS changes, then apply with conflict detection, backups and independent readback.

**Status: initial implementation. Automated tests use a simulated upstream API. Real-account
acceptance and deployment are separate steps; no production DNS is changed during setup.**

## Features

- All 15 operations documented in Domeneshop API v0: domains, DNS CRUD, HTTP forwards CRUD,
  invoices and dynamic DNS, including multiple hostnames and IPv4/IPv6 addresses.
- DNS types: A, AAAA, CNAME, MX, SRV, TLSA, TXT, plus ANAME, CAA, DS and NS from Domeneshop's
  official Python client's type catalog. The latter four need real-account acceptance.
- Shortcuts: website setup, verification TXT, exact IP replacement across selected domains,
  additive DNS copy/import, export, backup restore, configuration checks and expiry overview.
- Local **stdio** and authenticated **Streamable HTTP** at `/mcp` for a central gateway.
- Typed inputs, TLS verification, bounded read retries, domain allowlisting and read-only default.

The upstream API does **not** offer domain purchase/transfer, nameserver changes, mailbox
administration, invoice payment or webhosting administration. This server cannot add those
capabilities. [API coverage and sources](docs/api-coverage.md).

## Install

Python 3.11+ (tested on 3.12). From the project directory:

```sh
python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows PowerShell instead:
# .\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
```

Obtain a token/secret from [Domeneshop's API settings](https://www.domeneshop.no/admin?view=api).
Inject credentials through your process environment or a secret manager. Never commit them or
paste them into chat. `.env` files are deliberately **not** loaded automatically.

| Environment variable | Purpose / default |
| --- | --- |
| `DOMENESHOP_TOKEN` | Required upstream API token |
| `DOMENESHOP_SECRET` | Required upstream API secret |
| `DOMENESHOP_ALLOW_WRITES` | `false`; set exactly `true` to enable `apply_plan` |
| `DOMENESHOP_ALLOWED_DOMAINS` | Optional comma-separated exact domain names; omitted = all |
| `DOMENESHOP_STATE_DIR` | Backups/receipts, default `~/.domeneshop-mcp` |
| `MCP_HTTP_TOKEN` | Required for HTTP; separate random secret, at least 32 characters |
| `MCP_ALLOWED_HOSTS` | HTTP Host allowlist; local hosts by default; explicit for non-loopback binds |
| `MCP_ALLOWED_ORIGINS` | Optional exact HTTP Origin allowlist; local origins by default |

Domain allowlisting also disables invoices because invoices are account-wide. All HTTP callers
with the same bearer credential share one upstream account and plans. This is a server for one
trusted operator/team, **not tenant isolation or per-user authorization**. Run separate instances
and credentials for separate trust boundaries.

### Local stdio

```sh
domeneshop-mcp
```

Point your MCP client at the installed `domeneshop-mcp` executable (or the virtual environment's
Python with arguments `-m domeneshop_mcp.server`). Inherit/inject the environment above. Use an
absolute executable path when the client does not inherit your activated virtual environment.
Protocol messages use stdout; application transport logging must stay on stderr.

### Central HTTP / LiteLLM gateway

```sh
domeneshop-mcp --transport http --host 127.0.0.1 --port 8000
```

The MCP endpoint is `http://127.0.0.1:8000/mcp` with header
`Authorization: Bearer <MCP_HTTP_TOKEN>`. For remote access, terminate valid HTTPS at your
reverse proxy and set `MCP_ALLOWED_HOSTS` to its actual hostname (include port when applicable).
Do not expose plain HTTP to the internet. Forward the bearer header and configure the gateway
to send it. OAuth-only clients need an authenticating gateway: this server does not provide OAuth.

Use **one worker/process**: preview plans and their lock are in memory. Plans expire after ten
minutes and disappear on restart. A shared backup directory does not make multiple workers safe.
TLS to Domeneshop is always verified; redirects and environment-supplied HTTP proxies are disabled.

See [deployment examples](deploy/README.md) for Docker and a systemd unit. These are templates;
they do not install, publish or change your gateway automatically.

## Typical workflows

1. `list_domains`, `list_dns_records` and `list_forwards` inspect current state.
2. Call `setup_website`, `add_verification_txt`, `plan_dns_batch` or a CRUD tool to get a preview.
3. Inspect `actions`, `before`, `after`, warnings and expiry. The caller must have user authorization.
4. Call `apply_plan(plan_id)` to execute. Check its returned **status**.
5. `get_plan` retrieves the result; `export_zone` reads fresh state.

Example natural-language requests:

- «Vis domener som utløper de neste 30 dagene.»
- «Forbered example.no med 192.0.2.10 og www som CNAME.»
- «Legg til denne verifiserings-TXT-en uten å erstatte SPF.»
- «Vis hvilke poster som endres når 192.0.2.10 byttes til 192.0.2.20 på disse domenene.»

`ensure_dns_records` only adds missing exact records by default. With `replace_rrsets=true`,
all existing records of each supplied host/type are replaced by the supplied set. Other sets
are preserved. `setup_website` uses that replacement mode, preserves an omitted IP family,
and stops for conflicting aliases/forwards. It does not provision hosting or certificates.

## What a write result means

| Status | Meaning |
| --- | --- |
| `verified` | All planned operations passed independent API readback and state checks |
| `accepted_unverified` | Upstream accepted DDNS but intended address was not proven |
| `partial_or_unknown` | An operation or readback failed; some writes may have happened |

Backups are written **before** the first mutation; an unwritable state directory blocks writes.
Each successful operation is read back. The server checks for state drift before and between
operations. The upstream API has no conditional-write/version contract, so there remains a race
between a preflight read and a write. Batch operations are not transactions. On failure, execution
stops; there is no blind retry or automatic rollback. Inspect current state before preparing a new
plan. `restore_dns_backup` previews DNS restoration; forwards must be restored explicitly.

The plan ID is not a human-approval mechanism. Your MCP client/gateway is responsible for deciding
whether the user authorized an operation. Verified API state does not prove public DNS propagation,
website availability or email delivery. Automatic-IP DDNS uses the **server's** egress IP.

Backups contain DNS/TXT data and should be private. On POSIX they use restrictive permissions;
on Windows protect the state directory with the service account's ACL. Backups are retained until
the operator removes them. Routine logs omit API payloads and credentials; do not enable HTTP debug
logging around a credentialed deployment.

## Development

```sh
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

The official MCP Python SDK is pinned to its maintained 1.x line (`>=1.28,<2`) to keep the
FastMCP API stable. Upgrade to 2.x as an explicit compatibility change with protocol tests.
Runtime dependencies are pinned in `requirements.lock`; see deployment instructions for using it.
CI checks Linux/Windows and supported Python versions. [Contributing](CONTRIBUTING.md).

## License

MIT for this project's code. Domeneshop is a third-party service; this is an independent project,
not an official Domeneshop product. `docs/domeneshop-openapi.json` is a reference snapshot of
Domeneshop's public API specification; upstream attribution/terms remain applicable.
