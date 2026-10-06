import httpx
import pytest
from pydantic import ValidationError

from domeneshop_mcp.client import APIError, DomeneshopClient
from domeneshop_mcp.models import DNSRecord, Forward, hostname


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "A", "data": "2001:db8::1"},
        {"type": "AAAA", "data": "192.0.2.1"},
        {"type": "A", "data": "192.0.2.1", "ttl": 61},
        {"type": "A", "data": "192.0.2.1", "ttl": True},
        {"type": "A", "data": "192.0.2.1", "ttl": 604860},
        {"type": "A", "data": "192.0.2.1", "host": "../../invoices"},
        {"type": "A", "data": "192.0.2.1", "priority": 5},
        {"type": "MX", "data": "mail.example.no"},
        {"type": "MX", "data": "mail.example.no", "priority": "10"},
        {"type": "TLSA", "data": "AABB", "usage": 3, "selector": 1, "dtype": 1},
        {"type": "DS", "data": "AABB", "tag": "12", "alg": 8, "digest": 2},
        {"type": "CAA", "data": "letsencrypt.org", "tag": 0, "flags": 0},
        {"type": "TXT", "data": "abc", "unexpected": "value"},
    ],
)
def test_invalid_dns_rejected(payload):
    with pytest.raises(ValidationError):
        DNSRecord.model_validate(payload)


@pytest.mark.parametrize(
    "kind,extra,data",
    [
        ("A", {}, "192.0.2.1"),
        ("AAAA", {}, "2001:db8::1"),
        ("CNAME", {}, "target.example.no"),
        ("ANAME", {}, "target.example.no"),
        ("NS", {}, "ns.example.no"),
        ("MX", {"priority": 0}, "."),
        ("SRV", {"priority": 0, "weight": 5, "port": 443}, "target.example.no"),
        ("TLSA", {"usage": 3, "selector": 1, "dtype": 1}, "AB" * 32),
        ("DS", {"tag": 1234, "alg": 8, "digest": 2}, "AB" * 32),
        ("CAA", {"flags": 0, "tag": "issue"}, "letsencrypt.org"),
        ("TXT", {}, "Norwegian: æøå"),
    ],
)
def test_all_supported_record_types(kind, extra, data):
    record = DNSRecord(type=kind, data=data, **extra)
    assert record.payload()["data"] == data


def test_idna_and_srv_host():
    assert hostname("BØK.no.") == "xn--bk-lka.no"
    assert DNSRecord(host="_sip._tcp", type="TXT", data="abc").host == "_sip._tcp"
    assert DNSRecord(host="*", type="TXT", data="abc").host == "*"


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "https://user:secret@example.no", "file:///x"]
)
def test_bad_forward_url(url):
    with pytest.raises(ValidationError):
        Forward(url=url)


async def test_fixed_origin_auth_and_redacted_errors():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(401, text="token=secret-do-not-echo")

    client = DomeneshopClient("test-token", "test-secret", transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(APIError, match="401") as caught:
            await client.get("domains")
        assert "secret" not in str(caught.value)
        assert seen[0].url == "https://api.domeneshop.no/v0/domains"
        assert seen[0].headers["Authorization"].startswith("Basic ")
        with pytest.raises(ValueError):
            await client.get("https://evil.example/")
    finally:
        await client.close()


async def test_reads_retry_but_ddns_and_post_do_not(monkeypatch):
    calls = []

    async def no_sleep(_):
        pass

    monkeypatch.setattr("domeneshop_mcp.client.asyncio.sleep", no_sleep)

    def upstream(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "9999"})

    client = DomeneshopClient("test-token", "test-secret", transport=httpx.MockTransport(upstream))
    try:
        for method, path, expected in [
            ("GET", "domains", 3),
            ("GET", "dyndns/update", 1),
            ("POST", "domains/1/dns", 1),
        ]:
            calls.clear()
            with pytest.raises(APIError):
                await client.request(method, path)
            assert len(calls) == expected
    finally:
        await client.close()


async def test_tls_error_is_hard_stop():
    calls = []

    def upstream(request):
        calls.append(request)
        raise httpx.ConnectError("CERTIFICATE_VERIFY_FAILED test-secret", request=request)

    client = DomeneshopClient("test-token", "test-secret", transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(APIError, match="TLS") as caught:
            await client.get("domains")
        assert "test-secret" not in str(caught.value)
        assert len(calls) == 1
    finally:
        await client.close()


async def test_redirect_not_followed():
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    client = DomeneshopClient("test-token", "test-secret", transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(APIError, match="302"):
            await client.get("domains")
        assert len(calls) == 1
    finally:
        await client.close()
