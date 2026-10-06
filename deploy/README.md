# Deployment templates

These templates are for a single trusted account/team. No credentials belong in the image or repo.

## Docker

Build `docker build -t domeneshop-mcp .`, then run with environment variables injected by your
orchestrator/secret manager. Set `MCP_ALLOWED_HOSTS` to the actual proxy hostname and
`DOMENESHOP_STATE_DIR=/state`. Mount a private, writable volume at `/state` for UID 10001.
The container listens on 8000. Publish only to loopback (`-p 127.0.0.1:8000:8000`) behind a
TLS-validating HTTPS reverse proxy. The proxy must preserve the configured Host and Authorization.

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
