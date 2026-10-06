import asyncio
import json
import time

import pytest

from domeneshop_mcp.models import DNSChange, DNSRecord, Forward


def create(host="test", data="192.0.2.2"):
    return DNSChange(action="create", record=DNSRecord(type="A", host=host, data=data))


async def test_preview_is_read_only_and_apply_verifies(service, api, original):
    preview = await service.dns_plan("example.no", [create()])
    assert api.dns == original
    assert api.writes == 0
    result = await service.apply(preview["plan_id"])
    assert result["status"] == "verified"
    assert result["completed"][0]["readback"]["host"] == "test"
    assert api.writes == 1
    backup = json.loads((service.state_dir / result["backup"]).read_text())
    assert backup["snapshots"]["1"]["dns"] == original[1]
    assert await service.apply(preview["plan_id"]) == result
    assert api.writes == 1


async def test_create_location_header(service, api):
    api.location_only = True
    plan = await service.dns_plan(1, [create()])
    assert (await service.apply(plan["plan_id"]))["status"] == "verified"


async def test_update_and_delete_exact_record(service, api):
    plan = await service.dns_plan(
        1, [DNSChange(action="update", record_id=10, record=DNSRecord(type="A", data="192.0.2.9"))]
    )
    assert (await service.apply(plan["plan_id"]))["status"] == "verified"
    assert api.dns[1][0]["id"] == 10
    assert api.dns[1][0]["data"] == "192.0.2.9"
    plan = await service.dns_plan(1, [DNSChange(action="delete", record_id=10)])
    assert (await service.apply(plan["plan_id"]))["status"] == "verified"
    assert all(r["id"] != 10 for r in api.dns[1])


async def test_identical_create_noop(service, api):
    plan = await service.dns_plan(1, [create("@", "192.0.2.1")])
    assert plan["operation_count"] == 0
    assert (await service.apply(plan["plan_id"]))["status"] == "verified"
    assert api.writes == 0


async def test_drift_blocks_all_writes(service, api):
    plan = await service.dns_plan(1, [create()])
    api.dns[1][0]["ttl"] = 600
    with pytest.raises(ValueError, match="changed since preview"):
        await service.apply(plan["plan_id"])
    assert api.writes == 0


async def test_readonly_and_expiry(service, api):
    plan = await service.dns_plan(1, [create()])
    service.writable = False
    with pytest.raises(ValueError, match="disabled"):
        await service.apply(plan["plan_id"])
    service.writable = True
    service.plans[plan["plan_id"]]["expires_at"] = time.time() - 1
    with pytest.raises(ValueError, match="expired"):
        await service.apply(plan["plan_id"])
    assert api.writes == 0


async def test_backup_failure_blocks_writes(service, api, tmp_path):
    service.state_dir = tmp_path / "not-a-directory"
    service.state_dir.write_text("occupied")
    plan = await service.dns_plan(1, [create()])
    with pytest.raises(OSError):
        await service.apply(plan["plan_id"])
    assert api.writes == 0


async def test_partial_failure_stops_no_retry(service, api):
    api.fail_write_number = 2
    plan = await service.dns_plan(1, [create("one"), create("two"), create("three")])
    result = await service.apply(plan["plan_id"])
    assert result["status"] == "partial_or_unknown"
    assert len(result["completed"]) == 1
    assert api.writes == 2
    assert await service.apply(plan["plan_id"]) == result
    assert api.writes == 2


async def test_accepted_but_not_saved_is_not_success(service, api):
    api.ignore_write = True
    plan = await service.dns_plan(1, [DNSChange(action="delete", record_id=10)])
    result = await service.apply(plan["plan_id"])
    assert result["status"] == "partial_or_unknown"
    assert "still exists" in result["error"]


async def test_external_mutation_between_operations_stops(service, api):
    def external_change(fake):
        fake.dns[1][0]["ttl"] = 120

    api.after_write = external_change
    plan = await service.dns_plan(1, [create("one"), create("two")])
    result = await service.apply(plan["plan_id"])
    assert result["status"] == "partial_or_unknown"
    assert api.writes == 1


async def test_concurrent_apply_does_not_duplicate(service, api):
    plan = await service.dns_plan(1, [create()])
    results = await asyncio.gather(service.apply(plan["plan_id"]), service.apply(plan["plan_id"]))
    assert results[0] == results[1]
    assert api.writes == 1


async def test_cancelled_write_consumes_plan_with_unknown_result(service, api, monkeypatch):
    plan = await service.dns_plan(1, [create()])

    async def interrupted(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "execute", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await service.apply(plan["plan_id"])
    result = service.status(plan["plan_id"])
    assert result["status"] == "partial_or_unknown"
    assert await service.apply(plan["plan_id"]) == result
    assert api.writes == 0


async def test_preview_result_cannot_mutate_stored_plan(service, api):
    plan = await service.dns_plan(1, [create()])
    plan["actions"][0]["after"]["data"] = "192.0.2.250"
    result = await service.apply(plan["plan_id"])
    assert result["completed"][0]["readback"]["data"] == "192.0.2.2"


async def test_wrong_readback_stops_batch(service, api):
    def unexpected_result(fake):
        fake.dns[1][-1]["data"] = "192.0.2.250"

    api.after_write = unexpected_result
    plan = await service.dns_plan(1, [create("one"), create("two")])
    result = await service.apply(plan["plan_id"])
    assert result["status"] == "partial_or_unknown"
    assert api.writes == 1
    assert "readback differs" in result["error"]


async def test_plan_limit_and_restore_path_validation(service):
    service.max_actions = 1
    with pytest.raises(ValueError, match="limit"):
        await service.dns_plan(1, [create("one"), create("two")])
    with pytest.raises(ValueError, match="Invalid backup"):
        await service.restore("../secrets", 1)


async def test_cname_and_forward_conflicts(service, api):
    with pytest.raises(ValueError, match="CNAME conflict"):
        await service.dns_plan(
            1, [DNSChange(action="create", record=DNSRecord(type="CNAME", data="other.no"))]
        )
    api.forwards[1].append({"host": "test", "url": "https://example.org", "frame": False})
    with pytest.raises(ValueError, match="HTTP forward"):
        await service.dns_plan(1, [create()])
    with pytest.raises(ValueError, match="conflicts"):
        await service.forward_plan(1, "create", forward=Forward(url="https://example.org"))


async def test_forward_full_lifecycle(service, api):
    for action, url in [("create", "https://example.org"), ("update", "https://other.org")]:
        plan = await service.forward_plan(1, action, forward=Forward(host="go", url=url))
        assert (await service.apply(plan["plan_id"]))["status"] == "verified"
        assert (await service.forward_get(1, "go"))["url"] == url
    plan = await service.forward_plan(1, "delete", host="go")
    assert (await service.apply(plan["plan_id"]))["status"] == "verified"
    assert api.forwards[1] == []


async def test_website_preserves_mail_and_omitted_ipv6(service, api, original):
    api.dns[1].append({"id": 15, "host": "@", "type": "AAAA", "data": "2001:db8::1", "ttl": 3600})
    plan = await service.website("example.no", "192.0.2.9", None, True, 600)
    assert (await service.apply(plan["plan_id"]))["status"] == "verified"
    assert original[1][1] in api.dns[1] and original[1][2] in api.dns[1]
    assert any(r["id"] == 15 for r in api.dns[1])
    assert any(r["host"] == "www" and r["type"] == "CNAME" for r in api.dns[1])


async def test_verification_txt_is_additive_and_idempotent(service, api, original):
    records = [DNSRecord(type="TXT", data="verify-123")]
    plan = await service.ensure_records(1, records)
    await service.apply(plan["plan_id"])
    assert original[1][2] in api.dns[1]
    assert (await service.ensure_records(1, records))["operation_count"] == 0


async def test_copy_and_ip_replacement(service, api):
    plan = await service.copy_dns(1, 2, ["@"])
    assert plan["operation_count"] == 3
    assert (await service.apply(plan["plan_id"]))["status"] == "verified"
    assert any(r["data"] == "mail.example.no" for r in api.dns[2])
    plan = await service.replace_ip([1, "example.no", 2], "192.0.2.1", "192.0.2.5")
    assert plan["operation_count"] == 2
    assert (await service.apply(plan["plan_id"]))["status"] == "verified"


async def test_restore_previews_then_restores_content(service, api, original):
    plan = await service.website(1, "192.0.2.9", None, True, 3600)
    await service.apply(plan["plan_id"])
    restore = await service.restore(plan["plan_id"], 1)
    assert (await service.apply(restore["plan_id"]))["status"] == "verified"

    def clean(rows):
        return sorted(
            json.dumps({k: v for k, v in r.items() if k != "id"}, sort_keys=True) for r in rows
        )

    assert clean(api.dns[1]) == clean(original[1])


async def test_allowlist_exact_and_id_bypass_blocked(service):
    service.allowed_domains = {"example.no"}
    assert len(await service.domains()) == 1
    with pytest.raises(ValueError, match="outside"):
        await service.domain(2)
    with pytest.raises(ValueError, match="exact"):
        await service.domain("example")


async def test_ddns_multiple_hosts_addresses(service, api):
    plan = await service.ddns_plan(
        ["home.example.no", "home.other.no"], ["192.0.2.3", "2001:db8::2"]
    )
    assert api.writes == 0
    result = await service.apply(plan["plan_id"])
    assert result["status"] == "verified"
    assert api.writes == 1
    assert all(any(r["host"] == "home" for r in api.dns[d]) for d in [1, 2])


async def test_ddns_auto_ip_is_explicit_and_unverified(service):
    with pytest.raises(ValueError, match="explicitly"):
        await service.ddns_plan(["home.example.no"], None)
    plan = await service.ddns_plan(["home.example.no"], None, True)
    result = await service.apply(plan["plan_id"])
    assert result["status"] == "accepted_unverified"


async def test_audit_and_overview(service, api):
    api.dns[1].append({"id": 50, "host": "@", "type": "TXT", "data": "v=spf1 +all", "ttl": 3600})
    audit = await service.audit(1)
    assert "multiple_spf_records" in {f["code"] for f in audit["findings"]}
    overview = await service.overview()
    assert overview["domain_count"] == 2
    assert overview["expiring"][0]["domain"] == "other.no"
