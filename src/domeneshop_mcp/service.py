"""Domain operations and preview/apply workflow shared by both transports."""

import asyncio
import copy
import hashlib
import ipaddress
import json
import secrets
import time
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

from .client import APIError, DomeneshopClient
from .models import DNSChange, DNSRecord, hostname


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def record_payload(record: dict) -> dict:
    return DNSRecord.model_validate({k: v for k, v in record.items() if k != "id"}).payload()


def same_record(a: dict, b: dict) -> bool:
    return record_payload(a) == record_payload(b)


class Service:
    def __init__(
        self,
        client: DomeneshopClient,
        *,
        state_dir: Path,
        writable: bool = False,
        allowed_domains: set[str] | None = None,
        plan_ttl: int = 600,
        max_actions: int = 100,
    ):
        self.client = client
        self.state_dir = state_dir
        self.writable = writable
        self.allowed_domains = {hostname(x) for x in allowed_domains} if allowed_domains else None
        self.plan_ttl = plan_ttl
        self.max_actions = max_actions
        self.plans: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    def permitted(self, domain: dict) -> bool:
        return self.allowed_domains is None or hostname(domain["domain"]) in self.allowed_domains

    async def domains(self, query: str | None = None) -> list[dict]:
        rows = await self.client.get("domains", domain=query)
        return [row for row in rows if self.permitted(row)]

    async def domain(self, ref: str | int) -> dict:
        if type(ref) is int or (isinstance(ref, str) and ref.isdecimal()):
            if int(ref) <= 0:
                raise ValueError("Domain ID must be positive")
            result = await self.client.get(f"domains/{int(ref)}")
        else:
            name = hostname(str(ref))
            matches = [d for d in await self.domains(name) if hostname(d["domain"]) == name]
            if len(matches) != 1:
                raise ValueError("Domain not found uniquely; use exact domain name or ID")
            result = matches[0]
        if not self.permitted(result):
            raise ValueError("Domain is outside DOMENESHOP_ALLOWED_DOMAINS")
        return result

    async def snapshot(self, ref: str | int) -> dict:
        domain = await self.domain(ref)
        if not domain.get("services", {}).get("dns", False):
            raise ValueError("Domain does not have active Domeneshop DNS service")
        base = f"domains/{domain['id']}"
        dns, forwards = await asyncio.gather(
            self.client.get(f"{base}/dns"), self.client.get(f"{base}/forwards/")
        )
        return {
            "domain_id": domain["id"],
            "domain": domain["domain"],
            "dns": sorted(dns, key=lambda r: r["id"]),
            "forwards": sorted(forwards, key=lambda r: r["host"]),
        }

    async def dns_list(self, ref, host=None, record_type=None):
        d = await self.domain(ref)
        return await self.client.get(f"domains/{d['id']}/dns", host=host, type=record_type)

    async def dns_get(self, ref, record_id: int):
        d = await self.domain(ref)
        return await self.client.get(f"domains/{d['id']}/dns/{record_id}")

    async def forwards_list(self, ref):
        d = await self.domain(ref)
        return await self.client.get(f"domains/{d['id']}/forwards/")

    async def forward_get(self, ref, host):
        d = await self.domain(ref)
        return await self.client.get(
            f"domains/{d['id']}/forwards/{quote(hostname(host, relative=True), safe='')}"
        )

    def remember(self, title: str, snapshots: dict, actions: list, warnings=None) -> dict:
        if len(actions) > self.max_actions:
            raise ValueError(
                f"Plan exceeds the limit of {self.max_actions} operations; split the work"
            )
        now = time.time()
        self.plans = {
            k: v
            for k, v in self.plans.items()
            if v["expires_at"] > now or v["status"] == "applying"
        }
        if len(self.plans) >= 200:
            raise ValueError("Too many pending plans; wait for expiry")
        plan_id = secrets.token_urlsafe(24)
        plan = {
            "plan_id": plan_id,
            "title": title,
            "expires_at": now + self.plan_ttl,
            "snapshots": copy.deepcopy(snapshots),
            "actions": copy.deepcopy(actions),
            "warnings": warnings or [],
            "status": "preview",
            "result": None,
        }
        self.plans[plan_id] = plan
        return self.preview(plan)

    def preview(self, plan: dict) -> dict:
        return {
            "plan_id": plan["plan_id"],
            "title": plan["title"],
            "status": plan["status"],
            "expires_at": datetime.fromtimestamp(plan["expires_at"], UTC).isoformat(),
            "operation_count": len(plan["actions"]),
            "actions": copy.deepcopy(plan["actions"]),
            "warnings": plan["warnings"],
            "writes_enabled": self.writable,
            "next_step": "Review actions, then call apply_plan with this plan_id. "
            "The plan is an execution safeguard, not proof of human approval.",
        }

    def status(self, plan_id: str) -> dict:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise ValueError("Unknown plan; plans are local to this server process")
        return copy.deepcopy(plan["result"]) if plan["result"] else self.preview(plan)

    @staticmethod
    def check_dns_conflict(record: dict, records: list, forwards: list):
        peers = [r for r in records if r["host"] == record["host"]]
        if peers and (record["type"] == "CNAME" or any(r["type"] == "CNAME" for r in peers)):
            raise ValueError(f"CNAME conflict at {record['host']}; resolve explicitly")
        if record["type"] in {"A", "AAAA", "ANAME", "CNAME"}:
            if any(f["host"] == record["host"] for f in forwards):
                raise ValueError(f"DNS conflicts with an HTTP forward at {record['host']}")

    def build_dns_actions(self, snap: dict, changes: list[DNSChange]) -> list:
        records = copy.deepcopy(snap["dns"])
        actions = []
        touched = set()
        for change in changes:
            previous = None
            if change.record_id is not None:
                if change.record_id in touched:
                    raise ValueError("Each existing record may be changed only once per plan")
                touched.add(change.record_id)
                previous = next((r for r in records if r["id"] == change.record_id), None)
                if previous is None:
                    raise ValueError(f"Record ID {change.record_id} not found in this domain")
            new = change.record.payload() if change.record else None
            if new and previous and same_record(previous, new):
                continue
            peers = [r for r in records if r is not previous]
            if new:
                duplicates = [r for r in peers if same_record(r, new)]
                if duplicates:
                    if change.action == "create":
                        continue
                    raise ValueError("Update would duplicate an existing record")
                self.check_dns_conflict(new, peers, snap["forwards"])
            actions.append(
                {
                    "kind": "dns",
                    "action": change.action,
                    "domain_id": snap["domain_id"],
                    "record_id": change.record_id,
                    "before": previous,
                    "after": new,
                }
            )
            records = peers
            if new:
                records.append({**new, "id": change.record_id or -len(actions)})
        return actions

    async def dns_plan(self, ref, changes: list[DNSChange], title="DNS changes") -> dict:
        snap = await self.snapshot(ref)
        actions = self.build_dns_actions(snap, changes)
        warnings = (
            ["MX/TXT/SRV changes may affect mail or service verification."]
            if any((a["before"] or a["after"])["type"] in {"MX", "TXT", "SRV"} for a in actions)
            else []
        )
        return self.remember(title, {str(snap["domain_id"]): snap}, actions, warnings)

    async def forward_plan(self, ref, action: str, forward=None, host=None):
        snap = await self.snapshot(ref)
        host = forward.host if forward else hostname(host, relative=True)
        previous = next((f for f in snap["forwards"] if f["host"] == host), None)
        new = forward.model_dump() if forward else None
        if action == "create" and previous and previous != new:
            raise ValueError("Forward already exists; use update")
        if action != "create" and previous is None:
            raise ValueError("Forward not found")
        if new and any(
            r["host"] == host and r["type"] in {"A", "AAAA", "ANAME", "CNAME"} for r in snap["dns"]
        ):
            raise ValueError("Forward conflicts with an address/alias DNS record")
        actions = (
            []
            if previous == new
            else [
                {
                    "kind": "forward",
                    "action": action,
                    "domain_id": snap["domain_id"],
                    "host": host,
                    "before": previous,
                    "after": new,
                }
            ]
        )
        return self.remember("HTTP forward", {str(snap["domain_id"]): snap}, actions)

    async def ensure_records(
        self, ref, desired: list[DNSRecord], *, replace_rrsets=False, title="Ensure DNS records"
    ):
        snap = await self.snapshot(ref)
        desired_payloads = [r.payload() for r in desired]
        changes = []
        if replace_rrsets:
            keys = {(r.host, r.type) for r in desired}
            for old in snap["dns"]:
                if (old["host"], old["type"]) in keys and not any(
                    same_record(old, r) for r in desired_payloads
                ):
                    changes.append(DNSChange(action="delete", record_id=old["id"]))
        changes.extend(DNSChange(action="create", record=r) for r in desired)
        return self.remember(
            title, {str(snap["domain_id"]): snap}, self.build_dns_actions(snap, changes)
        )

    async def website(self, ref, ipv4: str | None, ipv6: str | None, www: bool, ttl: int):
        if not ipv4 and not ipv6:
            raise ValueError("Provide at least one IPv4 or IPv6 address")
        d = await self.domain(ref)
        records = []
        for kind, ip in (("A", ipv4), ("AAAA", ipv6)):
            if ip:
                records.append(DNSRecord(host="@", type=kind, data=ip, ttl=ttl))
        if www:
            records.append(DNSRecord(host="www", type="CNAME", data=d["domain"], ttl=ttl))
        return await self.ensure_records(
            d["id"], records, replace_rrsets=True, title="Website DNS (apex and optional www)"
        )

    async def replace_ip(self, refs: list, old_ip: str, new_ip: str):
        old, new = ipaddress.ip_address(old_ip), ipaddress.ip_address(new_ip)
        if old.version != new.version:
            raise ValueError("Old and new IP must have the same address family")
        kind = "A" if old.version == 4 else "AAAA"
        snapshots, actions = {}, []
        for ref in refs:
            snap = await self.snapshot(ref)
            key = str(snap["domain_id"])
            if key in snapshots:
                continue
            snapshots[key] = snap
            changes = [
                DNSChange(
                    action="update",
                    record_id=r["id"],
                    record=DNSRecord.model_validate({**record_payload(r), "data": str(new)}),
                )
                for r in snap["dns"]
                if r["type"] == kind and ipaddress.ip_address(r["data"]) == old
            ]
            actions.extend(self.build_dns_actions(snap, changes))
        return self.remember(
            "Replace exact IP in selected domains",
            snapshots,
            actions,
            ["Changes every matching address record, including mail hosts."],
        )

    async def copy_dns(self, source, target, hosts: list[str] | None = None):
        source_snap = await self.snapshot(source)
        target_snap = await self.snapshot(target)
        if source_snap["domain_id"] == target_snap["domain_id"]:
            raise ValueError("Source and target must differ")
        selected = {hostname(h, relative=True) for h in hosts} if hosts is not None else None
        records = [
            DNSRecord.model_validate(record_payload(r))
            for r in source_snap["dns"]
            if selected is None or r["host"] in selected
        ]
        actions = self.build_dns_actions(
            target_snap, [DNSChange(action="create", record=r) for r in records]
        )
        # Both source and target are checked again at execution time.
        return self.remember(
            "Copy DNS records",
            {str(s["domain_id"]): s for s in (source_snap, target_snap)},
            actions,
            ["Absolute targets are copied unchanged; source-domain names are not rewritten."],
        )

    async def ddns_plan(self, hostnames: list[str], ips: list[str] | None, use_request_ip=False):
        if not hostnames or len(hostnames) > 100:
            raise ValueError("Provide 1-100 hostnames")
        if not ips and not use_request_ip:
            raise ValueError("Provide IPs or explicitly set use_request_ip=true (server egress IP)")
        if ips and use_request_ip:
            raise ValueError("Choose explicit IPs or request IP")
        addresses = list(dict.fromkeys(str(ipaddress.ip_address(ip)) for ip in ips or []))
        if len(addresses) > 9:
            raise ValueError("Domeneshop allows at most nine IP addresses")
        domains = await self.domains()
        snapshots, targets = {}, []
        for raw in dict.fromkeys(hostnames):
            fqdn = hostname(raw)
            matches = [
                d
                for d in domains
                if fqdn == hostname(d["domain"]) or fqdn.endswith("." + hostname(d["domain"]))
            ]
            if not matches:
                raise ValueError("DDNS hostname has no accessible domain")
            d = max(matches, key=lambda row: len(row["domain"]))
            key = str(d["id"])
            if key not in snapshots:
                snapshots[key] = await self.snapshot(d["id"])
            relative = (
                "@" if fqdn == hostname(d["domain"]) else fqdn[: -(len(hostname(d["domain"])) + 1)]
            )
            for kind in {
                "A" if ipaddress.ip_address(ip).version == 4 else "AAAA" for ip in addresses
            } or {"A", "AAAA"}:
                self.check_dns_conflict(
                    {"host": relative, "type": kind},
                    snapshots[key]["dns"],
                    snapshots[key]["forwards"],
                )
            targets.append({"domain_id": d["id"], "host": relative, "fqdn": fqdn})
        return self.remember(
            "Dynamic DNS update",
            snapshots,
            [{"kind": "ddns", "targets": targets, "ips": addresses}],
            [
                "Request-IP mode uses the MCP server's egress IP. Readback cannot "
                "prove it equals the intended client address."
            ]
            if not addresses
            else [],
        )

    def persist(self, name: str, data: dict):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state_dir / name
        # Exclusive creation prevents accidental overwrites and symlink following.
        with path.open("x", encoding="utf-8") as handle:
            try:
                path.chmod(0o600)
            except OSError:
                pass  # On Windows the parent directory ACL is the access boundary.
            json.dump(data, handle, ensure_ascii=False, indent=2)

    async def apply(self, plan_id: str):
        async with self.lock:
            plan = self.plans.get(plan_id)
            if plan is None:
                raise ValueError("Unknown plan; preview again")
            if plan["result"]:
                return copy.deepcopy(plan["result"])
            if plan["status"] != "preview":
                raise ValueError("Plan already attempted; inspect live state and preview again")
            if plan["expires_at"] < time.time():
                raise ValueError("Plan expired; preview again")
            if not self.writable:
                raise ValueError(
                    "Writes are disabled; set DOMENESHOP_ALLOW_WRITES=true on the server"
                )
            # Optimistic concurrency: do a full preflight before the first write.
            for key, before in plan["snapshots"].items():
                if fingerprint(await self.snapshot(int(key))) != fingerprint(before):
                    raise ValueError("DNS/forward state changed since preview; build a new plan")
            self.persist(
                f"{plan_id}.backup.json",
                {
                    "version": 1,
                    "plan_id": plan_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "snapshots": plan["snapshots"],
                    "actions": plan["actions"],
                },
            )
            plan["status"] = "applying"
            completed = []
            expected = copy.deepcopy(plan["snapshots"])
            result = {
                "plan_id": plan_id,
                "status": "verified",
                "completed": completed,
                "operation_count": len(plan["actions"]),
                "backup": f"{plan_id}.backup.json",
            }
            try:
                for action in plan["actions"]:
                    # Detect intervening external changes between operations as well.
                    keys = (
                        {str(t["domain_id"]) for t in action["targets"]}
                        if action["kind"] == "ddns"
                        else {str(action["domain_id"])}
                    )
                    for key in keys:
                        if fingerprint(await self.snapshot(int(key))) != fingerprint(expected[key]):
                            raise ValueError(
                                "Concurrent change detected; remaining operations stopped"
                            )
                    receipt = await self.execute(action)
                    completed.append(receipt)
                    if not receipt["verified"]:
                        result["status"] = "accepted_unverified"
                        break
                    for key in keys:
                        # Derive expected state from the previous snapshot and the exact readback.
                        # Never adopt unrelated new state and silently treat it as our own change.
                        self.advance_expected(expected[key], action, receipt)
                        if fingerprint(await self.snapshot(int(key))) != fingerprint(expected[key]):
                            raise ValueError(
                                "Unexpected state after write; remaining operations stopped"
                            )
            except asyncio.CancelledError:
                result["status"] = "partial_or_unknown"
                result["error"] = (
                    "Execution cancelled; inspect live state before preparing a new plan"
                )
                raise
            except Exception as error:
                result["status"] = "partial_or_unknown"
                result["error"] = (
                    str(error)
                    if isinstance(error, (APIError, ValueError))
                    else "Unexpected API response or execution failure"
                )
                result["next_step"] = (
                    "Inspect live DNS and backup before preparing a new plan; "
                    "writes are not automatically retried or rolled back."
                )
            finally:
                # Cancellation also consumes the plan. A restart loses plans, never replays them.
                plan["status"] = result["status"]
                plan["result"] = copy.deepcopy(result)
                try:
                    self.persist(f"{plan_id}.result.json", result)
                except OSError:
                    result["receipt_warning"] = (
                        "Could not persist result; pre-change backup was saved"
                    )
                    plan["result"] = copy.deepcopy(result)
            return result

    @staticmethod
    def advance_expected(snap, action, receipt):
        if action["kind"] == "dns":
            snap["dns"] = [r for r in snap["dns"] if r["id"] != action["record_id"]]
            if receipt["readback"] is not None:
                snap["dns"].append(receipt["readback"])
            snap["dns"].sort(key=lambda r: r["id"])
        elif action["kind"] == "forward":
            snap["forwards"] = [f for f in snap["forwards"] if f["host"] != action["host"]]
            if receipt["readback"] is not None:
                snap["forwards"].append(receipt["readback"])
            snap["forwards"].sort(key=lambda r: r["host"])
        else:
            # DDNS is one opaque upstream operation; verify target data and report observed state.
            snap.update(receipt["snapshots"][str(snap["domain_id"])])

    async def execute(self, action):
        if action["kind"] == "ddns":
            params = {"hostname": ",".join(t["fqdn"] for t in action["targets"])}
            if action["ips"]:
                params["myip"] = ",".join(action["ips"])
            await self.client.request("GET", "dyndns/update", params=params)
            snapshots = {
                str(t["domain_id"]): await self.snapshot(t["domain_id"]) for t in action["targets"]
            }
            verified = bool(action["ips"])
            for target in action["targets"]:
                actual = snapshots[str(target["domain_id"])]["dns"]
                for version in {ipaddress.ip_address(ip).version for ip in action["ips"]}:
                    wanted = {
                        str(ipaddress.ip_address(ip))
                        for ip in action["ips"]
                        if ipaddress.ip_address(ip).version == version
                    }
                    found = {
                        str(ipaddress.ip_address(r["data"]))
                        for r in actual
                        if r["host"] == target["host"]
                        and r["type"] == ("A" if version == 4 else "AAAA")
                    }
                    verified = verified and wanted == found
            return {"action": action, "verified": verified, "snapshots": snapshots}
        base = f"domains/{action['domain_id']}"
        dns = action["kind"] == "dns"
        collection = f"{base}/dns" if dns else f"{base}/forwards/"
        identifier = action.get("record_id") if dns else quote(action["host"], safe="")
        path = f"{collection.rstrip('/')}/{identifier}"
        verb = {"create": "POST", "update": "PUT", "delete": "DELETE"}[action["action"]]
        reply = await self.client.request(
            verb, collection if verb == "POST" else path, body=action["after"]
        )
        if verb == "POST" and dns:
            identifier = reply.data.get("id") if isinstance(reply.data, dict) else None
            if identifier is None and reply.location:
                tail = reply.location.rstrip("/").split("/")[-1]
                if tail.isdecimal():
                    identifier = int(tail)
            if type(identifier) is not int or identifier <= 0:
                raise ValueError(
                    "Create was accepted but record ID is unknown; inspect DNS before retry"
                )
            path = f"{collection}/{identifier}"
        if verb == "DELETE":
            try:
                await self.client.get(path)
            except APIError as error:
                if error.status != 404:
                    raise
            else:
                raise ValueError("Delete was accepted but resource still exists")
            readback = None
        else:
            readback = await self.client.get(path)
            matches = same_record(readback, action["after"]) if dns else readback == action["after"]
            if not matches:
                raise ValueError("Write was accepted but exact readback differs from desired state")
        return {"action": action, "verified": True, "readback": readback}

    async def restore(self, backup_id: str, ref):
        if not all(c.isalnum() or c in "_-" for c in backup_id) or len(backup_id) != 32:
            raise ValueError("Invalid backup ID")
        d = await self.domain(ref)
        path = self.state_dir / f"{backup_id}.backup.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        source = data["snapshots"].get(str(d["id"]))
        if not source or source["domain"] != d["domain"]:
            raise ValueError("Backup does not belong to this domain")
        snap = await self.snapshot(d["id"])
        # Restore DNS only. Forward changes need their own explicit preview.
        desired = [DNSRecord.model_validate(record_payload(r)) for r in source["dns"]]
        changes = [
            DNSChange(action="delete", record_id=r["id"])
            for r in snap["dns"]
            if not any(same_record(r, wanted.payload()) for wanted in desired)
        ]
        changes.extend(DNSChange(action="create", record=r) for r in desired)
        return self.remember(
            "Restore DNS from backup",
            {str(d["id"]): snap},
            self.build_dns_actions(snap, changes),
            [
                "Restores DNS content; recreated records receive new IDs. "
                "HTTP forwards are not restored by this tool."
            ],
        )

    async def audit(self, ref):
        snap = await self.snapshot(ref)
        findings = []
        records = snap["dns"]
        counts = Counter(fingerprint(record_payload(r)) for r in records)
        if any(c > 1 for c in counts.values()):
            findings.append({"severity": "warning", "code": "duplicate_records"})
        for host in {r["host"] for r in records}:
            peers = [r for r in records if r["host"] == host]
            if any(r["type"] == "CNAME" for r in peers) and len(peers) > 1:
                findings.append({"severity": "error", "code": "cname_collision", "host": host})
            spf = [
                r for r in peers if r["type"] == "TXT" and r["data"].lower().startswith("v=spf1")
            ]
            if len(spf) > 1:
                findings.append({"severity": "error", "code": "multiple_spf_records", "host": host})
        if any(r["type"] == "MX" for r in records):
            if not any(r["host"] == "_dmarc" and r["type"] == "TXT" for r in records):
                findings.append({"severity": "info", "code": "dmarc_not_found"})
        return {
            "domain": snap["domain"],
            "record_count": len(records),
            "findings": findings,
            "scope": "API configuration only; no public propagation, SPF expansion, "
            "DKIM selector discovery or mail-delivery test",
        }

    async def overview(self, expiry_days: int = 30):
        domains = await self.domains()
        today = date.today()
        expiring = []
        for d in domains:
            try:
                remaining = (date.fromisoformat(d["expiry_date"]) - today).days
            except (KeyError, ValueError, TypeError):
                continue
            if remaining <= expiry_days:
                expiring.append({**d, "days_remaining": remaining})
        return {
            "domain_count": len(domains),
            "status_counts": dict(Counter(d.get("status", "unknown") for d in domains)),
            "expiring": expiring,
            "as_of": today.isoformat(),
        }
