import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "keycloak_sync", Path(__file__).parents[1] / "deploy" / "keycloak_sync.py"
)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def test_subject_join_never_claims_email_or_duplicate_identity():
    users = [
        {"user_id": "owner", "sso_user_id": "s1"},
        {"user_id": "reader", "sso_user_id": "s2"},
        {"user_id": "impostor", "user_email": "owner@example.com"},
        {"user_id": "outside-staff", "sso_user_id": "s3"},
        {"user_id": "disabled", "sso_user_id": "s4", "scim_active": False},
        {"user_id": "duplicate-a", "sso_user_id": "s5"},
        {"user_id": "duplicate-b", "sso_user_id": "s5"},
    ]
    assert sync.desired_members(
        users, {"s1", "s2", "s4", "s5"}, {"s1", "s2", "s3", "s4", "s5"}, {"s1"}
    ) == {"read": {"reader"}, "operators": {"owner"}}


def test_group_pagination_path_and_enabled_are_enforced():
    def api(path):
        if path == "/groups/g":
            return {"path": "/read"}
        if "first=0&" in path:
            return [{"id": str(n), "enabled": True} for n in range(100)]
        return [{"id": "disabled", "enabled": False}]

    assert len(sync.group_members(api, {"id": "g", "path": "/read"})) == 100
    with pytest.raises(ValueError, match="mismatch"):
        sync.group_members(api, {"id": "g", "path": "/other"})


def test_team_scope_cannot_drift_into_other_servers_or_invoice_access():
    group = {"path": "/read"}
    team = {
        "metadata": {"managed_by": sync.MANAGER, "keycloak_group": "/read"},
        "object_permission": {
            "mcp_servers": ["server"],
            "mcp_tool_permissions": {"server": sync.READ_TOOLS},
        },
    }
    sync.validate_team(team, group, "server", "read")
    assert "apply_plan" not in sync.READ_TOOLS
    assert not {"list_invoices", "get_invoice"} & set(sync.OPERATOR_TOOLS)
    assert "apply_plan" in sync.OPERATOR_TOOLS
    team["object_permission"]["mcp_access_groups"] = ["everyone"]
    with pytest.raises(ValueError, match="drift"):
        sync.validate_team(team, group, "server", "read")


def test_removal_addition_readback_and_idempotency():
    group = {"path": "/read", "team_id": "team"}
    team = {
        "metadata": {"managed_by": sync.MANAGER, "keycloak_group": "/read"},
        "object_permission": {
            "mcp_servers": ["server"],
            "mcp_tool_permissions": {"server": sync.READ_TOOLS},
        },
        "members_with_roles": [{"user_id": "removed"}],
        "blocked": True,
    }
    writes = []

    def api(path, data=None):
        if path.startswith("/team/info?"):
            return {"team_info": team}
        writes.append(path)
        if path == "/team/member_delete":
            team["members_with_roles"] = []
        elif path == "/team/member_add":
            team["members_with_roles"].append(data["member"])
        elif path == "/team/update":
            team["blocked"] = data["blocked"]

    assert sync.reconcile(api, {"read": group}, {"read": {"new"}}, "server") == {
        "added": 1,
        "removed": 1,
    }
    assert writes == ["/team/member_delete", "/team/member_add", "/team/update"]
    writes.clear()
    assert sync.reconcile(api, {"read": group}, {"read": {"new"}}, "server") == {
        "added": 0,
        "removed": 0,
    }
    assert writes == []


def test_identity_source_failure_blocks_both_managed_teams(monkeypatch):
    blocked = []

    def fake_api(base, token=None):
        if "idp" in base:
            raise ValueError("TLS or identity provider failure")

        def api(path, data=None, **kwargs):
            if path.startswith("/team/info?"):
                return {"team_info": {"metadata": {"managed_by": sync.MANAGER}}}
            blocked.append(data)

        return api

    monkeypatch.setattr(sync, "API", fake_api)
    result = sync.run(
        {
            "gateway_url": "https://gateway",
            "gateway_key": "test",
            "issuer": "https://idp",
            "groups": {"read": {"team_id": "r"}, "operators": {"team_id": "w"}},
        }
    )
    assert result == {"status": "failed", "managed_teams_blocked": 2, "error_type": "ValueError"}
    assert blocked == [{"team_id": "r", "blocked": True}, {"team_id": "w", "blocked": True}]


def test_transport_rejects_insecure_urls_and_redirects():
    for url in ["http://idp", "https://user:password@idp", "https://idp?key=secret"]:
        with pytest.raises(ValueError):
            sync.API(url)
    assert sync.NoRedirects().redirect_request(None, None, 302, None, None, "https://other") is None


def test_deleted_and_disabled_identities_but_not_unlinked_accounts():
    users = [
        {"user_id": "deleted", "sso_user_id": "missing"},
        {"user_id": "disabled", "sso_user_id": "disabled"},
        {"user_id": "active", "sso_user_id": "active"},
        {"user_id": "local-admin"},
    ]

    def api(path):
        subject = path.rsplit("/", 1)[1]
        if subject == "missing":
            raise sync.APIError(404)
        return {"id": subject, "enabled": subject != "disabled"}

    assert sync.inactive_identities(api, users) == users[:2]
    assert sync.inactive_identities(api, users, set()) == [users[1]]

    def outage(path):
        raise sync.APIError(503)

    with pytest.raises(sync.APIError):
        sync.inactive_identities(outage, users)


def test_revocation_narrows_removes_keys_and_memberships_then_reads_back(tmp_path):
    calls = []

    def api(path, data=None):
        calls.append((path, data))
        return {"users": [], "total_pages": 1}

    user = {"user_id": "deleted", "sso_user_id": "source", "teams": ["one", "two"]}
    sync.revoke_identity(api, user, tmp_path)
    assert [p for p, _ in calls[:4]] == [
        "/user/update",
        "/team/member_delete",
        "/team/member_delete",
        "/user/delete",
    ]
    assert calls[0][1]["object_permission"] == {"mcp_servers": ["no-mcp-servers"]}
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_full_senior_profile_is_explicit_and_contains_all_27_tools():
    assert len(sync.FULL_TOOLS) == len(set(sync.FULL_TOOLS)) == 27
    assert {"apply_plan", "list_invoices", "get_invoice"} <= set(sync.FULL_TOOLS)


def test_revocation_registry_pins_issuer_and_previously_verified_subject(tmp_path):
    user = {"user_id": "u", "sso_user_id": "s"}
    known = sync.verified_links(
        lambda path: {"id": "s", "enabled": True}, [user], tmp_path, "https://idp/realms/a"
    )
    assert known == {("s", "u")}

    def missing(path):
        raise sync.APIError(404)

    assert sync.verified_links(missing, [user], tmp_path, "https://idp/realms/a") == known
    assert sync.inactive_identities(missing, [user], known) == [user]
    with pytest.raises(ValueError, match="different issuer"):
        sync.verified_links(missing, [user], tmp_path, "https://idp/realms/b")
