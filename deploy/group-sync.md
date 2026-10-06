# Optional Keycloak to LiteLLM reconciliation

`keycloak_sync.py` uses Python's standard library and HTTPS APIs. It runs outside the MCP
container; identity administration is never exposed as a tool to models.

## Setup

1. Pin the exact Keycloak realm issuer used by gateway SSO. This adapter requires a single
   verified SSO issuer; do not apply it indiscriminately to mixed-provider users.
2. Create a confidential Keycloak client with service-account authentication only. Disable
   browser login, password grant and token exchange. Grant `realm-management / view-users`
   for group/user reads. This reads realm user data but cannot administer users or groups.
3. Create dedicated LiteLLM teams, initially blocked, with `models: ["no-default-models"]`,
   no member management permissions, and no unrelated access groups. Set `mcp_servers` to
   the single server UUID and `mcp_tool_permissions` to the corresponding exact list exported
   by the script. Metadata must contain `managed_by: domeneshop-keycloak-sync-v1` and
   `keycloak_group` with the full group path. Reconciliation removes unwanted creator membership.
4. Populate `group-sync.example.json` privately. The example maps Senior access to
   `staff-internal`, explicitly including invoices with `full_access: true`. Operator
   membership takes priority over reader membership.

The gateway credential needs team-management access; lifecycle revocation also needs user
management. Keep it on the deployment host, readable only by root. Never commit populated
configuration or use this administrative credential for client MCP access.

## Lifecycle mode is explicit

`revoke_inactive_users` defaults to false in the example. Enabling it removes ALL LiteLLM
access of a confirmed deleted/disabled linked person, including unrelated teams and keys.
It uses ordinary management APIs, not Enterprise SCIM or database writes. It does not change
Keycloak or other provider accounts.

The private `verified-subjects.json` registry pins the issuer and previously observed
subject/user pairs. Unknown 404s do not authorize deletion. Preserve this registry across
updates. Reactivating an account does not automatically restore old keys or unrelated grants.
Local accounts without an SSO subject are not managed by this job.

Run once and read back team membership before enabling the timer. Confirm linked identities
belong to the configured issuer. No invitations or messages are sent.

## Linux installation

Install the script at `/opt/domeneshop-mcp/keycloak_sync.py`, private configuration at
`/opt/domeneshop-mcp/private/group-sync.json` (0600), and create `private/revocations` (0700).
Install the supplied service/timer units in `/etc/systemd/system/`, then run:

```sh
systemctl daemon-reload
systemctl start domeneshop-group-sync.service
systemctl enable --now domeneshop-group-sync.timer
systemctl status domeneshop-group-sync.timer
journalctl -u domeneshop-group-sync.service --since '10 minutes ago'
```

The timer runs every 60 seconds. Monitor failed runs and stale successful runs. Output contains
status, counts and exception types, never credentials or raw API responses. On source failure,
the job attempts to block its dedicated teams; a gateway outage can prevent that action too.
An enabled timer alone is not proof of successful synchronization.

Disabling the timer leaves existing grants intact. Block its dedicated teams before stopping
it, and review independent grants/keys. A successful restart reconciles and unblocks its teams.

## Verification

Test allowed/denied users, reader calls to operator tools, membership removal, source identity
removal/deactivation, stale credentials and source failure/recovery. Do not describe a synthetic
identity test as deletion of a real human account. Live DNS writes require a separately approved
test domain and are unnecessary for verifying permission boundaries.
