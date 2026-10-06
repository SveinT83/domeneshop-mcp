import copy
import json

import httpx
import pytest

from domeneshop_mcp.client import DomeneshopClient
from domeneshop_mcp.service import Service


class FakeAPI:
    """Stateful upstream double. No live credentials or network requests."""

    def __init__(self):
        self.domains = [
            {
                "id": 1,
                "domain": "example.no",
                "services": {"dns": True},
                "expiry_date": "2099-01-01",
                "status": "active",
            },
            {
                "id": 2,
                "domain": "other.no",
                "services": {"dns": True},
                "expiry_date": "2000-01-01",
                "status": "expired",
            },
        ]
        self.dns = {
            1: [
                {"id": 10, "host": "@", "type": "A", "data": "192.0.2.1", "ttl": 3600},
                {
                    "id": 11,
                    "host": "@",
                    "type": "MX",
                    "data": "mail.example.no",
                    "ttl": 3600,
                    "priority": 10,
                },
                {"id": 12, "host": "@", "type": "TXT", "data": "v=spf1 -all", "ttl": 3600},
            ],
            2: [],
        }
        self.forwards = {1: [], 2: []}
        self.invoices = [{"id": 1, "status": "unpaid", "amount": 100, "currency": "NOK"}]
        self.calls = []
        self.counter = 100
        self.location_only = False
        self.fail_write_number = None
        self.writes = 0
        self.ignore_write = False
        self.after_write = None

    def __call__(self, request):
        self.calls.append(request)
        assert request.url.host == "api.domeneshop.no"
        assert request.url.path.startswith("/v0/")
        parts = request.url.path.removeprefix("/v0/").strip("/").split("/")
        method = request.method
        write = method != "GET" or parts[0] == "dyndns"
        if write:
            self.writes += 1
            if self.writes == self.fail_write_number:
                return httpx.Response(503)
        payload = json.loads(request.content) if request.content else None
        if parts[0] == "dyndns":
            for fqdn in request.url.params["hostname"].split(","):
                d = max(
                    [
                        d
                        for d in self.domains
                        if fqdn == d["domain"] or fqdn.endswith("." + d["domain"])
                    ],
                    key=lambda d: len(d["domain"]),
                )
                host = "@" if fqdn == d["domain"] else fqdn[: -(len(d["domain"]) + 1)]
                addresses = request.url.params.get("myip", "192.0.2.99").split(",")
                types = {"AAAA" if ":" in ip else "A" for ip in addresses}
                self.dns[d["id"]] = [
                    r for r in self.dns[d["id"]] if r["host"] != host or r["type"] not in types
                ]
                for ip in addresses:
                    self.counter += 1
                    self.dns[d["id"]].append(
                        {
                            "id": self.counter,
                            "host": host,
                            "type": "AAAA" if ":" in ip else "A",
                            "data": ip,
                            "ttl": 3600,
                        }
                    )
            return httpx.Response(204)
        if parts[0] == "invoices":
            if len(parts) == 2:
                row = next((r for r in self.invoices if r["id"] == int(parts[1])), None)
                return httpx.Response(200, json=row) if row else httpx.Response(404)
            status = request.url.params.get("status")
            return httpx.Response(
                200, json=[r for r in self.invoices if not status or r["status"] == status]
            )
        if len(parts) == 1:
            query = request.url.params.get("domain", "")
            return httpx.Response(200, json=[d for d in self.domains if query in d["domain"]])
        domain_id = int(parts[1])
        domain = next((d for d in self.domains if d["id"] == domain_id), None)
        if domain is None:
            return httpx.Response(404)
        if len(parts) == 2:
            return httpx.Response(200, json=domain)
        dns = parts[2] == "dns"
        store = self.dns if dns else self.forwards
        rows = store[domain_id]
        key = "id" if dns else "host"
        identifier = (
            int(parts[3]) if len(parts) == 4 and dns else parts[3] if len(parts) == 4 else None
        )
        current = next((r for r in rows if r[key] == identifier), None)
        if method == "GET":
            if identifier is not None:
                return httpx.Response(200, json=current) if current else httpx.Response(404)
            selected = [
                r for r in rows if all(r.get(k) == v for k, v in request.url.params.items())
            ]
            return httpx.Response(200, json=selected)
        if self.ignore_write:
            return httpx.Response(204)
        if method == "POST":
            self.counter += 1
            row = {**payload, "id": self.counter} if dns else payload
            rows.append(row)
            response = (
                httpx.Response(
                    201, headers={"Location": f"/v0/domains/{domain_id}/dns/{self.counter}"}
                )
                if self.location_only
                else httpx.Response(201, json={"id": self.counter})
            )
        elif current is None:
            return httpx.Response(404)
        elif method == "PUT":
            current.clear()
            current.update({**payload, "id": identifier} if dns else payload)
            response = httpx.Response(204)
        else:
            rows.remove(current)
            response = httpx.Response(204)
        if self.after_write:
            self.after_write(self)
        return response


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
async def service(api, tmp_path):
    client = DomeneshopClient("test-token", "test-secret", transport=httpx.MockTransport(api))
    service = Service(client, state_dir=tmp_path / "state", writable=True)
    yield service
    await client.close()


@pytest.fixture
def original(api):
    return copy.deepcopy(api.dns)
