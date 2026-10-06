"""Reconcile two dedicated LiteLLM teams with explicitly configured Keycloak groups.

Run once per minute with a root-only JSON configuration. This is an optional
deployment component, not part of the MCP server or an authentication provider.
Only already linked SSO users are admitted; email is never an identity key.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MANAGER = "domeneshop-keycloak-sync-v1"
READ_TOOLS = [
    "list_domains",
    "get_domain",
    "list_dns_records",
    "get_dns_record",
    "list_forwards",
    "get_forward",
    "account_overview",
    "export_zone",
    "audit_dns",
]
OPERATOR_TOOLS = READ_TOOLS + [
    "create_dns_record",
    "update_dns_record",
    "delete_dns_record",
    "plan_dns_batch",
    "create_forward",
    "update_forward",
    "delete_forward",
    "update_dynamic_dns",
    "ensure_dns_records",
    "setup_website",
    "add_verification_txt",
    "replace_ip",
    "copy_dns_records",
    "restore_dns_backup",
    "get_plan",
    "apply_plan",
]
FULL_TOOLS = OPERATOR_TOOLS + ["list_invoices", "get_invoice"]


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"API request failed with HTTP {status}")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base, token=None):
        url = urlsplit(base)
        if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
            raise ValueError("API base must be an HTTPS URL without credentials, query or fragment")
        self.base = base.rstrip("/")
        self.token = token
        self.opener = build_opener(NoRedirects())

    def __call__(self, path, data=None, *, form=False):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = None
        if data is not None:
            headers["Content-Type"] = (
                "application/x-www-form-urlencoded" if form else "application/json"
            )
            body = (urlencode(data) if form else json.dumps(data)).encode()
        try:
            with self.opener.open(Request(self.base + path, body, headers), timeout=15) as r:
                value = r.read()
                return json.loads(value) if value else None
        except HTTPError as e:
            # Never log response bodies, request bodies, headers or credentials.
            raise APIError(e.code) from None


def group_members(api, group):
    path = "/groups/" + quote(group["id"], safe="")
    if api(path).get("path") != group["path"]:
        raise ValueError("Configured group ID/path mismatch")
    members = set()
    for first in range(0, 100_000, 100):
        batch = api(
            path
            + "/members?"
            + urlencode({"first": first, "max": 100, "briefRepresentation": "false"})
        )
        if not isinstance(batch, list):
            raise ValueError("Invalid group membership response")
        for user in batch:
            if user.get("enabled") is True:
                members.add(user["id"])
        if len(batch) < 100:
            return members
    raise ValueError("Group pagination limit exceeded")


def linked_users(gateway):
    users = []
    for page in range(1, 1001):
        data = gateway(f"/user/list?page={page}&page_size=100")
        users.extend(data["users"])
        if page >= data["total_pages"]:
            return users
    raise ValueError("User pagination limit exceeded")


def desired_members(users, staff, readers, operators):
    """Exact SSO-subject join, with ambiguity and inactive users failing closed."""
    by_subject = {}
    for user in users:
        subject = user.get("sso_user_id")
        if subject:
            by_subject.setdefault(subject, []).append(user)
    desired = {"read": set(), "operators": set()}
    for subject in staff & (readers | operators):
        candidates = by_subject.get(subject, [])
        if len(candidates) != 1 or candidates[0].get("scim_active") is False:
            continue
        role = "operators" if subject in operators else "read"
        desired[role].add(candidates[0]["user_id"])
    return desired


def team_info(gateway, team_id):
    return gateway("/team/info?" + urlencode({"team_id": team_id}))["team_info"]


def validate_team(team, group, server_id, role):
    metadata = team.get("metadata") or {}
    if metadata.get("managed_by") != MANAGER or metadata.get("keycloak_group") != group["path"]:
        raise ValueError("Refusing to modify a team not owned by this reconciler")
    permission = team.get("object_permission") or {}
    tools = READ_TOOLS if role == "read" else OPERATOR_TOOLS
    if role == "operators" and group.get("full_access") is True:
        tools = FULL_TOOLS
    if (
        permission.get("mcp_servers") != [server_id]
        or permission.get("mcp_tool_permissions") != {server_id: tools}
        or permission.get("mcp_access_groups")
        or permission.get("mcp_toolsets")
        or team.get("access_group_ids")
    ):
        raise ValueError("Managed team permission drift; keep the team blocked and review")


def reconcile(gateway, groups, desired, server_id):
    changes = {"added": 0, "removed": 0}
    for role, group in groups.items():
        team_id = group["team_id"]
        team = team_info(gateway, team_id)
        validate_team(team, group, server_id, role)
        current = {u["user_id"] for u in team.get("members_with_roles", [])}
        # Remove first; never leave an old grant behind during a downgrade.
        for user_id in sorted(current - desired[role]):
            gateway("/team/member_delete", {"team_id": team_id, "user_id": user_id})
            changes["removed"] += 1
        for user_id in sorted(desired[role] - current):
            gateway(
                "/team/member_add",
                {
                    "team_id": team_id,
                    "member": {"user_id": user_id, "role": "user"},
                },
            )
            changes["added"] += 1
        after = team_info(gateway, team_id)
        actual = {u["user_id"] for u in after.get("members_with_roles", [])}
        if actual != desired[role]:
            raise RuntimeError("Team membership read-back mismatch")
        if after.get("blocked"):
            gateway("/team/update", {"team_id": team_id, "blocked": False})
    return changes


def block_managed_teams(gateway, groups):
    """Stop new use if the identity source or reconciliation cannot be verified."""
    blocked = 0
    for group in groups.values():
        team = team_info(gateway, group["team_id"])
        metadata = team.get("metadata") or {}
        if metadata.get("managed_by") != MANAGER:
            raise ValueError("Team ownership mismatch")
        gateway("/team/update", {"team_id": group["team_id"], "blocked": True})
        blocked += 1
    return blocked


def inactive_identities(keycloak, users, known_links=None):
    """Only a definitive 404 or enabled=false means revocation, never a timeout."""
    inactive = []
    for user in users:
        subject = user.get("sso_user_id")
        if not subject:
            continue
        try:
            source = keycloak("/users/" + quote(subject, safe=""))
        except APIError as e:
            if e.status != 404:
                raise
            if known_links is None or (subject, user["user_id"]) in known_links:
                inactive.append(user)
            continue
        if source.get("id") != subject:
            raise ValueError("Keycloak subject mismatch")
        if source.get("enabled") is False:
            inactive.append(user)
        elif source.get("enabled") is not True:
            raise ValueError("Missing Keycloak enabled state")
    return inactive


def verified_links(keycloak, users, audit_dir, issuer):
    """Remember only subject/user pairs actually observed at this exact issuer.

    An unknown 404 is not evidence that an imported identity belonged to this
    provider. Previously verified pairs remain recorded for deletion detection.
    """
    directory = Path(audit_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "verified-subjects.json"
    previous = json.loads(path.read_text()) if path.exists() else {"issuer": issuer, "links": []}
    if previous["issuer"] != issuer:
        raise ValueError("Revocation registry belongs to a different issuer")
    known = {tuple(pair) for pair in previous["links"]}
    for user in users:
        subject = user.get("sso_user_id")
        if not subject:
            continue
        try:
            source = keycloak("/users/" + quote(subject, safe=""))
        except APIError as error:
            if error.status == 404:
                continue
            raise
        if source.get("id") != subject:
            raise ValueError("Keycloak subject mismatch")
        known.add((subject, user["user_id"]))
    temporary = directory / (uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"issuer": issuer, "links": sorted(known)}, f)
    temporary.replace(path)
    return known


def revoke_identity(gateway, user, audit_dir):
    """Remove a confirmed inactive identity and all its gateway keys/memberships.

    Uses supported OSS management APIs, not SCIM or database writes. This opt-in
    affects ALL gateway access of the person. Receipts preserve permission IDs
    for administrator review; reactivation does not restore old keys or grants.
    """
    user_id = user["user_id"]
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = {
        "at": datetime.now(UTC).isoformat(),
        "before": {
            k: user.get(k)
            for k in (
                "user_id",
                "sso_user_id",
                "user_role",
                "teams",
                "models",
                "object_permission_id",
            )
        },
    }
    path = audit_dir / (uuid.uuid4().hex + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(receipt, f)
    # Narrow cached permissions before deleting the identity. The final delete
    # is what invalidates identity-bound OAuth sessions at their next reload.
    gateway(
        "/user/update",
        {
            "user_id": user_id,
            "user_role": "internal_user_viewer",
            "models": ["no-default-models"],
            "object_permission": {"mcp_servers": ["no-mcp-servers"]},
        },
    )
    for team_id in user.get("teams") or []:
        gateway("/team/member_delete", {"team_id": team_id, "user_id": user_id})
    gateway("/user/delete", {"user_ids": [user_id]})
    if any(u["user_id"] == user_id for u in linked_users(gateway)):
        raise RuntimeError("Identity revocation read-back failed")


def run(config):
    gateway = API(config["gateway_url"], config["gateway_key"])
    groups = config["groups"]
    if set(groups) != {"read", "operators"}:
        raise ValueError("Exactly read and operators groups must be configured")
    try:
        issuer = config["issuer"].rstrip("/")
        token = API(issuer)(
            "/protocol/openid-connect/token",
            {
                "grant_type": "client_credentials",
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
            },
            form=True,
        )["access_token"]
        parts = issuer.rsplit("/realms/", 1)
        if len(parts) != 2 or not parts[1] or "/" in parts[1]:
            raise ValueError("Expected an exact Keycloak realm issuer")
        keycloak = API(parts[0] + "/admin/realms/" + parts[1], token)
        staff = group_members(keycloak, config["staff_group"])
        readers = group_members(keycloak, groups["read"])
        operators = group_members(keycloak, groups["operators"])
        users = linked_users(gateway)
        revoked = 0
        if config.get("revoke_inactive_users") is True:
            # Fetch every source status before changing any identity.
            known = verified_links(keycloak, users, config["revocation_audit_dir"], issuer)
            inactive = inactive_identities(keycloak, users, known)
            for user in inactive:
                revoke_identity(gateway, user, config["revocation_audit_dir"])
                revoked += 1
            removed_ids = {u["user_id"] for u in inactive}
            users = [u for u in users if u["user_id"] not in removed_ids]
        desired = desired_members(users, staff, readers, operators)
        changes = reconcile(gateway, groups, desired, config["server_id"])
        return {
            "status": "ok",
            **changes,
            "identities_revoked": revoked,
            "members": {k: len(v) for k, v in desired.items()},
        }
    except Exception as error:
        # Best effort if the gateway itself is unavailable. Monitoring must alert
        # on service/timer failures; a stopped timer is not a revocation guarantee.
        try:
            blocked = block_managed_teams(gateway, groups)
        except Exception:
            blocked = None
        return {
            "status": "failed",
            "managed_teams_blocked": blocked,
            "error_type": type(error).__name__,
        }


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    if os.name != "nt" and args.config.stat().st_mode & 0o077:
        raise SystemExit("Configuration must be readable only by its owner")
    result = run(json.loads(args.config.read_text(encoding="utf-8")))
    print(json.dumps(result))
    sys.exit(0 if result["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
