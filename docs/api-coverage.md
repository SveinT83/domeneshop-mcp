# API coverage

Sources retrieved 2026-10-06:

- [Domeneshop API v0 documentation](https://api.domeneshop.no/docs/)
- [Official Python client](https://github.com/domeneshop/python-domeneshop/blob/master/domeneshop/client.py)
- [MCP Python SDK 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)

`domeneshop-openapi.json` is the specification embedded in the official Redoc page, saved for
contract comparison. The documentation currently enumerates seven DNS types; the official Python
client also lists ANAME, CAA, DS and NS and their extra fields. Those are included explicitly.

| Method | API path | MCP tool |
| --- | --- | --- |
| GET | `/domains` | `list_domains` |
| GET | `/domains/{domainId}` | `get_domain` |
| GET | `/domains/{domainId}/dns` | `list_dns_records` |
| POST | `/domains/{domainId}/dns` | `create_dns_record` → `apply_plan` |
| GET | `/domains/{domainId}/dns/{recordId}` | `get_dns_record` |
| PUT | `/domains/{domainId}/dns/{recordId}` | `update_dns_record` → `apply_plan` |
| DELETE | `/domains/{domainId}/dns/{recordId}` | `delete_dns_record` → `apply_plan` |
| GET | `/domains/{domainId}/forwards/` | `list_forwards` |
| POST | `/domains/{domainId}/forwards/` | `create_forward` → `apply_plan` |
| GET | `/domains/{domainId}/forwards/{host}` | `get_forward` |
| PUT | `/domains/{domainId}/forwards/{host}` | `update_forward` → `apply_plan` |
| DELETE | `/domains/{domainId}/forwards/{host}` | `delete_forward` → `apply_plan` |
| GET | `/invoices` | `list_invoices` |
| GET | `/invoices/{invoiceId}` | `get_invoice` |
| GET (mutating) | `/dyndns/update` | `update_dynamic_dns` → `apply_plan` |

DNS creation accepts either the documented JSON `{ "id": ... }` response or the `Location`
header used by the official Python client. Returned locations are never followed; only a numeric
record ID is extracted, then read from the fixed trusted API origin.

Inputs enforce TTL 60–604800 in multiples of 60, IP address family, required type-specific fields,
relative host labels, IDNA names, TLSA hashes and path-safe IDs. Forward URLs allow HTTP, HTTPS and
FTP as documented, without embedded credentials. They are stored upstream, never fetched locally.

Live responses may encode numeric type-specific fields as decimal strings (observed for MX
priority and SRV priority/weight/port). The server normalizes these response values for comparisons,
copying and audit. This does not relax the strict integer schema for caller-supplied write inputs.

All documented query filters are exposed. DDNS supports up to nine IP addresses and explicit
server-request-IP mode. Account-wide invoice reads are disabled when domain access is restricted.
