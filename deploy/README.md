# Deployment templates

These templates are for a single trusted account/team. No credentials belong in the image or repo.

## Docker

Build `docker build -t domeneshop-mcp .`, then run with environment variables injected by your
orchestrator/secret manager. Set `MCP_ALLOWED_HOSTS` to the actual proxy hostname and
`DOMENESHOP_STATE_DIR=/state`. Mount a private, writable volume at `/state` for UID 10001.
The container listens on 8000. Publish only to loopback (`-p 127.0.0.1:8000:8000`) behind a
TLS-validating HTTPS reverse proxy. Alternatively, attach it to the gateway's private Docker network
without publishing a port. The proxy must preserve the configured Host and Authorization.

For reproducible dependencies outside Docker:

```sh
python -m pip install -r requirements.lock
python -m pip install --no-deps .
```

## systemd

Install a venv in `/opt/domeneshop-mcp/.venv`, the project into it, and create a dedicated
`domeneshop-mcp` user. Copy the example unit, adapting paths as needed. Put the required environment
in `/etc/domeneshop-mcp/environment`, owned by root with mode 0600. systemd reads this file before
switching to the service user. Never include the real file in source control.

Use one process/worker. After a restart, prepare new plans; never try to resume an interrupted plan.
Take an independent DNS snapshot before real-account acceptance. Verify one harmless authorized
TXT create/read/delete on a test domain, then test the intended client/gateway end to end.

## LiteLLM

Use Streamable HTTP. For a container named `domeneshop-mcp` on the gateway's private Docker network,
set `MCP_ALLOWED_HOSTS=domeneshop-mcp:8000` and register the following through LiteLLM's admin UI or
`POST /v1/mcp/server`. Replace the placeholder privately with the server's `MCP_HTTP_TOKEN`:

```json
{
  "server_name": "domeneshop",
  "alias": "domeneshop",
  "url": "http://domeneshop-mcp:8000/mcp",
  "transport": "http",
  "auth_type": "bearer_token",
  "credentials": {"auth_value": "REPLACE_WITH_PRIVATE_UPSTREAM_TOKEN"},
  "allow_all_keys": false,
  "available_on_public_internet": false
}
```

The internal hostname must resolve inside LiteLLM's container. Include any health-check hostname
in `MCP_ALLOWED_HOSTS` as well. Keep the Domeneshop token/secret in the server's secret store;
LiteLLM only needs the separate upstream bearer token.

Grant the returned server UUID to the intended virtual key using
`object_permission.mcp_servers`. When updating an existing key, first read its permissions and
preserve other grants: supplied permission arrays replace existing arrays. A dedicated key can
restrict `allowed_routes` to `/mcp`, `/domeneshop/mcp`, `/mcp-rest/tools/list` and
`/mcp-rest/tools/call` to exclude model and admin routes.

Connect an MCP client to `https://YOUR_GATEWAY/domeneshop/mcp` with the header
`x-litellm-api-key: Bearer YOUR_LITELLM_VIRTUAL_KEY`. Verify both the allowed key and an ungranted key.
Tools appear with names such as `domeneshop-list_domains`. Test domain reads and a preview before
authorizing live writes. `DOMENESHOP_ALLOW_WRITES=true` enables `apply_plan`; every mutation still
requires a previously generated plan. Client/gateway user authorization remains separate.

This integration was tested with LiteLLM 1.103.2. See LiteLLM's
[MCP configuration reference](https://docs.litellm.ai/docs/mcp_config_reference) and
[access management guide](https://docs.litellm.ai/docs/mcp_grant_access) when adapting other versions.
