import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from domeneshop_mcp.server import BearerAuth, create_server


async def test_real_mcp_handshake_schemas_and_tools(service, api):
    server = create_server(service)
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        assert len(tools) == 27
        by_name = {t.name: t for t in tools}
        assert not by_name["apply_plan"].annotations.readOnlyHint
        assert by_name["update_dynamic_dns"].annotations.readOnlyHint
        listed = await session.call_tool("list_domains", {})
        assert not listed.isError
        assert listed.structuredContent["count"] == 2
        preview = await session.call_tool(
            "create_dns_record",
            {"domain": "example.no", "record": {"host": "mcp-test", "type": "TXT", "data": "æøå"}},
        )
        assert not preview.isError
        assert api.writes == 0
        result = await session.call_tool(
            "apply_plan", {"plan_id": preview.structuredContent["plan_id"]}
        )
        assert result.structuredContent["status"] == "verified"
        invalid = await session.call_tool("delete_dns_record", {"domain": 1, "record_id": -1})
        assert invalid.isError
        resources = await session.list_resources()
        assert str(resources.resources[0].uri) == "domeneshop://capabilities"


async def test_invoice_filters_and_domain_scope(service):
    server = create_server(service)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("list_invoices", {"status": "unpaid"})
        assert result.structuredContent["count"] == 1
        invoice = await session.call_tool("get_invoice", {"invoice_id": 1})
        assert invoice.structuredContent["id"] == 1
        service.allowed_domains = {"example.no"}
        denied = await session.call_tool("list_invoices", {})
        assert denied.isError


async def test_http_auth_host_guard_and_initialize(service):
    mcp = create_server(service)
    inner = mcp.streamable_http_app()
    app = BearerAuth(inner, "x" * 32)
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
            assert (await client.post("/mcp", json=payload)).status_code == 401
            headers = {
                "Authorization": "Bearer " + "x" * 32,
                "Accept": "application/json, text/event-stream",
            }
            ok = await client.post("/mcp", json=payload, headers=headers)
            assert ok.status_code == 200
            assert ok.json()["result"]["serverInfo"]["name"] == "Domeneshop MCP"
            bad_host = await client.post(
                "/mcp", json=payload, headers={**headers, "Host": "evil.example"}
            )
            assert bad_host.status_code == 421
            bad_origin = await client.post(
                "/mcp", json=payload, headers={**headers, "Origin": "https://evil.example"}
            )
            assert bad_origin.status_code == 403


def test_http_token_required():
    with pytest.raises(ValueError, match="32"):
        BearerAuth(None, "short")


async def test_stateless_http_keeps_api_client_and_plans_between_requests(service, api):
    inner = create_server(service).streamable_http_app()
    app = BearerAuth(inner, "x" * 32)
    headers = {
        "Authorization": "Bearer " + "x" * 32,
        "Accept": "application/json, text/event-stream",
    }
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers=headers,
        ) as client:
            initialized = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200

            async def call(name, arguments):
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                assert response.status_code == 200
                result = response.json()["result"]
                assert not result.get("isError"), result
                return result["structuredContent"]

            assert (await call("list_domains", {}))["count"] == 2
            preview = await call(
                "add_verification_txt",
                {
                    "domain": "example.no",
                    "host": "_verify",
                    "value": "test",
                },
            )
            assert api.writes == 0
            assert (await call("get_plan", {"plan_id": preview["plan_id"]}))["status"] == "preview"
            applied = await call("apply_plan", {"plan_id": preview["plan_id"]})
            assert applied["status"] == "verified"
            records = await call("list_dns_records", {"domain": "example.no"})
            assert any(r["host"] == "_verify" for r in records["records"])


async def test_stdio_subprocess_handshake_and_capabilities():
    # Only protocol methods: these synthetic credentials never reach an external API.
    env = {**os.environ, "DOMENESHOP_TOKEN": "test-token", "DOMENESHOP_SECRET": "test-secret"}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "domeneshop_mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=20)
        ) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 27
            resource = await session.read_resource("domeneshop://capabilities")
            assert json.loads(resource.contents[0].text)["documented_operations"] == 15


def test_documented_contract_has_full_mapping():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs/domeneshop-openapi.json").read_text(encoding="utf-8"))
    coverage = (root / "docs/api-coverage.md").read_text(encoding="utf-8")
    operations = [
        (method, path)
        for path, spec in schema["paths"].items()
        for method in spec
        if method in {"get", "post", "put", "delete"}
    ]
    assert len(operations) == 15
    for method, path in operations:
        assert f"`{path}`" in coverage
        assert method.upper() in coverage
